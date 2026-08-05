"""SQLite storage with versioned migrations.

The database holds watches, sold comps, live listings, and a log of sync runs.
It deliberately holds no credentials of any kind: there is no token table, no
settings table, and nothing that accepts a secret. A test walks every text cell
in every table and asserts a known secret value is absent.

Idempotency: sold comps and listings are keyed on ``(watch_id, item_id)`` with a
unique index, and writes use ``ON CONFLICT DO UPDATE``. Syncing the same fixture
twice therefore updates rows in place and never duplicates a comp.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import Condition, Listing, SoldComp, SyncResult, Watch

__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "connect",
    "migrate",
    "schema_version",
    "utc_now",
    "add_watch",
    "list_watches",
    "get_watch",
    "remove_watch",
    "upsert_sold_comps",
    "upsert_listings",
    "sold_comps_for",
    "listings_for",
    "record_sync",
    "last_sync_at",
    "deactivate_missing_listings",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

_MIGRATION_1 = """
CREATE TABLE watches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query            TEXT    NOT NULL,
    model_key        TEXT    NOT NULL,
    max_price        REAL,
    condition        TEXT    NOT NULL,
    currency         TEXT    NOT NULL,
    marketplace      TEXT    NOT NULL DEFAULT 'EBAY_US',
    required_tokens  TEXT    NOT NULL DEFAULT '[]',
    optional_tokens  TEXT    NOT NULL DEFAULT '[]',
    excluded_tokens  TEXT    NOT NULL DEFAULT '[]',
    created_at       TEXT    NOT NULL
);

CREATE TABLE sold_comps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    item_id       TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    price         REAL    NOT NULL,
    currency      TEXT    NOT NULL,
    condition     TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL DEFAULT '',
    sold_at       TEXT    NOT NULL,
    marketplace   TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL DEFAULT '',
    fetched_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX idx_sold_comps_identity ON sold_comps(watch_id, item_id);
CREATE INDEX idx_sold_comps_lookup ON sold_comps(watch_id, condition, currency);

CREATE TABLE listings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    item_id       TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    price         REAL    NOT NULL,
    currency      TEXT    NOT NULL,
    condition     TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL DEFAULT '',
    seller        TEXT    NOT NULL DEFAULT '',
    listed_at     TEXT    NOT NULL DEFAULT '',
    marketplace   TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL DEFAULT '',
    seen_at       TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX idx_listings_identity ON listings(watch_id, item_id);

CREATE TABLE sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id       INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT    NOT NULL,
    source         TEXT    NOT NULL DEFAULT '',
    comps_seen     INTEGER NOT NULL DEFAULT 0,
    comps_stored   INTEGER NOT NULL DEFAULT 0,
    comps_new      INTEGER NOT NULL DEFAULT 0,
    listings_seen  INTEGER NOT NULL DEFAULT 0,
    listings_new   INTEGER NOT NULL DEFAULT 0,
    pages_fetched  INTEGER NOT NULL DEFAULT 0,
    page_cap_hit   INTEGER NOT NULL DEFAULT 0,
    exclusions     TEXT    NOT NULL DEFAULT '{}'
);
"""

_MIGRATION_2 = """
-- Recording which source produced a run makes fixture-mode data obvious in the
-- report instead of silently masquerading as live market data.
ALTER TABLE sync_runs ADD COLUMN reference_time TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_sync_runs_watch ON sync_runs(watch_id, finished_at);
"""

#: ``(version, sql)`` applied in order. Append only; never edit a shipped entry.
MIGRATIONS: Tuple[Tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
)

SCHEMA_VERSION = MIGRATIONS[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the database's recorded version."""
    current = schema_version(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utc_now()),
        )
        conn.execute("PRAGMA user_version = %d" % version)
        current = version
    conn.commit()
    return current


def connect(path: str) -> sqlite3.Connection:
    """Open (and create the parent directory of) a gearwatch database."""
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------


def _dump(values: Sequence[str]) -> str:
    return json.dumps(list(values))


