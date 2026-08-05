"""A single self-contained HTML dashboard.

Constraints, all of which are asserted by the tests:

* One file. Inline CSS, inline JS, no CDN, no webfont, no analytics beacon, no
  external reference of any kind. Open it on a plane.
* The box plot is hand-computed inline SVG. There is no charting library, and
  the SVG carries no ``xmlns`` pointing at a remote schema, because inline SVG
  in HTML does not need one and the file must contain zero external references.
* Item links are deliberately omitted. A dashboard that phones out is not a
  self-contained dashboard, and the marketplace item id is enough to find a
  listing. This is a trade the README states plainly.
"""

from __future__ import annotations

import html
from typing import List, Optional, Sequence, Tuple

from .models import Deal, PriceStat, Watch

__all__ = ["render_dashboard", "write_dashboard", "box_plot_svg", "PLOT_WIDTH"]

PLOT_WIDTH = 720
PLOT_HEIGHT = 104
PLOT_LEFT = 56
PLOT_RIGHT = 40
PLOT_CENTER_Y = 46
BOX_HEIGHT = 34

_ACCENT = "#f0883e"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "n/a"
    return "%.2f %s" % (value, currency)


def _domain(stat: PriceStat, listing_prices: Sequence[float]) -> Tuple[float, float]:
    values: List[float] = []
    for candidate in (stat.minimum, stat.maximum):
        if candidate is not None:
            values.append(float(candidate))
    values.extend(float(p) for p in listing_prices)
    if not values:
        return 0.0, 1.0
    low = min(values)
    high = max(values)
    if high <= low:
        pad = max(1.0, abs(low) * 0.05)
        return low - pad, high + pad
    pad = (high - low) * 0.08
    return low - pad, high + pad


