"""OAuth2 client-credentials flow for eBay application tokens.

Rules this module enforces, and which the test suite checks:

1. Credentials come from environment variables only. There is no flag, no
   config file, and no database column that can supply them.
2. ``Credentials`` and ``Token`` have custom ``__repr__``/``__str__`` that emit
   a placeholder. A stray ``print(creds)`` or a dataclass repr in a traceback
   cannot leak them.
3. Both the secret and every minted access token are handed to
   :func:`gearwatch.http.register_secret`, so even if some other layer places
   them in a message, the redactor removes them before the string reaches an
   exception or a log record.
4. A token is reused until it is within ``refresh_skew`` seconds (default 60) of
   expiring, then refreshed.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .http import (
    GearwatchHttpError,
    HttpClient,
    redact,
    register_secret,
)

__all__ = [
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "MissingCredentialsError",
    "AuthError",
    "Credentials",
    "Token",
    "TokenProvider",
    "credential_status",
    "EBAY_OAUTH_ENDPOINT",
    "EBAY_SANDBOX_OAUTH_ENDPOINT",
    "DEFAULT_SCOPES",
    "DEFAULT_REFRESH_SKEW_SECONDS",
]

CLIENT_ID_ENV = "EBAY_CLIENT_ID"
CLIENT_SECRET_ENV = "EBAY_CLIENT_SECRET"

EBAY_OAUTH_ENDPOINT = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SANDBOX_OAUTH_ENDPOINT = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

DEFAULT_SCOPES: Tuple[str, ...] = ("https://api.ebay.com/oauth/api_scope",)

#: Refresh when the cached token has less than this many seconds of life left.
DEFAULT_REFRESH_SKEW_SECONDS = 60.0

_PLACEHOLDER = "<redacted>"


class AuthError(Exception):
    """Authentication failed. Message is redacted at construction time."""

    def __init__(self, message: object = "") -> None:
        super().__init__(redact(message))

    def __repr__(self) -> str:
        return "%s(%s)" % (type(self).__name__, redact(super().__str__()))


class MissingCredentialsError(AuthError):
    """A required environment variable is absent or empty.

    The message names the variable. It never contains, hints at, or partially
    reveals a value.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing: Tuple[str, ...] = tuple(missing)
        joined = ", ".join(self.missing)
        super().__init__(
            "missing required environment variable(s): %s. "
            "Export them in your shell; gearwatch never accepts credentials "
            "from flags, files, or the database." % joined
        )


@dataclass(frozen=True)
class Credentials:
    """An eBay application client id and secret.

    Both fields are ``repr=False`` and the dataclass supplies its own
    ``__repr__``, so neither value can appear in a default dataclass repr, an
    f-string, or a pytest assertion diff.
    """

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        register_secret(self.client_secret)
        register_secret(self.client_id)
        register_secret(self.basic_credential())

    def __repr__(self) -> str:
        return "Credentials(client_id=%s, client_secret=%s)" % (
            _PLACEHOLDER,
            _PLACEHOLDER,
        )

    __str__ = __repr__

    def basic_credential(self) -> str:
        raw = "%s:%s" % (self.client_id, self.client_secret)
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def basic_auth_header(self) -> Dict[str, str]:
        return {"Authorization": "Basic %s" % self.basic_credential()}

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Credentials":
        source = os.environ if env is None else env
        missing = [
            name
            for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV)
            if not (source.get(name) or "").strip()
        ]
        if missing:
            raise MissingCredentialsError(missing)
        return cls(
            client_id=(source.get(CLIENT_ID_ENV) or "").strip(),
            client_secret=(source.get(CLIENT_SECRET_ENV) or "").strip(),
        )


def credential_status(env: Optional[Mapping[str, str]] = None) -> Tuple[bool, List[str]]:
    """Return ``(ok, missing_variable_names)`` without reading any value.

    This backs ``gearwatch auth check``. It reports presence only; the values
    are never returned, printed, hashed, or length-disclosed.
    """
    source = os.environ if env is None else env
    missing = [
        name
        for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV)
        if not (source.get(name) or "").strip()
    ]
    return (not missing, missing)


@dataclass(frozen=True)
class Token:
    """A bearer token plus the absolute epoch time at which it expires."""

    value: str = field(repr=False)
    expires_at: float = 0.0
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        register_secret(self.value)

    def __repr__(self) -> str:
        return "Token(value=%s, expires_at=%.0f)" % (_PLACEHOLDER, self.expires_at)

    __str__ = __repr__

    def seconds_remaining(self, now: float) -> float:
        return self.expires_at - now

    def needs_refresh(self, now: float, skew: float = DEFAULT_REFRESH_SKEW_SECONDS) -> bool:
        """True when the token is expired or within ``skew`` seconds of expiring."""
        return self.seconds_remaining(now) <= skew

    def header(self) -> Dict[str, str]:
        return {"Authorization": "%s %s" % (self.token_type, self.value)}


class TokenProvider:
    """Fetches, caches, and refreshes an application access token.

    The cached token lives in memory for the life of the process only. It is
    never written to the database, never written to the HTTP disk cache (the
    token endpoint is POSTed with caching disabled), and never logged.
    """

    def __init__(
        self,
        credentials: Credentials,
        client: HttpClient,
        endpoint: str = EBAY_OAUTH_ENDPOINT,
        scopes: Iterable[str] = DEFAULT_SCOPES,
        clock: Callable[[], float] = time.time,
        refresh_skew: float = DEFAULT_REFRESH_SKEW_SECONDS,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self.endpoint = endpoint
        self.scopes = tuple(scopes)
        self._clock = clock
        self.refresh_skew = float(refresh_skew)
        self._token: Optional[Token] = None
        #: Number of times we actually hit the identity endpoint. Tests assert
        #: on this to prove caching and refresh behaviour.
        self.fetch_count = 0

    def __repr__(self) -> str:
        return "TokenProvider(endpoint=%r, cached=%s)" % (
            self.endpoint,
            self._token is not None,
        )

    @property
    def cached_token(self) -> Optional[Token]:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def get_token(self) -> Token:
        now = self._clock()
        token = self._token
        if token is not None and not token.needs_refresh(now, self.refresh_skew):
            return token
        self._token = self._fetch()
        return self._token

    def authorization_header(self) -> Dict[str, str]:
        return self.get_token().header()

    def _fetch(self) -> Token:
        fields = {
            "grant_type": "client_credentials",
            "scope": " ".join(self.scopes),
        }
        headers = self._credentials.basic_auth_header()
        try:
            payload = self._client.post_form(self.endpoint, fields, headers=headers)
        except GearwatchHttpError as exc:
            # str(exc) is already redacted by the http layer; wrap it so callers
            # only ever see an AuthError and never an object holding a header.
            raise AuthError(
                "token request to %s failed: %s" % (self.endpoint, exc)
            ) from None
        self.fetch_count += 1

        access_token = payload.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise AuthError(
                "identity endpoint %s returned no access_token "
                "(keys seen: %s)" % (self.endpoint, sorted(payload.keys()))
            )
        try:
            expires_in = float(payload.get("expires_in", 0) or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in <= 0:
            raise AuthError(
                "identity endpoint %s returned a non-positive expires_in" % self.endpoint
            )
        return Token(
            value=access_token,
            expires_at=self._clock() + expires_in,
            token_type=str(payload.get("token_type") or "Bearer"),
        )
