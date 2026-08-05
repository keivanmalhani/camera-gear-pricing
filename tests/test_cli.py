"""End to end tests for the command line interface.

Every one of these runs the real ``main([...])`` against a real sqlite file in a
temporary directory, in fixture mode, with no network and no credentials.
"""

from __future__ import annotations

import io
import os

import pytest

from conftest import FIXTURE_PATH
from gearwatch import cli
from gearwatch.auth import CLIENT_ID_ENV, CLIENT_SECRET_ENV


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def text(self) -> str:
        return self.out + self.err


def run(*argv) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), out=out, err=err)
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "gear.db")


@pytest.fixture
def no_credentials(monkeypatch):
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(CLIENT_SECRET_ENV, raising=False)
    return None


def add_sony(db_path):
    return run(
        "--db", db_path, "watch", "add", "Sony FE 35mm f/1.4 GM",
        "--max-price", "900", "--condition", "excellent",
        "--currency", "USD", "--require", "gm",
    )


# ---------------------------------------------------------------------------
# The full happy path
# ---------------------------------------------------------------------------


def test_end_to_end_in_fixture_mode(db_path, tmp_path, no_credentials):
    init = run("--db", db_path, "init")
    assert init.code == 0
    assert "schema version" in init.out
    assert os.path.exists(db_path)

    added = add_sony(db_path)
    assert added.code == 0
    assert "added watch 1" in added.out
    assert "sony 35mm f1.4 gm" in added.out

    added2 = run(
        "--db", db_path, "watch", "add", "Fujifilm XF 56mm f/1.2 R",
        "--max-price", "600", "--condition", "excellent", "--currency", "USD",
    )
    assert added2.code == 0

    listed = run("--db", db_path, "watch", "list")
    assert listed.code == 0
    assert "Sony FE 35mm f/1.4 GM" in listed.out
    assert "Fujifilm XF 56mm f/1.2 R" in listed.out

    synced = run("--db", db_path, "sync", "--fixture", FIXTURE_PATH, "--days", "90")
    assert synced.code == 0
    assert "source: fixture" in synced.out
    assert "not live market data" in synced.out
    assert "currency_mismatch=1" in synced.out
    assert "negative_token=1" in synced.out
    assert "title_mismatch=2" in synced.out

    prices = run("--db", db_path, "prices")
    assert prices.code == 0
    assert "median 902.00 USD" in prices.out
    assert "p25 883.75 USD" in prices.out
    assert "p75 926.25 USD" in prices.out
    assert "trimmed mean (10 percent off each tail): 904.10 USD" in prices.out
    assert "1 dropped outside the 1.5 IQR fence" in prices.out
    assert "320.00" in prices.out
    assert "12 used of 13 fetched" in prices.out
    assert "insufficient data" in prices.out          # the thin condition buckets
    assert "trend: down 45.00 USD" in prices.out

    deals = run("--db", db_path, "deals", "--min-score", "60")
    assert deals.code == 0
    assert "849.00 USD" in deals.out
    assert "under p25 of recent sold, 12 comps" in deals.out
    assert "under your max price" in deals.out
    assert "2 listing(s) at or above score 60" in deals.out
    # the for-parts listing was never stored, so it cannot show up here
    assert "399.00" not in deals.out
    assert "875.00" not in deals.out

    target = tmp_path / "dashboard.html"
    dash = run("--db", db_path, "dashboard", "-o", str(target))
    assert dash.code == 0
    assert target.exists()
    html = target.read_text(encoding="utf-8")
    assert "Sony FE 35mm f/1.4 GM" in html
    assert "http" not in html.lower()
    assert "bytes" in dash.out


def test_syncing_twice_does_not_duplicate_comps(db_path, no_credentials):
    run("--db", db_path, "init")
    add_sony(db_path)
    first = run("--db", db_path, "sync", "--fixture", FIXTURE_PATH)
    second = run("--db", db_path, "sync", "--fixture", FIXTURE_PATH)
    assert first.code == second.code == 0
    assert "20 stored, 20 new" in first.out
    assert "20 stored, 0 new" in second.out

    prices_first = run("--db", db_path, "prices").out
    prices_second = run("--db", db_path, "prices").out
    # Identical bands, modulo the "data as of" line.
    assert prices_first.split("\n")[2:] == prices_second.split("\n")[2:]


def test_min_comps_flag_can_force_a_refusal(db_path, no_credentials):
    run("--db", db_path, "init")
    add_sony(db_path)
    run("--db", db_path, "sync", "--fixture", FIXTURE_PATH)
    strict = run("--db", db_path, "--min-comps", "40", "prices")
    assert strict.code == 0
    assert "NOT REPORTED" in strict.out
    assert "need at least 40" in strict.out
    assert "median 902.00" not in strict.out


def test_deals_respects_min_score(db_path, no_credentials):
    run("--db", db_path, "init")
    add_sony(db_path)
    run("--db", db_path, "sync", "--fixture", FIXTURE_PATH)
    everything = run("--db", db_path, "deals", "--min-score", "0")
    picky = run("--db", db_path, "deals", "--min-score", "99")
    assert "899.00 USD" in everything.out
    assert "899.00 USD" not in picky.out
    assert "849.00 USD" in picky.out


