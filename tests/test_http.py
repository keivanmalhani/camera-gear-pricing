"""Tests for the request layer.

Nothing here sleeps for real: the limiter, the backoff, and the cache all take
injected clocks and sleepers.
"""

from __future__ import annotations

import io
import json
import os
import random
import socket
import urllib.error

import pytest

from conftest import FakeClock, FakeSleeper, RecordingOpener, json_response
from gearwatch import http as gw_http

URL = "https://api.ebay.com/buy/browse/v1/item_summary/search?q=test"


def make_client(script, clock=None, sleeper=None, cache=None, max_retries=4, timeout=20.0):
    clock = clock or FakeClock()
    sleeper = sleeper if sleeper is not None else FakeSleeper(clock)
    opener = RecordingOpener(script)
    client = gw_http.HttpClient(
        opener=opener,
        limiter=gw_http.TokenBucket(1000.0, 1000.0, clock=clock, sleeper=sleeper),
        cache=cache,
        max_retries=max_retries,
        timeout=timeout,
        sleeper=sleeper,
        rng=random.Random(1234),
    )
    return client, opener, clock, sleeper


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


def test_token_bucket_spaces_calls_using_the_injected_clock():
    clock = FakeClock(start=0.0)
    sleeper = FakeSleeper(clock)
    bucket = gw_http.TokenBucket(
        rate_per_second=2.0, burst=1.0, clock=clock, sleeper=sleeper
    )
    assert bucket.acquire() == 0.0            # the burst token
    assert bucket.acquire() == pytest.approx(0.5)   # refill at 2/s
    assert bucket.acquire() == pytest.approx(0.5)
    assert sleeper.calls == [pytest.approx(0.5), pytest.approx(0.5)]
    assert clock.now == pytest.approx(1.0)
    assert bucket.acquisitions == 3
    assert bucket.total_wait == pytest.approx(1.0)