def box_plot_svg(
    stat: PriceStat,
    listing_prices: Sequence[float] = (),
    element_id: str = "plot",
) -> str:
    """Hand-drawn box plot: whiskers to min/max, box p25 to p75, median line.

    Returns a complete ``<svg>`` element. When the band is insufficient it
    returns an SVG that says so rather than drawing a misleading shape.
    """
    if not stat.sufficient or stat.p25 is None or stat.p75 is None or stat.median is None:
        return (
            '<svg class="plot" id="%s" width="%d" height="%d" viewBox="0 0 %d %d" '
            'role="img" aria-label="no band available">'
            '<rect class="plot-bg" x="0" y="0" width="%d" height="%d" />'
            '<text class="plot-empty" x="%d" y="%d">no band: %s</text>'
            "</svg>"
            % (
                _esc(element_id),
                PLOT_WIDTH,
                PLOT_HEIGHT,
                PLOT_WIDTH,
                PLOT_HEIGHT,
                PLOT_WIDTH,
                PLOT_HEIGHT,
                PLOT_LEFT,
                PLOT_CENTER_Y + 5,
                _esc(stat.reason),
            )
        )

    low, high = _domain(stat, listing_prices)
    span = high - low or 1.0
    usable = PLOT_WIDTH - PLOT_LEFT - PLOT_RIGHT

    def x_of(value: float) -> float:
        return PLOT_LEFT + (float(value) - low) / span * usable

    x_min = x_of(stat.minimum if stat.minimum is not None else low)
    x_max = x_of(stat.maximum if stat.maximum is not None else high)
    x_p25 = x_of(stat.p25)
    x_p75 = x_of(stat.p75)
    x_med = x_of(stat.median)
    box_width = max(1.0, x_p75 - x_p25)
    top = PLOT_CENTER_Y - BOX_HEIGHT / 2.0
    bottom = PLOT_CENTER_Y + BOX_HEIGHT / 2.0

    parts: List[str] = []
    parts.append(
        '<svg class="plot" id="%s" width="%d" height="%d" viewBox="0 0 %d %d" '
        'role="img" aria-label="sold price distribution, %d comps">'
        % (
            _esc(element_id),
            PLOT_WIDTH,
            PLOT_HEIGHT,
            PLOT_WIDTH,
            PLOT_HEIGHT,
            stat.count,
        )
    )
    parts.append(
        '<rect class="plot-bg" x="0" y="0" width="%d" height="%d" />'
        % (PLOT_WIDTH, PLOT_HEIGHT)
    )
    # Whisker spine plus caps.
    parts.append(
        '<line class="whisker" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" />'
        % (x_min, PLOT_CENTER_Y, x_max, PLOT_CENTER_Y)
    )
    parts.append(
        '<line class="cap" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" />'
        % (x_min, PLOT_CENTER_Y - 11, x_min, PLOT_CENTER_Y + 11)
    )
    parts.append(
        '<line class="cap" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" />'
        % (x_max, PLOT_CENTER_Y - 11, x_max, PLOT_CENTER_Y + 11)
    )
    # The interquartile box.
    parts.append(
        '<rect class="box" x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="2" />'
        % (x_p25, top, box_width, bottom - top)
    )
    # Median.
    parts.append(
        '<line class="median" x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" />'
        % (x_med, top, x_med, bottom)
    )
    # Live listings as dots under the axis.
    for index, price in enumerate(sorted(listing_prices)):
        parts.append(
            '<circle class="listing-dot" id="%s-dot-%d" cx="%.2f" cy="%.2f" r="4" />'
            % (_esc(element_id), index, x_of(price), PLOT_CENTER_Y + 30)
        )
    # Axis labels.
    parts.append(
        '<text class="tick" x="%.2f" y="%d" text-anchor="middle">%s</text>'
        % (x_min, PLOT_CENTER_Y - 20, _esc("%.0f" % stat.minimum))
    )
    parts.append(
        '<text class="tick" x="%.2f" y="%d" text-anchor="middle">%s</text>'
        % (x_med, PLOT_CENTER_Y - 20, _esc("%.0f" % stat.median))
    )
    parts.append(
        '<text class="tick" x="%.2f" y="%d" text-anchor="middle">%s</text>'
        % (x_max, PLOT_CENTER_Y - 20, _esc("%.0f" % stat.maximum))
    )
    parts.append(
        '<text class="axis-label" x="6" y="%d">%s</text>'
        % (PLOT_CENTER_Y + 4, _esc(stat.currency))
    )
    parts.append("</svg>")
    return "".join(parts)


_CSS = """
:root {
  --bg: #0d1117;
  --panel: #161b22;
  --panel-2: #1c2129;
  --line: #30363d;
  --text: #c9d1d9;
  --muted: #8b949e;
  --accent: %(accent)s;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 14px;
  line-height: 1.5;
}
header {
  padding: 28px 32px 18px;
  border-bottom: 1px solid var(--line);
}
h1 { margin: 0 0 6px; font-size: 20px; letter-spacing: 0.02em; }
h1 .mark { color: var(--accent); }
.sub { color: var(--muted); font-size: 12px; }
main { padding: 24px 32px 60px; max-width: 1080px; }
.watch {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 22px;
}
.watch h2 { margin: 0 0 2px; font-size: 15px; font-weight: 600; }
.meta { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
.badge {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 1px 9px;
  margin-right: 6px;
  font-size: 11px;
  color: var(--muted);
}
.badge.accent { color: var(--accent); border-color: var(--accent); }
.refusal {
  border-left: 3px solid var(--accent);
  background: var(--panel-2);
  padding: 8px 12px;
  color: var(--muted);
  font-size: 12px;
  margin: 8px 0 12px;
}
table { width: 100%%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.good td { background: rgba(240, 136, 62, 0.10); }
tr.good td.score { color: var(--accent); font-weight: 700; }
.title { color: var(--text); }
.dim { color: var(--muted); }
.stats { display: flex; flex-wrap: wrap; gap: 18px; margin: 6px 0 4px; }
.stat .k { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.stat .v { font-size: 15px; font-variant-numeric: tabular-nums; }
.plot { display: block; margin: 2px 0 6px; }
.plot-bg { fill: var(--panel-2); }
.whisker { stroke: var(--muted); stroke-width: 1.5; }
.cap { stroke: var(--muted); stroke-width: 1.5; }
.box { fill: rgba(240, 136, 62, 0.18); stroke: var(--accent); stroke-width: 1.5; }
.median { stroke: var(--accent); stroke-width: 3; }
.listing-dot { fill: var(--text); opacity: 0.75; }
.tick { fill: var(--muted); font-size: 10px; }
.axis-label { fill: var(--muted); font-size: 10px; }
.plot-empty { fill: var(--muted); font-size: 12px; }
footer { color: var(--muted); font-size: 11px; padding: 0 32px 40px; max-width: 1080px; }
""" % {"accent": _ACCENT}

