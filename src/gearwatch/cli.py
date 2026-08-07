"""Command line interface.

Exit codes:

* ``0`` success
* ``1`` an expected failure the user can act on (no credentials, no watches,
  fixture missing, nothing to report)
* ``2`` a usage error (bad flags, unknown subcommand); argparse owns this one

``allow_abbrev=False`` throughout: ``--max`` must never silently become
``--max-price`` on a tool that spends money.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence, TextIO, Tuple

from . import __version__
from . import alerts, dashboard as dashboard_mod, db, ebay, match, report, stats
from .auth import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    AuthError,
    Credentials,
    MissingCredentialsError,
    TokenProvider,
    credential_status,
)
from .http import DiskCache, HttpClient, TokenBucket, redact
from .models import Condition, Deal, PriceStat, SyncResult, Watch

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

DEFAULT_DB = "gearwatch.db"
DEFAULT_CACHE_DIR = ".gearwatch-cache"


def _default_db() -> str:
    return os.environ.get("GEARWATCH_DB") or DEFAULT_DB


def _default_cache_dir() -> str:
    return os.environ.get("GEARWATCH_CACHE_DIR") or DEFAULT_CACHE_DIR


def _add_parser(subparsers, name: str, **kwargs) -> argparse.ArgumentParser:
    """``add_parser`` that never forgets ``allow_abbrev=False``.

    argparse does NOT propagate ``allow_abbrev`` from a parent parser to its
    subparsers, so a top-level ``allow_abbrev=False`` silently fails to protect
    subcommand flags. Without this, ``--max`` would quietly become
    ``--max-price``. Every subparser is built through here.
    """
    kwargs.setdefault("allow_abbrev", False)
    return subparsers.add_parser(name, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gearwatch",
        allow_abbrev=False,
        description=(
            "Used camera gear price tracking from official marketplace APIs. "
            "Sold prices are signal; asking prices are noise."
        ),
        epilog="gearwatch never scrapes. Official APIs only.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--db",
        default=None,
        help="sqlite database path (default: $GEARWATCH_DB or ./%s)" % DEFAULT_DB,
    )
    parser.add_argument(
        "--min-comps",
        type=int,
        default=stats.DEFAULT_MIN_COMPS,
        help=(
            "minimum completed sales before a band is published "
            "(default: %d). Below this gearwatch reports insufficient data."
            % stats.DEFAULT_MIN_COMPS
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    _add_parser(subparsers, "init", help="create or migrate the database")

    watch_parser = _add_parser(subparsers, "watch", help="manage watches")
    watch_sub = watch_parser.add_subparsers(dest="watch_command", metavar="ACTION")

    add_parser = _add_parser(watch_sub, "add", help="add a watch")
    add_parser.add_argument("query", help='model to hunt, e.g. "Sony FE 35mm f/1.4 GM"')
    add_parser.add_argument("--max-price", type=float, default=None)
    add_parser.add_argument("--condition", default="excellent")
    add_parser.add_argument("--currency", default="USD")
    add_parser.add_argument("--marketplace", default="EBAY_US")
    add_parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="TOKEN",
        help="token that must appear in a title (repeatable)",
    )
    add_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="TOKEN",
        help="token that disqualifies a title (repeatable)",
    )

    _add_parser(watch_sub, "list", help="list watches")
    remove_parser = _add_parser(watch_sub, "remove", help="remove a watch")
    remove_parser.add_argument("watch_id", type=int)

    sync_parser = _add_parser(subparsers, "sync", help="pull sold comps and live listings")
    sync_parser.add_argument("--fixture", default=None, help="replay canned API JSON")
    sync_parser.add_argument("--days", type=int, default=90)
    sync_parser.add_argument("--max-pages", type=int, default=ebay.DEFAULT_MAX_PAGES)
    sync_parser.add_argument("--page-size", type=int, default=ebay.DEFAULT_PAGE_SIZE)
    sync_parser.add_argument("--watch", type=int, default=None, dest="watch_id")
    sync_parser.add_argument("--cache-dir", default=None)
    sync_parser.add_argument("--no-cache", action="store_true")
    sync_parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help="requests per second ceiling (default: conservative)",
    )

    prices_parser = _add_parser(subparsers, "prices", help="the sold-price band report")
    prices_parser.add_argument("--watch", type=int, default=None, dest="watch_id")

    deals_parser = _add_parser(subparsers, "deals", help="live listings that beat the band")
    deals_parser.add_argument("--watch", type=int, default=None, dest="watch_id")
    deals_parser.add_argument(
        "--min-score", type=int, default=alerts.DEFAULT_MIN_SCORE
    )

    dash_parser = _add_parser(subparsers, "dashboard", help="write the HTML dashboard")
    dash_parser.add_argument("-o", "--output", default="gearwatch.html")
    dash_parser.add_argument("--watch", type=int, default=None, dest="watch_id")

    auth_parser = _add_parser(subparsers, "auth", help="credential checks")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", metavar="ACTION")
    _add_parser(auth_sub, "check", help="verify credentials are present")

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(args: argparse.Namespace):
    path = args.db or _default_db()
    conn = db.connect(path)
    db.migrate(conn)
    return conn, path


def _selected_watches(conn, watch_id: Optional[int]) -> List[Watch]:
    if watch_id is None:
        return db.list_watches(conn)
    watch = db.get_watch(conn, watch_id)
    return [watch] if watch else []


def _bands_for(conn, watch: Watch, min_comps: int, as_of: str):
    comps = db.sold_comps_for(conn, watch.id, currency=watch.currency)
    bands = alerts.build_bands(comps, watch, min_comps=min_comps, as_of=as_of)
    headline = alerts.headline_band(bands, watch, min_comps, as_of)
    return bands, headline


def _deals_for(conn, watch: Watch, headline: PriceStat, min_comps: int) -> List[Deal]:
    listings = db.listings_for(conn, watch.id, active_only=True)
    return alerts.score_listings(listings, headline, watch, min_comps=min_comps)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, path = _open_db(args)
    version = db.schema_version(conn)
    conn.close()
    out.write("initialised %s at schema version %d\n" % (path, version))
    out.write("next: gearwatch watch add \"Sony FE 35mm f/1.4 GM\" --max-price 900\n")
    return EXIT_OK


def cmd_watch_add(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        condition = Condition.parse(args.condition)
    except ValueError as exc:
        err.write("error: %s\n" % exc)
        return EXIT_FAILURE
    required, optional = match.derive_tokens(args.query, args.require or [])
    watch = Watch(
        query=args.query,
        max_price=args.max_price,
        condition=condition,
        currency=args.currency.upper(),
        marketplace=args.marketplace,
        required_tokens=required,
        optional_tokens=optional,
        excluded_tokens=tuple(args.exclude or []),
        model_key=match.model_key(args.query),
    )
    conn, _path = _open_db(args)
    stored = db.add_watch(conn, watch)
    conn.close()
    out.write("added watch %d: %s\n" % (stored.id, stored.label))
    out.write("  required tokens: %s\n" % " ".join(stored.required_tokens))
    out.write("  optional tokens: %s\n" % (" ".join(stored.optional_tokens) or "(none)"))
    if stored.excluded_tokens:
        out.write("  extra exclusions: %s\n" % " ".join(stored.excluded_tokens))
    return EXIT_OK


def cmd_watch_list(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    watches = db.list_watches(conn)
    conn.close()
    # An empty list is not an error, the same way `ls` on an empty directory is
    # not an error.
    out.write(report.format_watch_list(watches) + "\n")
    return EXIT_OK


def cmd_watch_remove(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    removed = db.remove_watch(conn, args.watch_id)
    conn.close()
    if not removed:
        err.write("error: no watch with id %d\n" % args.watch_id)
        return EXIT_FAILURE
    out.write("removed watch %d\n" % args.watch_id)
    return EXIT_OK


def _build_source(args: argparse.Namespace, err: TextIO):
    if args.fixture:
        try:
            return ebay.FixtureSource(args.fixture), None
        except FileNotFoundError as exc:
            err.write("error: %s\n" % exc)
            return None, EXIT_FAILURE
    ok, missing = credential_status()
    if not ok:
        err.write(
            "error: cannot reach the eBay API without credentials. "
            "Missing environment variable(s): %s\n" % ", ".join(missing)
        )
        err.write(
            "hint: run 'gearwatch sync --fixture fixtures/demo.json' to try "
            "gearwatch with no credentials at all.\n"
        )
        return None, EXIT_FAILURE
    try:
        credentials = Credentials.from_env()
    except MissingCredentialsError as exc:
        err.write("error: %s\n" % exc)
        return None, EXIT_FAILURE
    cache = None
    if not args.no_cache:
        cache = DiskCache(args.cache_dir or _default_cache_dir())
    limiter = TokenBucket(rate_per_second=args.rate) if args.rate else TokenBucket()
    client = HttpClient(limiter=limiter, cache=cache)
    return ebay.EbayApiSource(client, TokenProvider(credentials, client)), None


def cmd_sync(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    watches = _selected_watches(conn, args.watch_id)
    if not watches:
        conn.close()
        err.write("error: no watches to sync. add one with 'gearwatch watch add'.\n")
        return EXIT_FAILURE

    source, failure = _build_source(args, err)
    if source is None:
        conn.close()
        return failure or EXIT_FAILURE

    out.write("gearwatch sync (source: %s)\n" % source.name)
    if source.name == "fixture":
        out.write(
            "NOTE: fixture mode. This is canned data captured %s, not live "
            "market data.\n" % (source.reference_time or "at an unknown time")
        )
    out.write(report.RULE + "\n")

    exit_code = EXIT_OK
    for watch in watches:
        started = db.utc_now()
        try:
            raw_comps, comp_pages, comp_cap = source.sold_comps(
                watch, days=args.days, max_pages=args.max_pages, page_size=args.page_size
            )
            raw_listings, listing_pages, listing_cap = source.live_listings(
                watch, max_pages=args.max_pages, page_size=args.page_size
            )
        except AuthError as exc:
            err.write("error: %s\n" % exc)
            conn.close()
            return EXIT_FAILURE
        except Exception as exc:  # network/transport failures are expected
            # redact() again on the way out: a third-party exception message is
            # not something we control, and this is the last chance to scrub it.
            err.write(
                "error: sync failed for watch %s: %s\n"
                % (watch.id, redact("%s: %s" % (type(exc).__name__, exc)))
            )
            exit_code = EXIT_FAILURE
            continue

        comps, comp_exclusions = ebay.filter_comps(raw_comps, watch)
        listings, listing_exclusions = ebay.filter_listings(raw_listings, watch)

        comps_new, comps_stored = db.upsert_sold_comps(conn, watch.id, comps)
        listings_new, listings_stored = db.upsert_listings(conn, watch.id, listings)
        db.deactivate_missing_listings(conn, watch.id, [l.item_id for l in listings])

        result = SyncResult(
            watch_id=watch.id,
            query=watch.query,
            comps_seen=len(raw_comps),
            comps_stored=comps_stored,
            comps_new=comps_new,
            listings_seen=len(raw_listings),
            listings_stored=listings_stored,
            listings_new=listings_new,
            pages_fetched=comp_pages + listing_pages,
            page_cap_hit=bool(comp_cap or listing_cap),
            exclusions=tuple(comp_exclusions) + tuple(listing_exclusions),
            started_at=started,
            finished_at=db.utc_now(),
        )
        db.record_sync(conn, result, source=source.name, reference_time=source.reference_time)
        out.write(report.format_sync_result(result, source.name) + "\n")

    conn.close()
    return exit_code


def cmd_prices(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    watches = _selected_watches(conn, args.watch_id)
    if not watches:
        conn.close()
        err.write("error: no watches. add one with 'gearwatch watch add'.\n")
        return EXIT_FAILURE
    as_of = db.last_sync_at(conn)
    blocks = []
    for watch in watches:
        bands, headline = _bands_for(conn, watch, args.min_comps, as_of)
        blocks.append(report.format_watch_prices(watch, bands, headline))
    conn.close()
    out.write(report.format_price_report(blocks, as_of) + "\n")
    return EXIT_OK


def cmd_deals(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    watches = _selected_watches(conn, args.watch_id)
    if not watches:
        conn.close()
        err.write("error: no watches. add one with 'gearwatch watch add'.\n")
        return EXIT_FAILURE
    as_of = db.last_sync_at(conn)
    out.write("gearwatch deals (minimum score %d)\n" % args.min_score)
    out.write("data as of %s\n" % (as_of or "never synced"))
    out.write(report.RULE + "\n")
    found = 0
    for watch in watches:
        _bands, headline = _bands_for(conn, watch, args.min_comps, as_of)
        deals = _deals_for(conn, watch, headline, args.min_comps)
        found += len(alerts.rank_deals(deals, args.min_score))
        out.write(alerts.render_alerts(watch, headline, deals, args.min_score) + "\n")
    conn.close()
    out.write(report.RULE + "\n")
    out.write(
        "%d listing(s) at or above score %d. A score of 100 means the asking "
        "price is below every recent completed sale in the band.\n"
        % (found, args.min_score)
    )
    return EXIT_OK


def cmd_dashboard(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    conn, _path = _open_db(args)
    watches = _selected_watches(conn, args.watch_id)
    as_of = db.last_sync_at(conn)
    rows: List[Tuple[Watch, PriceStat, Sequence[Deal]]] = []
    for watch in watches:
        _bands, headline = _bands_for(conn, watch, args.min_comps, as_of)
        rows.append((watch, headline, _deals_for(conn, watch, headline, args.min_comps)))
    conn.close()
    size = dashboard_mod.write_dashboard(args.output, rows, as_of)
    out.write("wrote %s (%d bytes, %d watches)\n" % (args.output, size, len(rows)))
    return EXIT_OK


def cmd_auth_check(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Report presence of credentials without reading, echoing, or hashing them."""
    ok, missing = credential_status()
    if ok:
        out.write("credentials: present\n")
        out.write("  %s: set\n" % CLIENT_ID_ENV)
        out.write("  %s: set\n" % CLIENT_SECRET_ENV)
        out.write(
            "gearwatch never prints, logs, or stores credential values. "
            "This check reports presence only.\n"
        )
        return EXIT_OK
    err.write("credentials: incomplete\n")
    for name in (CLIENT_ID_ENV, CLIENT_SECRET_ENV):
        err.write("  %s: %s\n" % (name, "MISSING" if name in missing else "set"))
    err.write(
        "error: missing environment variable(s): %s\n" % ", ".join(missing)
    )
    err.write(
        "export them in your shell, for example:\n"
        "  export %s=your-app-id\n"
        "  export %s=your-cert-id\n" % (CLIENT_ID_ENV, CLIENT_SECRET_ENV)
    )
    return EXIT_FAILURE


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_WATCH_ACTIONS = {
    "add": cmd_watch_add,
    "list": cmd_watch_list,
    "remove": cmd_watch_remove,
}

_COMMANDS = {
    "init": cmd_init,
    "sync": cmd_sync,
    "prices": cmd_prices,
    "deals": cmd_deals,
    "dashboard": cmd_dashboard,
}


def main(
    argv: Optional[Sequence[str]] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse already printed the diagnostic
        return int(exc.code or 0)

    if not args.command:
        parser.print_help(out)
        return EXIT_USAGE

    if args.command == "watch":
        action = getattr(args, "watch_command", None)
        if not action:
            err.write("error: watch requires an action: add, list, remove\n")
            return EXIT_USAGE
        return _WATCH_ACTIONS[action](args, out, err)

    if args.command == "auth":
        action = getattr(args, "auth_command", None)
        if action != "check":
            err.write("error: auth requires an action: check\n")
            return EXIT_USAGE
        return cmd_auth_check(args, out, err)

    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        err.write("error: unknown command %r\n" % args.command)
        return EXIT_USAGE
    return handler(args, out, err)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
