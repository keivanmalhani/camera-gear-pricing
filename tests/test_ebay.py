"""Tests for the eBay adapter and the fixture source."""

from __future__ import annotations

import json

import pytest

from conftest import FakeClock, RecordingOpener, json_response
from gearwatch import ebay, match
from gearwatch.auth import Credentials, TokenProvider
from gearwatch.http import HttpClient, TokenBucket
from gearwatch.models import Condition, Watch


def make_watch(query="Sony FE 35mm f/1.4 GM", require=("gm",), currency="USD",
               condition=Condition.EXCELLENT, exclude=()):
    required, optional = match.derive_tokens(query, require)
    return Watch(
        query=query,
        max_price=900.0,
        condition=condition,
        currency=currency,
        marketplace="EBAY_US",
        required_tokens=required,
        optional_tokens=optional,
        excluded_tokens=tuple(exclude),
        model_key=match.model_key(query),
        id=1,
    )


# ---------------------------------------------------------------------------
# Condition mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "condition_id, expected",
    [
        ("1000", Condition.NEW),
        ("2750", Condition.LIKE_NEW),
        ("3000", Condition.EXCELLENT),
        ("4000", Condition.GOOD),
        ("5000", Condition.GOOD),
        ("6000", Condition.FAIR),
        ("7000", Condition.PARTS),
        (3000, Condition.EXCELLENT),
    ],
)
def test_condition_ids_map_to_the_internal_vocabulary(condition_id, expected):
    assert ebay.map_condition(condition_id) is expected


@pytest.mark.parametrize("condition_id", ["9999", "", None, "not-a-number", {}])
def test_unknown_condition_ids_map_to_a_safe_default_rather_than_crashing(condition_id):
    assert ebay.map_condition(condition_id) is Condition.UNKNOWN


def test_parse_money():
    assert ebay.parse_money({"value": "899.00", "currency": "usd"}) == (899.0, "USD")
    assert ebay.parse_money({"value": "x", "currency": "USD"}) == (None, "USD")
    assert ebay.parse_money(None) == (None, "")
    assert ebay.parse_money({"currency": "USD"}) == (None, "USD")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_sold_node_normalizes_into_a_sold_comp():
    node = {
        "itemId": "v1|123|0",
        "title": "Sony FE 35mm f/1.4 GM",
        "lastSoldPrice": {"value": "899.00", "currency": "USD"},
        "lastSoldDate": "2026-06-15T10:00:00.000Z",
        "conditionId": "3000",
        "itemWebUrl": "https://www.ebay.com/itm/123",
    }
    comp = ebay.normalize_sold(node, "EBAY_US")
    assert comp is not None
    assert comp.item_id == "v1|123|0"
    assert comp.price == 899.0
    assert comp.currency == "USD"
    assert comp.condition is Condition.EXCELLENT
    assert comp.condition_id == "3000"
    assert comp.sold_at == "2026-06-15T10:00:00Z"      # milliseconds trimmed
    assert comp.marketplace == "EBAY_US"


def test_live_node_normalizes_into_a_listing():
    node = {
        "itemId": "v1|456|0",
        "title": "Sony FE 35mm f/1.4 GM",
        "price": {"value": "849.00", "currency": "USD"},
        "conditionId": "3000",
        "seller": {"username": "camerashop_pdx"},
        "itemCreationDate": "2026-08-01T09:11:00.000Z",
        "itemWebUrl": "https://www.ebay.com/itm/456",
    }
    listing = ebay.normalize_listing(node, "EBAY_US")
    assert listing is not None
    assert listing.price == 849.0
    assert listing.seller == "camerashop_pdx"
    assert listing.active is True


@pytest.mark.parametrize(
    "node",
    [
        {},
        {"itemId": "v1|1|0"},
        {"itemId": "", "lastSoldPrice": {"value": "1", "currency": "USD"}},
        {"itemId": "v1|1|0", "lastSoldPrice": {"value": "0", "currency": "USD"}},
        {"itemId": "v1|1|0", "lastSoldPrice": {"value": "10", "currency": ""}},
    ],
)
def test_unusable_nodes_are_dropped_not_crashed_on(node):
    assert ebay.normalize_sold(node) is None