_JS = """
(function () {
  var toggles = document.querySelectorAll('[data-toggle]');
  for (var i = 0; i < toggles.length; i++) {
    toggles[i].addEventListener('click', function (event) {
      var id = event.currentTarget.getAttribute('data-toggle');
      var node = document.getElementById(id);
      if (!node) { return; }
      node.hidden = !node.hidden;
    });
  }
})();
"""


def _trend_text(stat: PriceStat) -> str:
    trend = stat.trend
    if not trend.available or trend.delta is None:
        return "trend unavailable (%s)" % trend.note
    arrow = {"up": "up", "down": "down", "flat": "flat"}.get(trend.arrow, "?")
    text = "%s %.2f %s (%+.1f%%) later half vs earlier" % (
        arrow,
        abs(trend.delta),
        stat.currency,
        trend.pct_change or 0.0,
    )
    if trend.weak:
        text += " - WEAK SIGNAL, %s" % trend.note
    return text


def _render_watch(
    watch: Watch, stat: PriceStat, deals: Sequence[Deal], index: int
) -> str:
    plot_id = "plot-%s" % (watch.id if watch.id is not None else index)
    detail_id = "detail-%s" % (watch.id if watch.id is not None else index)
    listing_prices = [
        float(d.listing.price)
        for d in deals
        if d.listing.currency == watch.currency
    ]

    out: List[str] = ['<section class="watch">']
    out.append("<h2>%s</h2>" % _esc(watch.query))
    out.append(
        '<div class="meta">'
        '<span class="badge">%s</span>'
        '<span class="badge">%s</span>'
        '<span class="badge">max %s</span>'
        '<span class="badge accent">%s</span>'
        "</div>"
        % (
            _esc(watch.condition.value),
            _esc(watch.currency),
            _esc(
                "%.0f" % watch.max_price if watch.max_price is not None else "any"
            ),
            _esc(stat.sample_note if stat.sufficient else "no band"),
        )
    )

    if stat.sufficient:
        out.append('<div class="stats">')
        for key, value in (
            ("p25", stat.p25),
            ("median", stat.median),
            ("p75", stat.p75),
            ("trimmed mean", stat.trimmed_mean),
        ):
            out.append(
                '<div class="stat"><div class="k">%s</div>'
                '<div class="v">%s</div></div>'
                % (_esc(key), _esc(_money(value, stat.currency)))
            )
        out.append(
            '<div class="stat"><div class="k">comps</div>'
            '<div class="v">%d</div></div>' % stat.count
        )
        out.append("</div>")
        out.append(box_plot_svg(stat, listing_prices, plot_id))
        out.append(
            '<div class="dim">whiskers min %s to max %s, box p25 to p75, '
            "line at the median, dots are live listings. %s</div>"
            % (
                _esc(_money(stat.minimum, stat.currency)),
                _esc(_money(stat.maximum, stat.currency)),
                _esc(_trend_text(stat)),
            )
        )
        if stat.outliers_dropped:
            out.append(
                '<div class="refusal">%d outlier%s dropped outside the 1.5 IQR '
                "fence before computing this band: %s. Nothing is discarded "
                "silently.</div>"
                % (
                    stat.outliers_dropped,
                    "" if stat.outliers_dropped == 1 else "s",
                    _esc(", ".join("%.2f" % p for p in stat.outlier_prices)),
                )
            )
    else:
        out.append(box_plot_svg(stat, listing_prices, plot_id))
        out.append(
            '<div class="refusal">No band published. %s. gearwatch refuses to '
            "print a median it cannot defend.</div>" % _esc(stat.reason)
        )

    scored = sorted(
        deals, key=lambda d: (not d.scored, -d.score, d.listing.price)
    )
    if scored:
        out.append(
            '<button class="badge" data-toggle="%s">toggle listings (%d)</button>'
            % (_esc(detail_id), len(scored))
        )
        out.append('<div id="%s">' % _esc(detail_id))
        out.append(
            "<table><thead><tr>"
            "<th>score</th><th>price</th><th>condition</th>"
            "<th>verdict</th><th>title</th><th>item id</th>"
            "</tr></thead><tbody>"
        )
        for deal in scored:
            good = " class=\"good\"" if deal.is_good else ""
            score_text = "%d" % deal.score if deal.scored else "--"
            out.append(
                "<tr%s><td class=\"num score\">%s</td>"
                '<td class="num">%s</td><td>%s</td>'
                '<td class="dim">%s</td><td class="title">%s</td>'
                '<td class="dim">%s</td></tr>'
                % (
                    good,
                    _esc(score_text),
                    _esc(_money(deal.listing.price, deal.listing.currency)),
                    _esc(deal.listing.condition.value),
                    _esc(deal.verdict),
                    _esc(deal.listing.title),
                    _esc(deal.listing.item_id),
                )
            )
        out.append("</tbody></table>")
        out.append("</div>")
    else:
        out.append('<div class="dim">no live listings stored for this watch</div>')

    out.append("</section>")
    return "".join(out)


