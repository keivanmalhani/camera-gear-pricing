"""Tests for storage, migrations, and idempotency."""

from __future__ import annotations

import os
import sqlite3

import pytest

from conftest import make_listing
from gearwatch import db, ebay, match
from gearwatch.models import Condition, Exclusion, SoldComp, SyncResult, Watch


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    db.migrate(connection)
    yield connection
    connection.close()


def make_watch(query="Sony FE 35mm f/1.4 GM", require=("gm",)):
    required, optional = match.derive_tokens(query, require)
    return Watch(
        query=query,
        max_price=900.0,
        condition=Condition.EXCELLENT,
        currency="USD",
        marketplace="EBAY_US",
        required_tokens=required,
        optional_tokens=optional,
        excluded_tokens=("body only",),
        model_key=match.model_key(query),
    )


def comp(item_id="v1|1|0", price=899.0, currency="USD",
         condition=Condition.EXCELLENT, sold_at="2026-06-15T10:00:00Z"):
    return SoldComp(
        item_id=item_id,
        title="Sony FE 35mm f/1.4 GM",
        price=price,
        currency=currency,
        condition=condition,
        condition_id="3000",
        sold_at=sold_at,
        marketplace="EBAY_US",
        url="https://www.ebay.com/itm/1",
    )


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_migrate_applies_every_version_and_records_them(tmp_path):
    connection = db.connect(str(tmp_path / "m.db"))
    assert db.schema_version(connection) == 0
    assert db.migrate(connection) == db.SCHEMA_VERSION
    assert db.schema_version(connection) == db.SCHEMA_VERSION
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in rows] == [v for v, _sql in db.MIGRATIONS]
    connection.close()


def test_migrate_is_idempotent(tmp_path):
    path = str(tmp_path / "m.db")
    first = db.connect(path)
    db.migrate(first)
    first.close()
    second = db.connect(path)
    assert db.migrate(second) == db.SCHEMA_VERSION      # no error, no duplicate work
    assert db.migrate(second) == db.SCHEMA_VERSION
    second.close()


def test_migration_two_added_the_reference_time_column(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_runs)")}
    assert "reference_time" in columns
    assert "source" in columns


def test_connect_creates_the_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "gear.db"
    connection = db.connect(str(target))
    db.migrate(connection)
    connection.close()
    assert os.path.exists(str(target))


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_watch_round_trip(conn):
    stored = db.add_watch(conn, make_watch())
    assert stored.id == 1
    fetched = db.get_watch(conn, 1)
    assert fetched is not None
    assert fetched.query == "Sony FE 35mm f/1.4 GM"
    assert fetched.condition is Condition.EXCELLENT
    assert fetched.max_price == 900.0
    assert fetched.required_tokens == ("sony", "35mm", "f1.4", "gm")
    assert fetched.optional_tokens == ("fe",)
    assert fetched.excluded_tokens == ("body only",)
    assert fetched.model_key == match.model_key("Sony FE 35mm f/1.4 GM")
    assert fetched.created_at


def test_list_and_remove_watches(conn):
    db.add_watch(conn, make_watch())
    db.add_watch(conn, make_watch("Fujifilm XF 56mm f/1.2 R", require=()))
    assert [w.id for w in db.list_watches(conn)] == [1, 2]
    assert db.remove_watch(conn, 1) is True
    assert db.remove_watch(conn, 1) is False
    assert [w.id for w in db.list_watches(conn)] == [2]
    assert db.get_watch(conn, 99) is None


def test_removing_a_watch_cascades_to_its_comps(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_sold_comps(conn, watch.id, [comp()])
    assert len(db.sold_comps_for(conn, watch.id)) == 1
    db.remove_watch(conn, watch.id)
    assert conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0] == 0