# ---------------------------------------------------------------------------
# Fixture source
# ---------------------------------------------------------------------------


def test_fixture_source_produces_the_expected_dataclasses(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch()
    comps, pages, cap_hit = source.sold_comps(watch, days=90, max_pages=5)
    assert pages == 2                       # 12 + 10 across two pages
    assert cap_hit is False
    assert len(comps) == 22
    assert all(c.item_id.startswith("v1|") for c in comps)
    assert {c.currency for c in comps} == {"USD", "EUR"}
    assert Condition.PARTS in {c.condition for c in comps}


def test_fixture_pagination_stops_at_the_cap(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch()
    comps, pages, cap_hit = source.sold_comps(watch, days=90, max_pages=1)
    assert pages == 1
    assert cap_hit is True
    assert len(comps) == 12                 # only the first page
    assert len(source.page_requests_for("sold")) == 1


def test_fixture_source_reports_its_snapshot_time(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    assert source.reference_time == "2026-08-05T00:00:00Z"
    assert source.name == "fixture"
    assert "FixtureSource" in repr(source)


def test_fixture_day_window_is_relative_to_the_snapshot_not_wall_clock(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch()
    wide, _p, _c = source.sold_comps(watch, days=90)
    narrow, _p2, _c2 = source.sold_comps(watch, days=30)
    assert len(wide) == 22
    assert 0 < len(narrow) < len(wide)
    # Every surviving comp is inside the 30 day window ending 2026-08-05.
    assert all(c.sold_at >= "2026-07-06" for c in narrow)


def test_missing_fixture_file_raises():
    with pytest.raises(FileNotFoundError):
        ebay.FixtureSource("/nonexistent/fixture.json")


def test_unknown_query_yields_nothing_rather_than_exploding(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    comps, pages, cap_hit = source.sold_comps(make_watch("Nikon Z 50mm f/1.8 S"))
    assert comps == []
    assert pages == 1
    assert cap_hit is False


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_mismatched_currency_comps_are_excluded_and_counted_with_a_reason(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch()
    raw, _pages, _cap = source.sold_comps(watch, days=90)
    kept, exclusions = ebay.filter_comps(raw, watch)

    reasons = [e.reason for e in exclusions]
    assert reasons.count("currency_mismatch") == 1
    currency_exclusion = next(e for e in exclusions if e.reason == "currency_mismatch")
    assert "EUR" in currency_exclusion.detail
    assert "does not convert" in currency_exclusion.detail
    assert all(c.currency == "USD" for c in kept)


def test_title_mismatches_and_negative_tokens_are_separated(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch()
    raw_comps, _p, _c = source.sold_comps(watch, days=90)
    _kept, comp_exclusions = ebay.filter_comps(raw_comps, watch)
    raw_listings, _p2, _c2 = source.live_listings(watch)
    kept_listings, listing_exclusions = ebay.filter_listings(raw_listings, watch)

    comp_reasons = [e.reason for e in comp_exclusions]
    assert "title_mismatch" in comp_reasons          # the Zeiss ZA near miss

    listing_reasons = [e.reason for e in listing_exclusions]
    assert listing_reasons.count("negative_token") == 1   # the "FOR PARTS" listing
    assert listing_reasons.count("title_mismatch") == 1   # the f/1.8 near miss
    assert len(kept_listings) == 3
    assert all("PARTS" not in l.title for l in kept_listings)
    assert all("F1.8" not in l.title for l in kept_listings)


def test_unknown_condition_is_kept_but_flagged(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch("Fujifilm XF 56mm f/1.2 R", require=())
    raw, _p, _c = source.sold_comps(watch, days=90)
    kept, exclusions = ebay.filter_comps(raw, watch)
    unknown = [c for c in kept if c.condition is Condition.UNKNOWN]
    assert len(unknown) == 1
    assert unknown[0].condition_id == "9999"
    flagged = [e for e in exclusions if e.reason == "unknown_condition"]
    assert len(flagged) == 1
    assert "9999" in flagged[0].detail


def test_extra_watch_exclusions_apply_to_listings(fixture_path):
    source = ebay.FixtureSource(fixture_path)
    watch = make_watch(exclude=("moving sale",))
    raw, _p, _c = source.live_listings(watch)
    kept, exclusions = ebay.filter_listings(raw, watch)
    assert len(kept) == 2
    assert any("moving sale" in e.detail for e in exclusions)


# ---------------------------------------------------------------------------
# API source (no network: the opener is a scripted stub)
# ---------------------------------------------------------------------------


def build_api_source(script):
    clock = FakeClock()
    opener = RecordingOpener(script)
    client = HttpClient(
        opener=opener,
        limiter=TokenBucket(1000.0, 1000.0, clock=clock, sleeper=lambda s: None),
        cache=None,
        max_retries=0,
        sleeper=lambda s: None,
    )
    credentials = Credentials(
        client_id="id-canary-000111", client_secret="secret-canary-000222"
    )
    provider = TokenProvider(credentials, client, clock=clock)
    source = ebay.EbayApiSource(client, provider)
    return source, opener


def test_api_source_sends_the_token_and_marketplace_headers():
    token = json.dumps({"access_token": "tok-abc-123456", "expires_in": 7200})
    page = json.dumps(
        {
            "total": 1,
            "itemSales": [
                {
                    "itemId": "v1|9|0",
                    "title": "Sony FE 35mm f/1.4 GM",
                    "lastSoldPrice": {"value": "900.00", "currency": "USD"},
                    "lastSoldDate": "2026-07-01T00:00:00.000Z",
                    "conditionId": "3000",
                }
            ],
        }
    )
    source, opener = build_api_source([json_response(token), json_response(page)])
    comps, pages, cap_hit = source.sold_comps(make_watch(), days=90, max_pages=3)
    assert len(comps) == 1 and pages == 1 and cap_hit is False

    api_request = opener.calls[1]
    assert api_request.get_header("Authorization") == "Bearer tok-abc-123456"
    assert api_request.get_header("X-ebay-c-marketplace-id") == "EBAY_US"
    assert "lastSoldDate" in api_request.full_url
    assert "priceCurrency%3AUSD" in api_request.full_url
    # Credentials never appear in a URL.
    assert "secret-canary" not in api_request.full_url


def test_api_pagination_walks_offsets_and_stops_at_the_cap():
    token = json.dumps({"access_token": "tok-abc-123456", "expires_in": 7200})

    def page(offset):
        return json.dumps(
            {
                "total": 500,
                "offset": offset,
                "limit": 2,
                "itemSummaries": [
                    {
                        "itemId": "v1|%d|0" % (offset + i),
                        "title": "Sony FE 35mm f/1.4 GM lens",
                        "price": {"value": "900.00", "currency": "USD"},
                        "conditionId": "3000",
                    }
                    for i in range(2)
                ],
            }
        )

    script = [json_response(token)] + [json_response(page(o)) for o in (0, 2, 4, 6, 8)]
    source, opener = build_api_source(script)
    listings, pages, cap_hit = source.live_listings(make_watch(), max_pages=3, page_size=2)
    assert pages == 3
    assert cap_hit is True
    assert len(listings) == 6
    # One token call plus three page calls.
    assert opener.call_count == 4
    offsets = [c.full_url.split("offset=")[1].split("&")[0] for c in opener.calls[1:]]
    assert offsets == ["0", "2", "4"]


def test_api_source_repr_leaks_nothing():
    source, _opener = build_api_source([json_response("{}")])
    assert "secret-canary" not in repr(source)
    assert "EbayApiSource" in repr(source)


def test_fixture_json_on_disk_is_well_formed(fixture_path):
    with open(fixture_path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["fixture_version"] == 1
    assert set(data["queries"]) == {"Sony FE 35mm f/1.4 GM", "Fujifilm XF 56mm f/1.2 R"}
    total_sold = sum(
        len(page["itemSales"])
        for entry in data["queries"].values()
        for page in entry["sold_pages"]
    )
    total_live = sum(
        len(page["itemSummaries"])
        for entry in data["queries"].values()
        for page in entry["live_pages"]
    )
    assert total_sold >= 25
    assert total_live >= 5