def test_watch_remove(db_path, no_credentials):
    run("--db", db_path, "init")
    add_sony(db_path)
    removed = run("--db", db_path, "watch", "remove", "1")
    assert removed.code == 0
    missing = run("--db", db_path, "watch", "remove", "1")
    assert missing.code == 1
    assert "no watch with id 1" in missing.err


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_auth_check_without_credentials_exits_nonzero_and_names_the_variables(
    monkeypatch,
):
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(CLIENT_SECRET_ENV, raising=False)
    result = run("auth", "check")
    assert result.code == 1
    assert CLIENT_ID_ENV in result.err
    assert CLIENT_SECRET_ENV in result.err
    assert "MISSING" in result.err
    assert "export" in result.err


def test_auth_check_names_only_the_missing_variable(monkeypatch):
    secret_value = "canary-secret-value-must-not-print-0917"
    monkeypatch.setenv(CLIENT_ID_ENV, "canary-id-must-not-print-0917")
    monkeypatch.delenv(CLIENT_SECRET_ENV, raising=False)
    result = run("auth", "check")
    assert result.code == 1
    assert "EBAY_CLIENT_ID: set" in result.err
    assert "EBAY_CLIENT_SECRET: MISSING" in result.err
    assert "missing environment variable(s): EBAY_CLIENT_SECRET" in result.err
    assert secret_value not in result.text
    assert "canary-id-must-not-print" not in result.text


def test_auth_check_with_credentials_prints_no_values(monkeypatch):
    monkeypatch.setenv(CLIENT_ID_ENV, "canary-id-abcdef-123456")
    monkeypatch.setenv(CLIENT_SECRET_ENV, "canary-secret-abcdef-123456")
    result = run("auth", "check")
    assert result.code == 0
    assert "credentials: present" in result.out
    assert "canary-id-abcdef-123456" not in result.text
    assert "canary-secret-abcdef-123456" not in result.text
    assert "presence only" in result.out


def test_sync_without_credentials_and_without_a_fixture_fails_clearly(
    db_path, no_credentials
):
    run("--db", db_path, "init")
    add_sony(db_path)
    result = run("--db", db_path, "sync")
    assert result.code == 1
    assert CLIENT_ID_ENV in result.err
    assert CLIENT_SECRET_ENV in result.err
    assert "--fixture" in result.err


def test_sync_with_a_missing_fixture_fails(db_path, no_credentials):
    run("--db", db_path, "init")
    add_sony(db_path)
    result = run("--db", db_path, "sync", "--fixture", "/nope/missing.json")
    assert result.code == 1
    assert "fixture not found" in result.err


def test_a_failing_sync_reports_the_failure_without_leaking(
    db_path, no_credentials, monkeypatch
):
    from gearwatch import ebay
    from gearwatch import http as gw_http

    secret = "canary-secret-inside-a-third-party-exception-5521"
    gw_http.register_secret(secret)

    def boom(self, *args, **kwargs):
        raise RuntimeError("upstream blew up while holding %s" % secret)

    monkeypatch.setattr(ebay.FixtureSource, "sold_comps", boom)

    run("--db", db_path, "init")
    add_sony(db_path)
    result = run("--db", db_path, "sync", "--fixture", FIXTURE_PATH)
    assert result.code == 1
    assert "sync failed for watch 1" in result.err
    assert "RuntimeError" in result.err
    assert secret not in result.text
    assert "[redacted]" in result.err


def test_commands_that_need_a_watch_say_so(db_path, no_credentials):
    run("--db", db_path, "init")
    for command in ("sync", "prices", "deals"):
        result = run("--db", db_path, command, *(["--fixture", FIXTURE_PATH] if command == "sync" else []))
        assert result.code == 1, command
        assert "no watches" in result.err


def test_watch_list_on_an_empty_database_is_not_an_error(db_path):
    run("--db", db_path, "init")
    result = run("--db", db_path, "watch", "list")
    assert result.code == 0
    assert "no watches yet" in result.out


def test_an_unknown_condition_is_rejected(db_path):
    run("--db", db_path, "init")
    result = run("--db", db_path, "watch", "add", "Sony FE 35mm f/1.4 GM",
                 "--condition", "pristine")
    assert result.code == 1
    assert "unknown condition" in result.err
    assert "excellent" in result.err


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_unknown_command_is_a_usage_error():
    assert run("frobnicate").code == 2


def test_unknown_flag_is_a_usage_error(db_path):
    assert run("--db", db_path, "prices", "--nope").code == 2


def test_abbreviations_are_disabled(db_path):
    run("--db", db_path, "init")
    # --max would be an unambiguous prefix of --max-price if abbreviation were on.
    result = run("--db", db_path, "watch", "add", "Sony FE 35mm f/1.4 GM", "--max", "900")
    assert result.code == 2


def test_no_command_prints_help_and_exits_two():
    result = run()
    assert result.code == 2
    assert "usage: gearwatch" in result.out


def test_bare_subcommand_groups_require_an_action():
    assert run("watch").code == 2
    assert run("auth").code == 2


def test_help_exits_zero():
    assert run("--help").code == 0


def test_the_parser_advertises_the_no_scraping_stance():
    parser = cli.build_parser()
    assert "never scrapes" in parser.epilog
    assert "Official APIs only" in parser.epilog


def test_default_database_path_comes_from_the_environment(monkeypatch, tmp_path):
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("GEARWATCH_DB", str(target))
    assert run("init").code == 0
    assert target.exists()
