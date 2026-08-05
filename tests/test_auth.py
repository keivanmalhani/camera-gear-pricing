"""Tests for the OAuth2 client-credentials flow and, above all, for leakage.

The distinctive secret value below exists so that a leak is unmistakable: if
that string turns up anywhere in an exception, a repr, a traceback, or a log
line, these tests fail.
"""

from __future__ import annotations

import json
import logging
import traceback
import urllib.error
import urllib.request

import pytest

from conftest import FakeClock, RecordingOpener, json_response
from gearwatch import http as gw_http
from gearwatch.auth import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    AuthError,
    Credentials,
    MissingCredentialsError,
    Token,
    TokenProvider,
    credential_status,
)

SECRET = "PSTsWordSecret-canary-9c1f7b4e-DO-NOT-LEAK"
CLIENT_ID = "KeivanMa-gearwat-PRD-canary-3a7d21ff"
TOKEN_VALUE = "v^1.1#bearer#canary-token-6d2e88b0-DO-NOT-LEAK"

ENV = {CLIENT_ID_ENV: CLIENT_ID, CLIENT_SECRET_ENV: SECRET}


def token_payload(expires_in=7200, value=TOKEN_VALUE):
    return json.dumps(
        {"access_token": value, "expires_in": expires_in, "token_type": "Application"}
    )


def make_provider(script, clock=None, refresh_skew=60.0):
    clock = clock or FakeClock()
    opener = RecordingOpener(script)
    client = gw_http.HttpClient(
        opener=opener,
        limiter=gw_http.TokenBucket(1000.0, 1000.0, clock=clock, sleeper=lambda s: None),
        cache=None,
        max_retries=0,
        sleeper=lambda s: None,
    )
    credentials = Credentials.from_env(ENV)
    provider = TokenProvider(credentials, client, clock=clock, refresh_skew=refresh_skew)
    return provider, opener, clock


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------


def test_missing_both_variables_names_both():
    with pytest.raises(MissingCredentialsError) as excinfo:
        Credentials.from_env({})
    message = str(excinfo.value)
    assert CLIENT_ID_ENV in message
    assert CLIENT_SECRET_ENV in message
    assert excinfo.value.missing == (CLIENT_ID_ENV, CLIENT_SECRET_ENV)


def test_missing_only_the_secret_names_only_the_secret_variable():
    with pytest.raises(MissingCredentialsError) as excinfo:
        Credentials.from_env({CLIENT_ID_ENV: CLIENT_ID})
    message = str(excinfo.value)
    assert CLIENT_SECRET_ENV in message
    assert excinfo.value.missing == (CLIENT_SECRET_ENV,)
    # It names the variable. It must not name the value of the one that is set.
    assert CLIENT_ID not in message


def test_blank_values_count_as_missing():
    ok, missing = credential_status({CLIENT_ID_ENV: "   ", CLIENT_SECRET_ENV: SECRET})
    assert ok is False
    assert missing == [CLIENT_ID_ENV]


def test_credential_status_reports_presence_only():
    ok, missing = credential_status(ENV)
    assert ok is True
    assert missing == []


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_credentials_repr_and_str_never_contain_the_values():
    credentials = Credentials.from_env(ENV)
    for rendered in (repr(credentials), str(credentials), "%s" % (credentials,)):
        assert SECRET not in rendered
        assert CLIENT_ID not in rendered
        assert "redacted" in rendered
    # ...and neither does an f-string interpolation of the dataclass.
    assert SECRET not in f"{credentials!r} {credentials}"


def test_token_repr_never_contains_the_token():
    token = Token(value=TOKEN_VALUE, expires_at=123.0)
    assert TOKEN_VALUE not in repr(token)
    assert TOKEN_VALUE not in str(token)
    assert "redacted" in repr(token)


def test_the_secret_cannot_appear_in_a_formatted_exception():
    """Deliberately trigger a failure while the secret is live.

    The simulated server echoes the submitted credentials back in its error
    body, which is exactly the real-world shape that leaks secrets into logs
    and bug reports.
    """
    credentials = Credentials.from_env(ENV)
    body = json.dumps(
        {
            "error": "invalid_client",
            "error_description": "client_secret=%s basic=%s" % (
                SECRET,
                credentials.basic_credential(),
            ),
        }
    ).encode("utf-8")

    class EchoingOpener:
        calls = 0

        def __call__(self, request, timeout):
            EchoingOpener.calls += 1
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=__import__("io").BytesIO(body),
            )

    client = gw_http.HttpClient(
        opener=EchoingOpener(),
        limiter=gw_http.TokenBucket(1000.0, 1000.0, sleeper=lambda s: None),
        cache=None,
        max_retries=0,
        sleeper=lambda s: None,
    )
    provider = TokenProvider(credentials, client, clock=FakeClock())

    with pytest.raises(AuthError) as excinfo:
        provider.get_token()

    formatted = "%s|%r|%s" % (
        excinfo.value,
        excinfo.value,
        "".join(traceback.format_exception(excinfo.value)),
    )
    assert SECRET not in formatted
    assert CLIENT_ID not in formatted
    assert credentials.basic_credential() not in formatted
    assert "[redacted]" in formatted
    # The error still says something useful.
    assert "invalid_client" in formatted


