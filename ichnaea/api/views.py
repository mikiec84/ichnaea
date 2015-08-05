"""
Implementation of a API specific HTTP service view.
"""

import colander
import simplejson as json
import six

from ichnaea.api.exceptions import (
    DailyLimitExceeded,
    InvalidAPIKey,
    ParseError,
)
from ichnaea.models.api import ApiKey
from ichnaea.rate_limit import rate_limit_exceeded
from ichnaea import util
from ichnaea.webapp.view import BaseView

if six.PY2:  # pragma: no cover
    from ipaddr import IPAddress as ip_address  # NOQA
else:  # pragma: no cover
    from ipaddress import ip_address


class BaseAPIView(BaseView):
    """Common base class for all API related views."""

    check_api_key = True  #: Should API keys be checked?
    error_on_invalidkey = True  #: Deny access for invalid API keys?
    metric_path = None  #: Dotted URL path, for example v1.submit.
    view_type = None  #: The type of view, for example submit or locate.

    def __init__(self, request):
        super(BaseAPIView, self).__init__(request)
        self.raven_client = request.registry.raven_client
        self.redis_client = request.registry.redis_client
        self.stats_client = request.registry.stats_client

    def log_unique_ip(self, apikey_shortname):
        try:
            ip = str(ip_address(self.request.client_addr))
        except ValueError:  # pragma: no cover
            ip = None
        if ip:
            redis_key = 'apiuser:{api_type}:{api_name}:{date}'.format(
                api_type=self.view_type,
                api_name=apikey_shortname,
                date=util.utcnow().date().strftime('%Y-%m-%d'),
            )
            with self.redis_client.pipeline() as pipe:
                pipe.pfadd(redis_key, ip)
                pipe.expire(redis_key, 691200)  # 8 days
                pipe.execute()

    def log_count(self, apikey_shortname, apikey_log):
        self.stats_client.incr(
            self.view_type + '.request',
            tags=['path:' + self.metric_path,
                  'key:' + apikey_shortname])

        if self.request.client_addr and apikey_log:
            try:
                self.log_unique_ip(apikey_shortname)
            except Exception:  # pragma: no cover
                self.raven_client.captureException()

    def check(self):
        api_key = None
        api_key_text = self.request.GET.get('key', None)

        if api_key_text is None:
            self.log_count('none', False)
            if self.error_on_invalidkey:
                raise InvalidAPIKey()
        try:
            api_key = ApiKey.getkey(self.request.db_ro_session,
                                    {'valid_key': api_key_text})
        except Exception:  # pragma: no cover
            # if we cannot connect to backend DB, skip api key check
            self.raven_client.captureException()

        if api_key is not None:
            self.log_count(api_key.name, api_key.log)

            rate_key = 'apilimit:{key}:{time}'.format(
                key=api_key_text,
                time=util.utcnow().strftime('%Y%m%d')
            )

            should_limit = rate_limit_exceeded(
                self.redis_client,
                rate_key,
                maxreq=api_key.maxreq
            )

            if should_limit:
                raise DailyLimitExceeded()
        else:
            if api_key_text is not None:
                self.log_count('invalid', False)
            if self.error_on_invalidkey:
                raise InvalidAPIKey()

        # If we failed to look up an ApiKey, create an empty one
        # rather than passing None through
        api_key = api_key or ApiKey(valid_key=None)
        return self.view(api_key)

    def preprocess_request(self):
        errors = []

        request_content = self.request.body
        if self.request.headers.get('Content-Encoding') == 'gzip':
            # handle gzip self.request bodies
            try:
                request_content = util.decode_gzip(self.request.body)
            except OSError as exc:
                errors.append({'name': None, 'description': repr(exc)})

        request_data = {}
        try:
            request_data = json.loads(
                request_content, encoding=self.request.charset)
        except ValueError as exc:
            errors.append({'name': None, 'description': repr(exc)})

        validated_data = {}
        try:
            validated_data = self.schema().deserialize(request_data)
        except colander.Invalid as exc:
            errors.append({'name': None, 'description': exc.asdict()})

        if request_content and errors:
            raise ParseError()

        return (validated_data, errors)

    def __call__(self):
        """Execute the view and return a response."""
        if self.check_api_key:
            return self.check()
        else:
            api_key = ApiKey(valid_key=None, allow_fallback=False, log=False)
            return self.view(api_key)