def test_token_bucket_allows_a_burst_then_throttles():
    clock = FakeClock(start=0.0)
    sleeper = FakeSleeper(clock)
    bucket = gw_http.TokenBucket(1.0, 3.0, clock=clock, sleeper=sleeper)
    assert [bucket.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.acquire() == pytest.approx(1.0)
    assert sleeper.calls == [pytest.approx(1.0)]


def test_token_bucket_refills_over_idle_time():
    clock = FakeClock(start=0.0)
    sleeper = FakeSleeper(clock)
    bucket = gw_http.TokenBucket(1.0, 2.0, clock=clock, sleeper=sleeper)
    bucket.acquire()
    bucket.acquire()
    clock.advance(5.0)          # plenty of idle time, capped at burst
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == pytest.approx(1.0)


def test_token_bucket_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        gw_http.TokenBucket(rate_per_second=0.0)
    with pytest.raises(ValueError):
        gw_http.TokenBucket(rate_per_second=1.0, burst=0.5)


def test_default_rate_is_conservative():
    assert gw_http.DEFAULT_RATE_PER_SECOND <= 2.0
    assert gw_http.DEFAULT_BURST <= 5.0


# ---------------------------------------------------------------------------
# Retries and backoff
# ---------------------------------------------------------------------------


def test_429_with_retry_after_is_honoured_and_retried():
    client, opener, _clock, sleeper = make_client(
        [
            gw_http.HttpResponse(status=429, headers={"Retry-After": "7"}, body=b"slow down"),
            json_response('{"ok": true}'),
        ]
    )
    payload = client.get_json(URL, use_cache=False)
    assert payload == {"ok": True}
    assert opener.call_count == 2
    assert client.sleep_log == [pytest.approx(7.0)]


def test_retry_after_is_capped_at_the_backoff_ceiling():
    client, _opener, _clock, _sleeper = make_client(
        [
            gw_http.HttpResponse(status=429, headers={"Retry-After": "9999"}, body=b""),
            json_response("{}"),
        ]
    )
    client.get_json(URL, use_cache=False)
    assert client.sleep_log == [pytest.approx(gw_http.DEFAULT_BACKOFF_CAP_SECONDS)]


def test_429_without_retry_after_uses_jittered_backoff():
    client, _opener, _clock, _sleeper = make_client(
        [
            gw_http.HttpResponse(status=429, headers={}, body=b""),
            gw_http.HttpResponse(status=429, headers={}, body=b""),
            json_response("{}"),
        ]
    )
    client.get_json(URL, use_cache=False)
    assert len(client.sleep_log) == 2
    # Equal jitter: never below half the nominal delay, never above it.
    assert 0.25 <= client.sleep_log[0] <= 0.5
    assert 0.5 <= client.sleep_log[1] <= 1.0
    assert client.sleep_log[1] > client.sleep_log[0]


def test_retries_give_up_after_the_cap_and_raise():
    client, opener, _clock, _sleeper = make_client(
        [gw_http.HttpResponse(status=429, headers={"Retry-After": "1"}, body=b"nope")],
        max_retries=3,
    )
    with pytest.raises(gw_http.RetryExhausted) as excinfo:
        client.get_json(URL, use_cache=False)
    assert opener.call_count == 4               # the first try plus 3 retries
    assert excinfo.value.attempts == 4
    assert isinstance(excinfo.value.__cause__, gw_http.RateLimitError)
    assert excinfo.value.__cause__.status == 429


def test_server_errors_are_retried_but_client_errors_are_not():
    client, opener, _clock, _sleeper = make_client(
        [
            gw_http.HttpResponse(status=503, headers={}, body=b"maintenance"),
            gw_http.HttpResponse(status=503, headers={}, body=b"maintenance"),
            json_response('{"ok": 1}'),
        ]
    )
    assert client.get_json(URL, use_cache=False) == {"ok": 1}
    assert opener.call_count == 3

    client2, opener2, _c2, _s2 = make_client(
        [gw_http.HttpResponse(status=404, headers={}, body=b"gone")]
    )
    with pytest.raises(gw_http.HttpStatusError) as excinfo:
        client2.get_json(URL, use_cache=False)
    assert excinfo.value.status == 404
    assert opener2.call_count == 1


def test_a_timeout_raises_the_timeout_error_type():
    client, opener, _clock, _sleeper = make_client([socket.timeout()], max_retries=0)
    with pytest.raises(gw_http.RequestTimeout):
        client.get_json(URL, use_cache=False)
    assert opener.call_count == 1


def test_a_timeout_wrapped_in_urlerror_is_also_a_timeout():
    client, _opener, _clock, _sleeper = make_client(
        [urllib.error.URLError(socket.timeout())], max_retries=0
    )
    with pytest.raises(gw_http.RequestTimeout):
        client.get_json(URL, use_cache=False)


def test_repeated_timeouts_exhaust_retries():
    client, opener, _clock, _sleeper = make_client([socket.timeout()], max_retries=2)
    with pytest.raises(gw_http.RetryExhausted) as excinfo:
        client.get_json(URL, use_cache=False)
    assert opener.call_count == 3
    assert isinstance(excinfo.value.__cause__, gw_http.RequestTimeout)


def test_transport_errors_are_wrapped():
    client, _opener, _clock, _sleeper = make_client(
        [urllib.error.URLError("name or service not known")], max_retries=0
    )
    with pytest.raises(gw_http.GearwatchHttpError):
        client.get_json(URL, use_cache=False)


def test_http_error_from_urllib_becomes_a_status_error():
    error = urllib.error.HTTPError(
        url=URL, code=401, msg="Unauthorized", hdrs={}, fp=io.BytesIO(b"bad token")
    )
    client, _opener, _clock, _sleeper = make_client([error], max_retries=0)
    with pytest.raises(gw_http.HttpStatusError) as excinfo:
        client.get_json(URL, use_cache=False)
    assert excinfo.value.status == 401
    assert "bad token" in str(excinfo.value)


def test_retry_after_http_date_is_parsed():
    seconds = gw_http._parse_retry_after(
        "Wed, 05 Aug 2026 00:01:00 GMT", now=1785888000.0
    )
    assert seconds is not None and seconds >= 0.0
    assert gw_http._parse_retry_after(None) is None
    assert gw_http._parse_retry_after("not a date") is None
    assert gw_http._parse_retry_after("12") == 12.0


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def test_ttl_cache_returns_the_body_without_a_second_call(tmp_path):
    clock = FakeClock(start=0.0)
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0, clock=clock)
    client, opener, _clock, _sleeper = make_client(
        [json_response('{"itemSummaries": []}')], clock=clock, cache=cache
    )
    first = client.get_json(URL)
    second = client.get_json(URL)
    assert first == second == {"itemSummaries": []}
    assert opener.call_count == 1
    assert cache.hits == 1


def test_cache_entries_expire_after_the_ttl(tmp_path):
    clock = FakeClock(start=0.0)
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0, clock=clock)
    client, opener, _clock, _sleeper = make_client(
        [json_response('{"a": 1}'), json_response('{"a": 2}')], clock=clock, cache=cache
    )
    assert client.get_json(URL) == {"a": 1}
    clock.advance(101.0)
    assert client.get_json(URL) == {"a": 2}
    assert opener.call_count == 2


