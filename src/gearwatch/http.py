"""The request layer: secret redaction, rate limiting, retries, and a TTL cache.

Design notes that matter for review:

* Every exception message produced here is passed through :func:`redact` before
  it reaches the ``Exception`` constructor. A secret that is never placed in the
  args tuple cannot leak through ``str()``, ``repr()``, ``traceback``, or the
  logging module.
* The ``Authorization`` header is never logged, never cached, and never part of
  a cache key. The cache key is derived from method, URL, and request body only,
  which also means an expired token does not invalidate the disk cache.
* The rate limiter and every sleeping code path take an injected clock and
  sleeper so tests can assert on timing without spending wall clock time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "GearwatchHttpError",
    "RequestTimeout",
    "HttpStatusError",
    "RateLimitError",
    "RetryExhausted",
    "TokenBucket",
    "DiskCache",
    "HttpResponse",
    "HttpClient",
    "register_secret",
    "forget_secrets",
    "redact",
    "safe_headers",
    "RedactingFilter",
    "install_redacting_filter",
    "REDACTED",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_RATE_PER_SECOND",
    "DEFAULT_BURST",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
]

REDACTED = "[redacted]"

#: Six hours. A used-gear sold price does not move meaningfully inside a day, so
#: this keeps a re-run of ``gearwatch sync`` off the network entirely while still
#: refreshing at least a few times per day.
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60

#: Deliberately conservative. eBay grants far more than this; the point is to be
#: a good citizen by default and let the operator raise it knowingly.
DEFAULT_RATE_PER_SECOND = 1.0
DEFAULT_BURST = 3.0

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_CAP_SECONDS = 30.0

#: Response headers we are willing to persist to disk alongside a cached body.
_CACHEABLE_RESPONSE_HEADERS = ("content-type",)

_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

_SECRETS: List[str] = []

#: Anything that looks like a bearer token is scrubbed even if it was never
#: registered, so a token minted by a code path we did not anticipate still
#: cannot reach a log line.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def register_secret(value: Optional[str]) -> None:
    """Register a literal string that must never appear in output.

    Short values are ignored: redacting a 3 character string would mangle
    unrelated text and provide no real protection.
    """
    if not value or len(value) < 6:
        return
    if value not in _SECRETS:
        _SECRETS.append(value)


def forget_secrets() -> None:
    """Clear the registry. Used by tests; harmless in production."""
    _SECRETS.clear()


def redact(text: object) -> str:
    """Return ``text`` with every registered secret and bearer token removed."""
    out = text if isinstance(text, str) else str(text)
    # Longest first so a secret that contains another secret is fully covered.
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret and secret in out:
            out = out.replace(secret, REDACTED)
    out = _BEARER_RE.sub(lambda m: "%s %s" % (m.group(1), REDACTED), out)
    return out


def safe_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Copy of ``headers`` with sensitive values replaced, for logs and errors."""
    if not headers:
        return {}
    out: Dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADER_NAMES:
            out[key] = REDACTED
        else:
            out[key] = redact(value)
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs registered secrets from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


def install_redacting_filter(logger: logging.Logger) -> logging.Logger:
    """Attach :class:`RedactingFilter` to ``logger`` exactly once."""
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger


LOGGER = install_redacting_filter(logging.getLogger("gearwatch.http"))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GearwatchHttpError(Exception):
    """Base error for the request layer. Messages are redacted on construction."""

    def __init__(self, message: object = "", *args: object) -> None:
        super().__init__(redact(message), *[redact(a) for a in args])

    def __repr__(self) -> str:
        return "%s(%s)" % (type(self).__name__, redact(super().__str__()))


class RequestTimeout(GearwatchHttpError):
    """The request did not complete inside the configured timeout."""


