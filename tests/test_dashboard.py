"""Tests for the self-contained HTML dashboard."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from conftest import make_comps, make_listing
from gearwatch import alerts, dashboard, match, stats
from gearwatch.models import Condition, Watch

SVG_RE = re.compile(r"<svg.*?</svg>", re.S)


def make_watch(query="Sony FE 35mm f/1.4 GM", watch_id=1):
    required, optional = match.derive_tokens(query, ["gm"] if "GM" in query else [])
    return Watch(
        query=query,
        max_price=900.0,
        condition=Condition.EXCELLENT,
        currency="USD",
        required_tokens=required,
        optional_tokens=optional,
        model_key=match.model_key(query),
        id=watch_id,
    )


def build_rows():
    watch = make_watch()
    comps = make_comps([860, 872, 880, 885, 890, 899, 905, 915, 925, 930, 940, 950])
    stat = stats.compute_stat(
        comps, model_key=watch.model_key, condition=Condition.EXCELLENT, currency="USD"
    )
    listings = [
        make_listing(849.0, "l1", title="Sony FE 35mm f/1.4 GM <clean> & boxed"),
        make_listing(1099.0, "l2", title="Sony FE 35mm f/1.4 GM pricey"),
    ]
    deals = alerts.score_listings(listings, stat, watch)

    thin_watch = make_watch("Fujifilm XF 56mm f/1.2 R", watch_id=2)
    thin_stat = stats.compute_stat(
        make_comps([600, 610, 620]), condition=Condition.EXCELLENT, currency="USD"
    )
    thin_deals = alerts.score_listings(
        [make_listing(585.0, "l3", title="Fujifilm XF 56mm f/1.2 R")],
        thin_stat,
        thin_watch,
    )
    return [(watch, stat, deals), (thin_watch, thin_stat, thin_deals)]


@pytest.fixture
def document():
    return dashboard.render_dashboard(build_rows(), as_of="2026-08-05T00:00:00Z")


# ---------------------------------------------------------------------------
# Self-containment
# ---------------------------------------------------------------------------


def test_dashboard_has_no_external_reference(document):
    lowered = document.lower()
    assert "http" not in lowered            # covers http:// and https:// and xmlns
    assert "<script src" not in lowered
    assert "<link" not in lowered
    assert "//cdn" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered


def test_dashboard_inlines_its_own_css_and_js(document):
    assert "<style>" in document and "</style>" in document
    assert "<script>" in document and "</script>" in document
    assert "--accent" in document


def test_dashboard_writes_to_disk(tmp_path):
    target = tmp_path / "dash.html"
    size = dashboard.write_dashboard(str(target), build_rows(), "2026-08-05T00:00:00Z")
    assert target.exists()
    assert size == len(target.read_text(encoding="utf-8").encode("utf-8"))
    assert size > 2000


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_dashboard_contains_the_watch_names(document):
    assert "Sony FE 35mm f/1.4 GM" in document
    assert "Fujifilm XF 56mm f/1.2 R" in document


def test_dashboard_shows_the_data_as_of_timestamp(document):
    assert "data as of 2026-08-05T00:00:00Z" in document


def test_dashboard_prints_comp_counts_next_to_numbers(document):
    assert "12 comps" in document
    assert ">comps<" in document
    assert "902.00 USD" in document          # the median of the twelve comps


def test_dashboard_publishes_a_refusal_for_a_thin_sample(document):
    assert "No band published" in document
    assert "insufficient data" in document
    assert "refuses to print a median it cannot defend" in document


def test_dashboard_escapes_listing_titles(document):
    assert "&lt;clean&gt; &amp; boxed" in document
    assert "<clean>" not in document


def test_dashboard_highlights_good_deals(document):
    assert 'class="good"' in document
    assert "under p25 of recent sold" in document


def test_empty_dashboard_is_still_valid(tmp_path):
    document = dashboard.render_dashboard([], as_of="")
    assert "no watches" in document
    assert "never synced" in document
    assert "http" not in document.lower()


# ---------------------------------------------------------------------------
# The box plot
# ---------------------------------------------------------------------------


def test_every_svg_parses_as_xml(document):
    svgs = SVG_RE.findall(document)
    assert len(svgs) == 2
    for markup in svgs:
        root = ET.fromstring(markup)
        assert root.tag == "svg"
        assert root.get("id")


def test_box_plot_geometry_is_sane(document):
    markup = SVG_RE.findall(document)[0]
    root = ET.fromstring(markup)
    box = root.find("./rect[@class='box']")
    median = root.find("./line[@class='median']")
    whisker = root.find("./line[@class='whisker']")
    caps = root.findall("./line[@class='cap']")
    assert box is not None and median is not None and whisker is not None
    assert len(caps) == 2

    left = float(box.get("x"))
    width = float(box.get("width"))
    right = left + width
    median_x = float(median.get("x1"))

    assert width > 0
    assert left <= median_x <= right, "median line must lie inside the box"
    assert float(median.get("x1")) == float(median.get("x2")), "median is vertical"

    # Whiskers reach at least as far as the box on both sides.
    assert float(whisker.get("x1")) <= left
    assert float(whisker.get("x2")) >= right
    assert float(whisker.get("y1")) == float(whisker.get("y2")), "whisker is horizontal"

    # The box sits inside the canvas.
    assert 0 <= left and right <= dashboard.PLOT_WIDTH


def test_box_plot_places_listings_as_dots(document):
    root = ET.fromstring(SVG_RE.findall(document)[0])
    dots = root.findall("./circle[@class='listing-dot']")
    assert len(dots) == 2
    xs = sorted(float(d.get("cx")) for d in dots)
    assert xs[0] < xs[1]


def test_box_plot_refuses_to_draw_an_insufficient_band():
    stat = stats.compute_stat(make_comps([600, 610]), currency="USD")
    markup = dashboard.box_plot_svg(stat, [], "plot-x")
    root = ET.fromstring(markup)
    assert root.find("./rect[@class='box']") is None
    assert root.find("./line[@class='median']") is None
    text = root.find("./text[@class='plot-empty']")
    assert text is not None
    assert "no band" in text.text


def test_box_plot_survives_a_zero_width_distribution():
    stat = stats.compute_stat(make_comps([500.0] * 8), currency="USD")
    markup = dashboard.box_plot_svg(stat, [500.0], "plot-flat")
    root = ET.fromstring(markup)
    box = root.find("./rect[@class='box']")
    assert box is not None
    assert float(box.get("width")) >= 1.0     # never zero, never negative
    median = root.find("./line[@class='median']")
    assert float(box.get("x")) <= float(median.get("x1")) <= float(
        box.get("x")
    ) + float(box.get("width"))