def render_dashboard(
    rows: Sequence[Tuple[Watch, PriceStat, Sequence[Deal]]],
    as_of: str,
    source_note: str = "",
) -> str:
    """Build the whole document as one string."""
    body: List[str] = []
    for index, (watch, stat, deals) in enumerate(rows):
        body.append(_render_watch(watch, stat, deals, index))
    if not body:
        body.append(
            '<section class="watch"><h2>no watches</h2>'
            '<div class="dim">add one with: gearwatch watch add "Sony FE 35mm f/1.4 GM"'
            "</div></section>"
        )
    total_comps = sum(stat.count for _w, stat, _d in rows)
    total_listings = sum(len(deals) for _w, _s, deals in rows)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>gearwatch</title>\n"
        "<style>%s</style>\n</head>\n<body>\n"
        "<header>\n"
        '<h1><span class="mark">gearwatch</span> sold-price bands</h1>\n'
        '<div class="sub">data as of %s | %d watches | %d comps in the published '
        "bands | %d live listings | official APIs only, no scraping%s</div>\n"
        "</header>\n<main>\n%s\n</main>\n"
        "<footer>Every price on this page is accompanied by the number of "
        "completed sales behind it. Bands below the minimum comp count are not "
        "published at all. This file is self-contained and makes no network "
        "requests.</footer>\n"
        "<script>%s</script>\n</body>\n</html>\n"
        % (
            _CSS,
            _esc(as_of or "never synced"),
            len(rows),
            total_comps,
            total_listings,
            (" | " + _esc(source_note)) if source_note else "",
            "\n".join(body),
            _JS,
        )
    )


def write_dashboard(
    path: str,
    rows: Sequence[Tuple[Watch, PriceStat, Sequence[Deal]]],
    as_of: str,
    source_note: str = "",
) -> int:
    document = render_dashboard(rows, as_of, source_note)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)
    return len(document.encode("utf-8"))