def _load(text: str) -> Tuple[str, ...]:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return ()
    return tuple(str(item) for item in data)


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=int(row["id"]),
        query=row["query"],
        model_key=row["model_key"],
        max_price=None if row["max_price"] is None else float(row["max_price"]),
        condition=Condition(row["condition"]),
        currency=row["currency"],
        marketplace=row["marketplace"],
        required_tokens=_load(row["required_tokens"]),
        optional_tokens=_load(row["optional_tokens"]),
        excluded_tokens=_load(row["excluded_tokens"]),
        created_at=row["created_at"],
    )


def add_watch(conn: sqlite3.Connection, watch: Watch) -> Watch:
    created = watch.created_at or utc_now()
    cursor = conn.execute(
        "INSERT INTO watches("
        " query, model_key, max_price, condition, currency, marketplace,"
        " required_tokens, optional_tokens, excluded_tokens, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            watch.query,
            watch.model_key,
            watch.max_price,
            watch.condition.value,
            watch.currency,
            watch.marketplace,
            _dump(watch.required_tokens),
            _dump(watch.optional_tokens),
            _dump(watch.excluded_tokens),
            created,
        ),
    )
    conn.commit()
    return Watch(
        query=watch.query,
        model_key=watch.model_key,
        max_price=watch.max_price,
        condition=watch.condition,
        currency=watch.currency,
        marketplace=watch.marketplace,
        required_tokens=watch.required_tokens,
        optional_tokens=watch.optional_tokens,
        excluded_tokens=watch.excluded_tokens,
        created_at=created,
        id=int(cursor.lastrowid),
    )


def list_watches(conn: sqlite3.Connection) -> List[Watch]:
    rows = conn.execute("SELECT * FROM watches ORDER BY id").fetchall()
    return [_row_to_watch(row) for row in rows]


def get_watch(conn: sqlite3.Connection, watch_id: int) -> Optional[Watch]:
    row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row) if row else None


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    cursor = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
    conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Comps and listings
# ---------------------------------------------------------------------------


def upsert_sold_comps(
    conn: sqlite3.Connection, watch_id: int, comps: Iterable[SoldComp]
) -> Tuple[int, int]:
    """Insert or refresh comps. Returns ``(new_rows, total_written)``."""
    now = utc_now()
    new_rows = 0
    total = 0
    for comp in comps:
        total += 1
        existing = conn.execute(
            "SELECT id FROM sold_comps WHERE watch_id = ? AND item_id = ?",
            (watch_id, comp.item_id),
        ).fetchone()
        if existing is None:
            new_rows += 1
        conn.execute(
            "INSERT INTO sold_comps("
            " watch_id, item_id, title, price, currency, condition, condition_id,"
            " sold_at, marketplace, url, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(watch_id, item_id) DO UPDATE SET"
            "  title=excluded.title, price=excluded.price, currency=excluded.currency,"
            "  condition=excluded.condition, condition_id=excluded.condition_id,"
            "  sold_at=excluded.sold_at, marketplace=excluded.marketplace,"
            "  url=excluded.url, fetched_at=excluded.fetched_at",
            (
                watch_id,
                comp.item_id,
                comp.title,
                float(comp.price),
                comp.currency,
                comp.condition.value,
                comp.condition_id,
                comp.sold_at,
                comp.marketplace,
                comp.url,
                comp.fetched_at or now,
            ),
        )
    conn.commit()
    return new_rows, total


def upsert_listings(
    conn: sqlite3.Connection, watch_id: int, listings: Iterable[Listing]
) -> Tuple[int, int]:
    now = utc_now()
    new_rows = 0
    total = 0
    for listing in listings:
        total += 1
        existing = conn.execute(
            "SELECT id FROM listings WHERE watch_id = ? AND item_id = ?",
            (watch_id, listing.item_id),
        ).fetchone()
        if existing is None:
            new_rows += 1
        conn.execute(
            "INSERT INTO listings("
            " watch_id, item_id, title, price, currency, condition, condition_id,"
            " seller, listed_at, marketplace, url, seen_at, active)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)"
            " ON CONFLICT(watch_id, item_id) DO UPDATE SET"
            "  title=excluded.title, price=excluded.price, currency=excluded.currency,"
            "  condition=excluded.condition, condition_id=excluded.condition_id,"
            "  seller=excluded.seller, listed_at=excluded.listed_at,"
            "  marketplace=excluded.marketplace, url=excluded.url,"
            "  seen_at=excluded.seen_at, active=1",
            (
                watch_id,
                listing.item_id,
                listing.title,
                float(listing.price),
                listing.currency,
                listing.condition.value,
                listing.condition_id,
                listing.seller,
                listing.listed_at,
                listing.marketplace,
                listing.url,
                listing.seen_at or now,
                            ),
        )
    conn.commit()
    return new_rows, total