class HttpStatusError(GearwatchHttpError):
    """Non-2xx response."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = int(status)
        self.url = redact(url)
        # Bodies are attacker/vendor controlled text. Truncate, then redact.
        self.body = redact(body or "")[:500]
        super().__init__("HTTP %d for %s: %s" % (self.status, self.url, self.body))


class RateLimitError(HttpStatusError):
    """HTTP 429. Carries the parsed Retry-After delay when the server sent one."""

    def __init__(
        self, status: int, url: str, body: str = "", retry_after: Optional[float] = None
    ) -> None:
        self.retry_after = retry_after
        super().__init__(status, url, body)


class RetryExhausted(GearwatchHttpError):
    """Ran out of retries. ``__cause__`` holds the final underlying error."""

    def __init__(self, attempts: int, url: str, last_error: BaseException) -> None:
        self.attempts = attempts
        self.url = redact(url)
        self.last_error = last_error
        super().__init__(
            "gave up after %d attempt(s) for %s: %s"
            % (attempts, self.url, type(last_error).__name__)
        )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TokenBucket:
    """Classic token bucket.

    ``clock`` and ``sleeper`` are injected so tests can drive it deterministically
    without spending real time. ``acquire`` returns the number of seconds it
    waited, which is what the tests assert on.
    """

    def __init__(
        self,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: float = DEFAULT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate_per_second = float(rate_per_second)
        self.burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleeper = sleeper
        self._last = clock()
        self.total_wait = 0.0
        self.acquisitions = 0

    def acquire(self, tokens: float = 1.0) -> float:
        waited = 0.0
        while True:
            now = self._clock()
            elapsed = max(0.0, now - self._last)
            self._last = now
            self._tokens = min(
                self.burst, self._tokens + elapsed * self.rate_per_second
            )
            if self._tokens >= tokens:
                self._tokens -= tokens
                self.total_wait += waited
                self.acquisitions += 1
                return waited
            deficit = tokens - self._tokens
            delay = deficit / self.rate_per_second
            self._sleeper(delay)
            waited += delay


# ---------------------------------------------------------------------------
# TTL disk cache
# ---------------------------------------------------------------------------


class DiskCache:
    """A dead simple content addressed response cache with a TTL.

    The cache key intentionally excludes headers, so no credential material is
    ever part of a filename, and a token refresh does not invalidate the cache.
    Only a whitelist of response headers is persisted.
    """

    def __init__(
        self,
        directory: str,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = directory
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key_for(method: str, url: str, body: Optional[bytes]) -> str:
        digest = hashlib.sha256()
        digest.update(method.upper().encode("utf-8"))
        digest.update(b"\n")
        digest.update(url.encode("utf-8"))
        digest.update(b"\n")
        digest.update(body or b"")
        return digest.hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, key + ".json")

    def get(self, key: str) -> Optional["HttpResponse"]:
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            self.misses += 1
            return None
        stored_at = float(payload.get("stored_at", 0.0))
        if self._clock() - stored_at > self.ttl_seconds:
            self.misses += 1
            try:
                os.remove(path)
            except OSError:  # pragma: no cover - best effort
                pass
            return None
        self.hits += 1
        return HttpResponse(
            status=int(payload.get("status", 200)),
            headers=dict(payload.get("headers", {})),
            body=base64.b64decode(payload.get("body_b64", "")),
            from_cache=True,
        )

    def put(self, key: str, response: "HttpResponse") -> None:
        os.makedirs(self.directory, exist_ok=True)
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in _CACHEABLE_RESPONSE_HEADERS
        }
        payload = {
            "stored_at": self._clock(),
            "status": response.status,
            "headers": headers,
            "body_b64": base64.b64encode(response.body).decode("ascii"),
        }
        tmp = self._path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self._path(key))

    def clear(self) -> int:
        removed = 0
        if not os.path.isdir(self.directory):
            return 0
        for name in os.listdir(self.directory):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(self.directory, name))
                    removed += 1
                except OSError:  # pragma: no cover - best effort
                    pass
        return removed


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    from_cache: bool = False

    def json(self) -> dict:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def _parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Parse a Retry-After header: delta-seconds or an HTTP-date."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # pragma: no cover - defensive
        return None
    reference = time.time() if now is None else now
    return max(0.0, when.timestamp() - reference)


def _urllib_opener(request: urllib.request.Request, timeout: float) -> HttpResponse:
    opener = urllib.request.build_opener()
    with opener.open(request, timeout=timeout) as raw:  # nosec - fixed https hosts
        return HttpResponse(
            status=int(getattr(raw, "status", 200) or 200),
            headers={k: v for k, v in raw.headers.items()},
            body=raw.read(),
        )


class HttpClient:
    """Rate limited, retrying, optionally caching HTTP client.

    ``opener`` is the single seam the tests use. It is a callable taking
    ``(urllib.request.Request, timeout)`` and returning an :class:`HttpResponse`
    or raising. Nothing else in gearwatch touches the network.
    """

    def __init__(
        self,
        opener: Optional[Callable[[urllib.request.Request, float], HttpResponse]] = None,
        limiter: Optional[TokenBucket] = None,
        cache: Optional[DiskCache] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        user_agent: str = "gearwatch/0.1 (+official-api-only)",
    ) -> None:
        self._opener = opener or _urllib_opener
        self.limiter = limiter if limiter is not None else TokenBucket()
        self.cache = cache
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._sleeper = sleeper
        self._rng = rng or random.Random()
        self.user_agent = user_agent
        #: Every backoff delay actually applied, for assertions and diagnostics.
        self.sleep_log: List[float] = []
        self.request_count = 0

    # -- internals ---------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        raw = self.backoff_base * (2 ** attempt)
        capped = min(self.backoff_cap, raw)
        # Equal jitter: half fixed, half random. Avoids a synchronised retry
        # stampede without ever waiting less than half the intended delay.
        return capped / 2.0 + self._rng.uniform(0.0, capped / 2.0)

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.sleep_log.append(seconds)
        self._sleeper(seconds)

    def _build_request(
        self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]
    ) -> urllib.request.Request:
        request = urllib.request.Request(url, data=body, method=method.upper())
        request.add_header("User-Agent", self.user_agent)
        for name, value in headers.items():
            request.add_header(name, value)
        return request

    def _perform(
        self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]
    ) -> HttpResponse:
        self.limiter.acquire()
        request = self._build_request(method, url, headers, body)
        self.request_count += 1
        LOGGER.debug(
            "%s %s headers=%s", method.upper(), url, safe_headers(headers)
        )
        try:
            response = self._opener(request, self.timeout)
        except urllib.error.HTTPError as exc:  # status carrying error
            try:
                payload = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                payload = ""
            status = int(getattr(exc, "code", 0) or 0)
            retry_after = _parse_retry_after(
                exc.headers.get("Retry-After") if exc.headers else None
            )
            if status == 429:
                raise RateLimitError(status, url, payload, retry_after) from None
            raise HttpStatusError(status, url, payload) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise RequestTimeout("timeout after %.1fs for %s" % (self.timeout, url)) from None
            raise GearwatchHttpError("transport error for %s: %s" % (url, reason)) from None
        except (socket.timeout, TimeoutError):
            raise RequestTimeout("timeout after %.1fs for %s" % (self.timeout, url)) from None

        if response.status == 429:
            retry_after = _parse_retry_after(response.header("Retry-After"))
            raise RateLimitError(
                response.status, url, response.body.decode("utf-8", "replace"), retry_after
            )
        if response.status >= 400:
            raise HttpStatusError(
                response.status, url, response.body.decode("utf-8", "replace")
            )
        return response

    # -- public API --------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        use_cache: bool = True,
    ) -> HttpResponse:
        headers = dict(headers or {})
        cache_key = DiskCache.key_for(method, url, body)
        if use_cache and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                LOGGER.debug("cache hit for %s", url)
                return cached

        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._perform(method, url, headers, body)
            except RateLimitError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = exc.retry_after
                if delay is None:
                    delay = self._backoff_delay(attempt)
                else:
                    delay = min(delay, self.backoff_cap)
                self._sleep(delay)
                continue
            except HttpStatusError as exc:
                last_error = exc
                if exc.status < 500 or attempt >= self.max_retries:
                    raise
                self._sleep(self._backoff_delay(attempt))
                continue
            except (RequestTimeout, GearwatchHttpError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                self._sleep(self._backoff_delay(attempt))
                continue

            if use_cache and self.cache is not None:
                self.cache.put(cache_key, response)
            return response

        assert last_error is not None  # loop always sets it before breaking
        if self.max_retries == 0:
            raise last_error
        raise RetryExhausted(self.max_retries + 1, url, last_error) from last_error

    def get_json(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        use_cache: bool = True,
    ) -> dict:
        return self.request("GET", url, headers=headers, use_cache=use_cache).json()

    def post_form(
        self,
        url: str,
        fields: Dict[str, str],
        headers: Optional[Dict[str, str]] = None,
    ) -> dict:
        """POST an application/x-www-form-urlencoded body. Never cached.

        Token endpoints must not be cached to disk: a cached token file is a
        credential at rest that nobody asked for.
        """
        import urllib.parse

        payload = urllib.parse.urlencode(fields).encode("utf-8")
        merged = dict(headers or {})
        merged.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return self.request(
            "POST", url, headers=merged, body=payload, use_cache=False
        ).json()


def join_query(base: str, params: Iterable[Tuple[str, str]]) -> str:
    """Build a URL with an encoded query string (no credentials ever go here)."""
    import urllib.parse

    encoded = urllib.parse.urlencode(list(params))
    if not encoded:
        return base
    separator = "&" if "?" in base else "?"
    return base + separator + encoded
