"""Shared test scaffolding.

Two guarantees are established here for the whole suite:

* ``no_network`` is autouse and replaces the socket constructor, so any test
  that accidentally reaches for the network fails loudly instead of quietly
  succeeding on a developer machine and hanging in CI.
* ``clean_secret_registry`` is autouse and empties the redaction registry
  around every test, so redaction assertions cannot pass because of leftover
  state from an earlier test.
"""

from __future__ import annotations

import os
import socket
import urllib.request
from typing import Dict, List, Optional, Sequence

import pytest

from gearwatch import http as gw_http
from gearwatch.models import Condition, Listing, SoldComp

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "demo.json"
)


@pytest.fixture(autouse=True)
def clean_secret_registry():
    gw_http.forget_secrets()
    yield
    gw_http.forget_secrets()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make any real outbound socket an immediate, obvious failure."""

    def _boom(*args, **kwargs):
        raise RuntimeError("the gearwatch test suite must never touch the network")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    yield


class FakeClock:
    """Monotonic-ish clock the tests drive by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeSleeper:
    """A sleeper that advances a :class:`FakeClock` instead of blocking."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        self.clock.advance(seconds)


class RecordingOpener:
    """A scripted stand-in for the single network seam in HttpClient.

    ``script`` is a list of either :class:`gearwatch.http.HttpResponse` objects
    or exceptions. Each call pops the next entry; the last entry repeats.
    """

    def __init__(self, script: Sequence[object]) -> None:
        self.script = list(script)
        self.calls: List[urllib.request.Request] = []

    def __call__(self, request, timeout):  # noqa: D401 - callable seam
        self.calls.append(request)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        item = self.script[index]
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, gw_http.HttpResponse):
            return item(request, timeout)
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def header_of(self, index: int, name: str) -> Optional[str]:
        return self.calls[index].get_header(name.capitalize())


def json_response(payload: str, status: int = 200, headers: Optional[Dict] = None):
    return gw_http.HttpResponse(
        status=status,
        headers=headers or {"Content-Type": "application/json"},
        body=payload.encode("utf-8"),
    )


def make_comps(
    prices: Sequence[float],
    condition: Condition = Condition.EXCELLENT,
    currency: str = "USD",
    start_day: int = 1,
) -> List[SoldComp]:
    """Comps with strictly increasing sale dates, one per day."""
    out: List[SoldComp] = []
    for index, price in enumerate(prices):
        day = start_day + index
        month = 1 + (day - 1) // 28
        dom = 1 + (day - 1) % 28
        out.append(
            SoldComp(
                item_id="item-%03d" % index,
                title="test comp %d" % index,
                price=float(price),
                currency=currency,
                condition=condition,
                sold_at="2026-%02d-%02dT12:00:00Z" % (month, dom),
                marketplace="EBAY_US",
                condition_id="3000",
            )
        )
    return out


def make_listing(
    price: float,
    item_id: str = "live-1",
    currency: str = "USD",
    condition: Condition = Condition.EXCELLENT,
    title: str = "test listing",
) -> Listing:
    return Listing(
        item_id=item_id,
        title=title,
        price=float(price),
        currency=currency,
        condition=condition,
        seller="tester",
        listed_at="2026-08-01T00:00:00Z",
        marketplace="EBAY_US",
    )


@pytest.fixture
def fixture_path() -> str:
    assert os.path.exists(FIXTURE_PATH), "demo fixture is missing"
    return FIXTURE_PATH


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeper(clock: FakeClock) -> FakeSleeper:
    return FakeSleeper(clock)