def deactivate_missing_listings(
    conn: sqlite3.Connection, watch_id: int, seen_item_ids: Sequence[str]
) -> int:
    """Mark listings we did not see in this sync as no longer active."""
    if seen_item_ids:
        placeholders = ",".join("?" for _ in seen_item_ids)
        cursor = conn.execute(
            "UPDATE listings SET active = 0 WHERE watch_id = ? AND active = 1"
            " AND item_id NOT IN (%s)" % placeholders,
            [watch_id, *seen_item_ids],
        )
    else:
        cursor = conn.execute(
            "UPDATE listings SET active = 0 WHERE watch_id = ? AND active = 1",
            (watch_id,),
        )
    conn.commit()
    return cursor.rowcount


def sold_comps_for(
    conn: sqlite3.Connection,
    watch_id: int,
    condition: Optional[Condition] = None,
    currency: Optional[str] = None,
) -> List[SoldComp]:
    sql = "SELECT * FROM sold_comps WHERE watch_id = ?"
    params: List[object] = [watch_id]
    if condition is not None:
        sql += " AND condition = ?"
        params.append(condition.value)
    if currency is not None:
        sql += " AND currency = ?"
        params.append(currency)
    sql += " ORDER BY sold_at, item_id"
    rows = conn.execute(sql, params).fetchall()
    return [
        SoldComp(
            item_id=row["item_id"],
            title=row["title"],
            price=float(row["price"]),
            currency=row["currency"],
            condition=Condition(row["condition"]),
            condition_id=row["condition_id"],
            sold_at=row["sold_at"],
            marketplace=row["marketplace"],
            url=row["url"],
            watch_id=int(row["watch_id"]),
            fetched_at=row["fetched_at"],
        )
        for row in rows
    ]


def listings_for(
    conn: sqlite3.Connection, watch_id: int, active_only: bool = True
) -> List[Listing]:
    sql = "SELECT * FROM listings WHERE watch_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY price, item_id"
    rows = conn.execute(sql, (watch_id,)).fetchall()
    return [
        Listing(
            item_id=row["item_id"],
            title=row["title"],
            price=float(row["price"]),
            currency=row["currency"],
            condition=Condition(row["condition"]),
            condition_id=row["condition_id"],
            seller=row["seller"],
            listed_at=row["listed_at"],
            marketplace=row["marketplace"],
            url=row["url"],
            watch_id=int(row["watch_id"]),
            seen_at=row["seen_at"],
            active=bool(row["active"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------


def record_sync(
    conn: sqlite3.Connection,
    result: SyncResult,
    source: str = "",
    reference_time: str = "",
) -> int:
    cursor = conn.execute(
        "INSERT INTO sync_runs("
        " watch_id, started_at, finished_at, source, comps_seen, comps_stored,"
        " comps_new, listings_seen, listings_new, pages_fetched, page_cap_hit,"
        " exclusions, reference_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            result.watch_id,
            result.started_at or utc_now(),
            result.finished_at or utc_now(),
            source,
            result.comps_seen,
            result.comps_stored,
            result.comps_new,
            result.listings_seen,
            result.listings_new,
            result.pages_fetched,
            1 if result.page_cap_hit else 0,
            json.dumps(result.exclusion_counts()),
            reference_time,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def last_sync_at(conn: sqlite3.Connection, watch_id: Optional[int] = None) -> str:
    if watch_id is None:
        row = conn.execute(
            "SELECT MAX(finished_at) AS ts FROM sync_runs"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(finished_at) AS ts FROM sync_runs WHERE watch_id = ?",
            (watch_id,),
        ).fetchone()
    return (row["ts"] if row and row["ts"] else "") or ""