def test_cache_key_ignores_headers_so_a_token_never_lands_on_disk(tmp_path):
    token = "bearer-value-that-must-not-be-persisted-4417"
    clock = FakeClock(start=0.0)
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0, clock=clock)
    client, opener, _clock, _sleeper = make_client(
        [json_response('{"a": 1}')], clock=clock, cache=cache
    )
    client.get_json(URL, headers={"Authorization": "Bearer %s" % token})
    client.get_json(URL, headers={"Authorization": "Bearer refreshed-%s" % token})
    assert opener.call_count == 1, "a header change must not invalidate the cache"

    directory = str(tmp_path / "cache")
    names = os.listdir(directory)
    assert names
    for name in names:
        with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
            blob = handle.read()
        assert token not in blob
        assert "Authorization" not in blob
        assert "authorization" not in blob


def test_cache_only_persists_whitelisted_response_headers(tmp_path):
    clock = FakeClock(start=0.0)
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0, clock=clock)
    client, _opener, _clock, _sleeper = make_client(
        [
            gw_http.HttpResponse(
                status=200,
                headers={
                    "Content-Type": "application/json",
                    "Set-Cookie": "session=deadbeef",
                },
                body=b"{}",
            )
        ],
        clock=clock,
        cache=cache,
    )
    client.get_json(URL)
    directory = str(tmp_path / "cache")
    blob = ""
    for name in os.listdir(directory):
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            blob += handle.read()
    assert "deadbeef" not in blob
    assert "application/json" in blob


def test_cache_key_is_stable_and_body_sensitive():
    a = gw_http.DiskCache.key_for("GET", URL, None)
    b = gw_http.DiskCache.key_for("GET", URL, None)
    c = gw_http.DiskCache.key_for("GET", URL + "&offset=50", None)
    d = gw_http.DiskCache.key_for("POST", URL, b"body")
    assert a == b
    assert a != c != d
    assert len(a) == 64


def test_cache_clear_removes_entries(tmp_path):
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0)
    cache.put("abc", gw_http.HttpResponse(status=200, headers={}, body=b"{}"))
    assert cache.clear() == 1
    assert cache.get("abc") is None


def test_corrupt_cache_entries_are_treated_as_misses(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    (directory / ("%s.json" % ("a" * 64))).write_text("not json", encoding="utf-8")
    cache = gw_http.DiskCache(str(directory), ttl_seconds=100.0)
    assert cache.get("a" * 64) is None
    assert cache.misses == 1


def test_default_ttl_is_documented_and_sane():
    assert gw_http.DEFAULT_CACHE_TTL_SECONDS == 6 * 60 * 60


# ---------------------------------------------------------------------------
# Redaction primitives
# ---------------------------------------------------------------------------


def test_redact_replaces_registered_secrets_and_bearer_headers():
    gw_http.register_secret("hunter2-hunter2-hunter2")
    text = "auth failed for hunter2-hunter2-hunter2 with Bearer abcdefgh12345678"
    scrubbed = gw_http.redact(text)
    assert "hunter2" not in scrubbed
    assert "abcdefgh12345678" not in scrubbed
    assert scrubbed.count(gw_http.REDACTED) == 2


def test_short_values_are_not_registered():
    gw_http.register_secret("abc")
    gw_http.register_secret("")
    gw_http.register_secret(None)
    assert gw_http.redact("abc is fine") == "abc is fine"


def test_errors_redact_at_construction_time():
    gw_http.register_secret("supersecretvalue123")
    error = gw_http.GearwatchHttpError("boom supersecretvalue123")
    assert "supersecretvalue123" not in str(error)
    assert "supersecretvalue123" not in repr(error)
    assert "supersecretvalue123" not in str(error.args)


def test_post_form_is_never_cached(tmp_path):
    clock = FakeClock(start=0.0)
    cache = gw_http.DiskCache(str(tmp_path / "cache"), ttl_seconds=100.0, clock=clock)
    client, opener, _clock, _sleeper = make_client(
        [json_response(json.dumps({"access_token": "x", "expires_in": 1}))],
        clock=clock,
        cache=cache,
    )
    client.post_form("https://api.ebay.com/identity/v1/oauth2/token", {"a": "b"})
    client.post_form("https://api.ebay.com/identity/v1/oauth2/token", {"a": "b"})
    assert opener.call_count == 2
    assert not os.path.isdir(str(tmp_path / "cache")) or not os.listdir(
        str(tmp_path / "cache")
    )


def test_join_query_encodes_parameters():
    url = gw_http.join_query("https://example.test/x", [("q", "35mm f/1.4"), ("limit", "50")])
    assert "q=35mm+f%2F1.4" in url
    assert url.startswith("https://example.test/x?")
    assert gw_http.join_query("https://example.test/x", []) == "https://example.test/x"
