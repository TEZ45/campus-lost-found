"""Early shared request budgets with atomic fixed-window counters.

Each window allows its configured limit. A client can use twice that allowance
across a window boundary; the independent short and long windows bound bursts.
Redis is required outside DEBUG. LocMemCache is only for one-process development.
"""

import hashlib
import ipaddress
import logging
import math
import re
import time

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache
from django.core.checks import Error, Tags, register
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse

logger = logging.getLogger(__name__)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UPLOAD_PATHS = (
    re.compile(r"^/report/(?:new|\d+/edit|\d+/claim)/$"),
    re.compile(r"^/claim/\d+/resubmit/$"),
    re.compile(r"^/api/v1/reports/(?:\d+/(?:claims/)?)?$"),
    re.compile(r"^/api/v1/claims/\d+/resubmit/$"),
)


class RequestLimitUnavailable(Exception):
    """The shared request guard cannot safely account for this operation."""


def guard_response(request, status, code, message, retry_after=None):
    if request.path_info.startswith("/api/"):
        response = JsonResponse({"error": {"code": code, "details": message}}, status=status)
    else:
        response = HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "private, no-store"
    if retry_after is not None:
        response["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return response


def canonical_ip(value):
    if "%" in value:
        raise ValueError("Scoped addresses are not client identities.")
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def client_ip(request):
    """Use forwarded hops only when the socket peer is a configured proxy.

    Strip trusted hops from the right. Untrusted clients cannot choose a budget
    by supplying a header; malformed chains fall back to the socket peer.
    """
    try:
        peer = canonical_ip(request.META.get("REMOTE_ADDR", ""))
    except ValueError:
        return "unknown"
    trusted = [ipaddress.ip_network(value) for value in settings.REQUEST_LIMITS_TRUSTED_PROXIES]

    def is_trusted(address):
        return any(address.version == network.version and address in network for network in trusted)

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not is_trusted(peer) or not forwarded:
        return str(peer)
    # Bound parsing work even if the trusted proxy preserved an attacker header.
    if len(forwarded) > 2048 or forwarded.count(",") >= 32:
        return str(peer)
    try:
        chain = [canonical_ip(value) for value in forwarded.split(",")] + [peer]
    except ValueError:
        return str(peer)
    while len(chain) > 1 and is_trusted(chain[-1]):
        chain.pop()
    return str(chain[-1])


def guard_cache():
    backend = caches[settings.REQUEST_LIMITS_CACHE_ALIAS]
    if isinstance(backend, RedisCache):
        return backend
    if settings.DEBUG and isinstance(backend, LocMemCache):
        return backend
    raise RequestLimitUnavailable("Request limits require a supported shared atomic cache.")


def consume_budget(backend, scope, identity, limit, seconds, now=None):
    """Return wait seconds, or zero if admitted. add/incr must be atomic.

    Counters expire after a grace period past their fixed window, preventing an
    expiry between add and incr during normal use. Failed increments and backend
    failures fail closed. Production Redis must not evict active counters.
    """
    now = time.time() if now is None else now
    window = int(now // seconds)
    retry_after = max(1, math.ceil((window + 1) * seconds - now))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    key = f"request-limits:v1:{scope}:{digest}:{window}"
    try:
        if backend.add(key, 1, timeout=retry_after + seconds):
            count = 1
        else:
            count = backend.incr(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("Invalid request limit counter.")
    except Exception as exc:
        raise RequestLimitUnavailable("Request limit accounting failed.") from exc
    return retry_after if count > limit else 0


def endpoint_budgets(request):
    path = request.path_info
    if request.method in WRITE_METHODS:
        budgets = ["write"]
        if path in {"/login/", "/api/v1/auth/login/"}:
            budgets.append("login")
        elif path in {"/register/", "/api/v1/auth/register/"}:
            budgets.append("register")
        if (request.content_type == "multipart/form-data"
                and any(pattern.fullmatch(path) for pattern in UPLOAD_PATHS)):
            budgets.append("upload")
        return budgets
    if request.method in {"GET", "HEAD"} and path in {
        "/", "/api/v1/reports/", "/staff/reports/", "/api/v1/staff/reports/",
    }:
        return ["search"]
    return []


class BodyLimitStream:
    """Cap actual stream reads, including requests with a missing/false length."""

    def __init__(self, stream, request, limit):
        self.stream = stream
        self.request = request
        self.limit = limit
        self.bytes_read = 0

    def _read(self, method, size=-1):
        remaining = self.limit - self.bytes_read
        size = min(size, remaining + 1) if size is not None and size >= 0 else remaining + 1
        data = getattr(self.stream, method)(size)
        self.bytes_read += len(data)
        if self.bytes_read > self.limit:
            self.request._request_limits_body_rejected = True
            from .upload_limits import close_upload_files

            close_upload_files(self.request)
            raise RequestDataTooBig("Request body exceeds the total byte limit.")
        return data

    def read(self, size=-1):
        return self._read("read", size)

    def readline(self, size=-1):
        return self._read("readline", size)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class RequestProtectionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def _check(self, request, identity, scopes):
        try:
            backend = guard_cache()
            waits = []
            for scope in scopes:
                windows = settings.REQUEST_LIMITS[scope]
                if identity[0] == "ip" and scope not in {"ip_burst", "ip_sustained"}:
                    windows = settings.REQUEST_LIMITS_IP.get(scope, [
                        (limit * settings.REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER, seconds)
                        for limit, seconds in windows
                    ])
                for limit, seconds in windows:
                    waits.append(consume_budget(
                        backend, f"{identity[0]}:{scope}:{seconds}", identity[1], limit, seconds,
                    ))
            retry_after = max(waits, default=0)
        except Exception as exc:  # noqa: BLE001 - Backend failures deny requests.
            logger.warning("Request protection is unavailable: %s", type(exc).__name__)
            return guard_response(
                request, 503, "request_guard_unavailable", "Please retry shortly.", retry_after=5,
            )
        if retry_after:
            return guard_response(
                request, 429, "rate_limited", "Too many requests. Please retry later.",
                retry_after=retry_after,
            )
        return None

    def __call__(self, request):
        raw_length = request.META.get("CONTENT_LENGTH", "")
        try:
            length = int(raw_length) if raw_length else 0
        except (TypeError, ValueError):
            return guard_response(request, 400, "invalid_content_length", "Invalid Content-Length.")
        if length < 0:
            return guard_response(request, 400, "invalid_content_length", "Invalid Content-Length.")
        if length > settings.REQUEST_MAX_BODY_BYTES:
            return guard_response(request, 413, "request_too_large", "Request body is too large.")
        rejection = self._check(
            request, ("ip", client_ip(request)), ["ip_burst", "ip_sustained", *endpoint_budgets(request)],
        )
        if rejection is not None:
            return rejection
        request._stream = BodyLimitStream(request._stream, request, settings.REQUEST_MAX_BODY_BYTES)
        try:
            response = self.get_response(request)
        except RequestDataTooBig:
            if not (
                getattr(request, "_request_limits_body_rejected", False)
                or getattr(request, "_request_limits_upload_rejected", False)
            ):
                raise
            response = None
        if getattr(request, "_request_limits_body_rejected", False):
            return guard_response(request, 413, "request_too_large", "Request body is too large.")
        if getattr(request, "_request_limits_upload_rejected", False):
            return guard_response(request, 413, "upload_too_large", "Total uploaded files are too large.")
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return self._check(
                request, ("account", str(user.pk)),
                ["account_burst", "account_sustained", *endpoint_budgets(request)],
            )
        return None


def check_request_limit_configuration(app_configs=None, **kwargs):
    errors = []
    alias = settings.REQUEST_LIMITS_CACHE_ALIAS
    backend = settings.CACHES.get(alias, {}).get("BACKEND", "")
    supported = backend == "django.core.cache.backends.redis.RedisCache"
    development = settings.DEBUG and backend == "django.core.cache.backends.locmem.LocMemCache"
    if not supported and not development:
        errors.append(Error(
            "Request limits require Django RedisCache outside DEBUG development.",
            hint="Set REQUEST_LIMITS_REDIS_URL to the shared Redis primary URL.",
            id="reports.E001",
        ))
    try:
        for value in settings.REQUEST_LIMITS_TRUSTED_PROXIES:
            ipaddress.ip_network(value)
    except ValueError:
        errors.append(Error("Invalid trusted proxy network.", id="reports.E002"))
    return errors


def register_request_limit_checks():
    register(Tags.caches)(check_request_limit_configuration)