def test_sold_comp_round_trip_preserves_every_field(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_sold_comps(conn, watch.id, [comp()])
    stored = db.sold_comps_for(conn, watch.id)[0]
    assert stored.item_id == "v1|1|0"
    assert stored.price == 899.0
    assert stored.currency == "USD"
    assert stored.condition is Condition.EXCELLENT
    assert stored.condition_id == "3000"
    assert stored.sold_at == "2026-06-15T10:00:00Z"
    assert stored.watch_id == watch.id
    assert stored.fetched_at


def test_sold_comp_filters(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_sold_comps(
        conn,
        watch.id,
        [
            comp("a", 900, "USD", Condition.EXCELLENT),
            comp("b", 800, "USD", Condition.GOOD),
            comp("c", 850, "EUR", Condition.EXCELLENT),
        ],
    )
    assert len(db.sold_comps_for(conn, watch.id)) == 3
    assert len(db.sold_comps_for(conn, watch.id, currency="USD")) == 2
    assert len(db.sold_comps_for(conn, watch.id, condition=Condition.EXCELLENT)) == 2
    assert (
        len(
            db.sold_comps_for(
                conn, watch.id, condition=Condition.EXCELLENT, currency="USD"
            )
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_upserting_the_same_comps_twice_does_not_duplicate(conn):
    watch = db.add_watch(conn, make_watch())
    comps = [comp("a"), comp("b"), comp("c")]
    new_first, total_first = db.upsert_sold_comps(conn, watch.id, comps)
    new_second, total_second = db.upsert_sold_comps(conn, watch.id, comps)
    assert (new_first, total_first) == (3, 3)
    assert (new_second, total_second) == (0, 3)
    assert len(db.sold_comps_for(conn, watch.id)) == 3


def test_upsert_refreshes_a_changed_price_in_place(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_sold_comps(conn, watch.id, [comp("a", price=899.0)])
    db.upsert_sold_comps(conn, watch.id, [comp("a", price=905.0)])
    rows = db.sold_comps_for(conn, watch.id)
    assert len(rows) == 1
    assert rows[0].price == 905.0


def test_the_unique_index_on_identity_is_real(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_sold_comps(conn, watch.id, [comp("a")])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sold_comps(watch_id, item_id, title, price, currency,"
            " condition, sold_at, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            (watch.id, "a", "dupe", 1.0, "USD", "excellent", "x", "y"),
        )


def test_syncing_the_same_fixture_twice_is_idempotent(conn, fixture_path):
    watch = db.add_watch(conn, make_watch())
    source = ebay.FixtureSource(fixture_path)
    for _pass in range(2):
        raw, _pages, _cap = source.sold_comps(watch, days=90)
        kept, _exclusions = ebay.filter_comps(raw, watch)
        db.upsert_sold_comps(conn, watch.id, kept)
    assert conn.execute("SELECT COUNT(*) FROM sold_comps").fetchone()[0] == 20
    ids = [r[0] for r in conn.execute("SELECT item_id FROM sold_comps")]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def test_listing_round_trip_and_deactivation(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_listings(
        conn, watch.id, [make_listing(849.0, "l1"), make_listing(899.0, "l2")]
    )
    assert len(db.listings_for(conn, watch.id)) == 2

    # Second sync only sees l1: l2 must be marked inactive, not deleted.
    db.upsert_listings(conn, watch.id, [make_listing(839.0, "l1")])
    deactivated = db.deactivate_missing_listings(conn, watch.id, ["l1"])
    assert deactivated == 1
    active = db.listings_for(conn, watch.id, active_only=True)
    assert [l.item_id for l in active] == ["l1"]
    assert active[0].price == 839.0
    assert len(db.listings_for(conn, watch.id, active_only=False)) == 2


def test_deactivating_with_an_empty_seen_list_clears_everything(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_listings(conn, watch.id, [make_listing(849.0, "l1")])
    assert db.deactivate_missing_listings(conn, watch.id, []) == 1
    assert db.listings_for(conn, watch.id) == []


def test_a_reappearing_listing_is_reactivated(conn):
    watch = db.add_watch(conn, make_watch())
    db.upsert_listings(conn, watch.id, [make_listing(849.0, "l1")])
    db.deactivate_missing_listings(conn, watch.id, [])
    db.upsert_listings(conn, watch.id, [make_listing(849.0, "l1")])
    assert len(db.listings_for(conn, watch.id, active_only=True)) == 1


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------


def test_sync_runs_are_recorded_and_queryable(conn):
    watch = db.add_watch(conn, make_watch())
    result = SyncResult(
        watch_id=watch.id,
        query=watch.query,
        comps_seen=22,
        comps_stored=20,
        comps_new=20,
        listings_seen=5,
        listings_new=3,
        pages_fetched=3,
        exclusions=(
            Exclusion("a", "currency_mismatch", "EUR"),
            Exclusion("b", "title_mismatch", "no gm"),
            Exclusion("c", "currency_mismatch", "GBP"),
        ),
        started_at="2026-08-05T00:00:00Z",
        finished_at="2026-08-05T00:00:05Z",
    )
    assert result.exclusion_counts() == {"currency_mismatch": 2, "title_mismatch": 1}
    db.record_sync(conn, result, source="fixture", reference_time="2026-08-05T00:00:00Z")
    assert db.last_sync_at(conn) == "2026-08-05T00:00:05Z"
    assert db.last_sync_at(conn, watch.id) == "2026-08-05T00:00:05Z"
    assert db.last_sync_at(conn, 999) == ""


def test_last_sync_at_is_empty_before_any_sync(conn):
    assert db.last_sync_at(conn) == ""


# ---------------------------------------------------------------------------
# The database holds no credentials
# ---------------------------------------------------------------------------


def test_no_credential_ever_lands_in_the_database(conn, fixture_path, monkeypatch):
    secret = "SECRET-in-the-db-would-be-a-bug-71f3aa"
    monkeypatch.setenv("EBAY_CLIENT_ID", "ID-in-the-db-would-be-a-bug-71f3aa")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", secret)

    watch = db.add_watch(conn, make_watch())
    source = ebay.FixtureSource(fixture_path)
    raw, _pages, _cap = source.sold_comps(watch, days=90)
    kept, _exclusions = ebay.filter_comps(raw, watch)
    db.upsert_sold_comps(conn, watch.id, kept)
    raw_listings, _p, _c = source.live_listings(watch)
    listings, _e = ebay.filter_listings(raw_listings, watch)
    db.upsert_listings(conn, watch.id, listings)

    tables = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    assert tables, "no tables were created"
    haystack = []
    for table in tables:
        for row in conn.execute("SELECT * FROM %s" % table):
            haystack.extend(str(value) for value in tuple(row))
    blob = "\n".join(haystack)
    assert secret not in blob
    assert "ID-in-the-db-would-be-a-bug" not in blob
    assert "Bearer" not in blob
    # And there is no table that even looks like it stores credentials.
    assert not any(
        word in name.lower()
        for name in tables
        for word in ("token", "secret", "credential", "auth")
    )