def test_the_token_cannot_appear_in_a_log_line():
    logger = logging.getLogger("gearwatch.test.leak")
    gw_http.install_redacting_filter(logger)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        gw_http.register_secret(TOKEN_VALUE)
        logger.info("about to call with Authorization: Bearer %s", TOKEN_VALUE)
        logger.info("raw token %s in a message", TOKEN_VALUE)
    finally:
        logger.removeHandler(handler)

    assert records, "the capturing handler never fired"
    for line in records:
        assert TOKEN_VALUE not in line
        assert "[redacted]" in line


def test_safe_headers_masks_authorization():
    masked = gw_http.safe_headers(
        {"Authorization": "Bearer %s" % TOKEN_VALUE, "Accept": "application/json"}
    )
    assert masked["Authorization"] == gw_http.REDACTED
    assert masked["Accept"] == "application/json"
    assert TOKEN_VALUE not in json.dumps(masked)


# ---------------------------------------------------------------------------
# Caching and refresh
# ---------------------------------------------------------------------------


def test_token_is_fetched_once_and_reused():
    provider, opener, _clock = make_provider([json_response(token_payload())])
    first = provider.get_token()
    second = provider.get_token()
    assert first is second
    assert provider.fetch_count == 1
    assert opener.call_count == 1


def test_token_within_sixty_seconds_of_expiry_is_refreshed():
    clock = FakeClock(start=1000.0)
    provider, opener, _clock = make_provider(
        [json_response(token_payload(expires_in=100))], clock=clock
    )
    first = provider.get_token()
    assert provider.fetch_count == 1
    assert first.expires_at == 1100.0

    # 39 seconds later there are 61 seconds of life left: still fine.
    clock.advance(39)
    provider.get_token()
    assert provider.fetch_count == 1

    # One more second and we are inside the 60 second skew: refresh.
    clock.advance(1)
    provider.get_token()
    assert provider.fetch_count == 2
    assert opener.call_count == 2


def test_expired_token_is_refreshed():
    clock = FakeClock(start=1000.0)
    provider, _opener, _clock = make_provider(
        [json_response(token_payload(expires_in=60))], clock=clock
    )
    provider.get_token()
    clock.advance(10_000)
    provider.get_token()
    assert provider.fetch_count == 2


def test_needs_refresh_boundary_is_inclusive():
    token = Token(value=TOKEN_VALUE, expires_at=1000.0)
    assert token.needs_refresh(now=940.0) is True    # exactly 60 seconds left
    assert token.needs_refresh(now=939.0) is False   # 61 seconds left
    assert token.seconds_remaining(900.0) == 100.0


def test_invalidate_forces_a_new_fetch():
    provider, _opener, _clock = make_provider([json_response(token_payload())])
    provider.get_token()
    provider.invalidate()
    assert provider.cached_token is None
    provider.get_token()
    assert provider.fetch_count == 2


def test_authorization_header_is_a_bearer_header():
    provider, _opener, _clock = make_provider([json_response(token_payload())])
    header = provider.authorization_header()
    assert header["Authorization"] == "Application %s" % TOKEN_VALUE


def test_client_credentials_grant_is_posted_correctly():
    provider, opener, _clock = make_provider([json_response(token_payload())])
    provider.get_token()
    request = opener.calls[0]
    assert request.get_method() == "POST"
    body = request.data.decode("utf-8")
    assert "grant_type=client_credentials" in body
    assert "scope=" in body
    # The secret goes in the Basic header, never in the body or the URL.
    assert SECRET not in body
    assert SECRET not in request.full_url
    assert request.get_header("Authorization").startswith("Basic ")


def test_malformed_token_responses_raise_auth_errors():
    provider, _opener, _clock = make_provider([json_response(json.dumps({}))])
    with pytest.raises(AuthError) as excinfo:
        provider.get_token()
    assert "no access_token" in str(excinfo.value)

    provider2, _o2, _c2 = make_provider(
        [json_response(json.dumps({"access_token": "abc", "expires_in": 0}))]
    )
    with pytest.raises(AuthError) as excinfo2:
        provider2.get_token()
    assert "expires_in" in str(excinfo2.value)


def test_provider_repr_is_boring():
    provider, _opener, _clock = make_provider([json_response(token_payload())])
    assert "canary" not in repr(provider)
    assert "TokenProvider" in repr(provider)
