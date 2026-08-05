"""Plain text reports.

Formatting rule enforced throughout: no price is ever printed without the
number of completed sales that produced it, and a band that does not exist is
printed as a refusal with its reason, never as a blank or a zero.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .models import CONDITION_ORDER, Condition, PriceStat, Watch

__all__ = [
    "money",
    "format_band",
    "format_watch_prices",
    "format_price_report",
    "format_watch_list",
    "format_sync_result",
]

RULE = "-" * 72


def money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "n/a"
    return "%.2f %s" % (value, currency)


def format_band(stat: PriceStat, indent: str = "    ") -> List[str]:
    lines: List[str] = []
    if not stat.sufficient:
        lines.append("%sband: NOT REPORTED - %s" % (indent, stat.reason))
        if stat.prices:
            lines.append(
                "%s      observed prices: %s"
                % (indent, ", ".join("%.2f" % p for p in stat.prices))
            )
        return lines

    lines.append(
        "%scomps: %d used of %d fetched in this condition and currency"
        % (indent, stat.count, stat.raw_count)
    )
    if stat.outliers_dropped:
        lines.append(
            "%soutliers: %d dropped outside the 1.5 IQR fence [%s .. %s]: %s"
            % (
                indent,
                stat.outliers_dropped,
                money(stat.lower_fence, stat.currency),
                money(stat.upper_fence, stat.currency),
                ", ".join("%.2f" % p for p in stat.outlier_prices),
            )
        )
    else:
        lines.append("%soutliers: none outside the 1.5 IQR fence" % indent)
    lines.append(
        "%sband:  min %s | p25 %s | median %s | p75 %s | max %s"
        % (
            indent,
            money(stat.minimum, stat.currency),
            money(stat.p25, stat.currency),
            money(stat.median, stat.currency),
            money(stat.p75, stat.currency),
            money(stat.maximum, stat.currency),
        )
    )
    lines.append(
        "%strimmed mean (10 percent off each tail): %s"
        % (indent, money(stat.trimmed_mean, stat.currency))
    )
    trend = stat.trend
    if trend.available and trend.delta is not None:
        lines.append(
            "%strend: %s %s (%+.1f%%) later half vs earlier half; %s"
            % (
                indent,
                trend.arrow,
                money(abs(trend.delta), stat.currency),
                trend.pct_change if trend.pct_change is not None else 0.0,
                trend.note,
            )
        )
    else:
        lines.append("%strend: not available - %s" % (indent, trend.note))
    return lines


def format_watch_prices(
    watch: Watch,
    bands: Dict[Condition, PriceStat],
    headline: PriceStat,
) -> List[str]:
    lines: List[str] = []
    lines.append("[%s] %s" % (watch.id, watch.query))
    lines.append(
        "    target: %s, at or below %s"
        % (
            watch.condition.value,
            money(watch.max_price, watch.currency)
            if watch.max_price is not None
            else "any price",
        )
    )
    lines.extend(format_band(headline))
    others = [
        (condition, bands[condition])
        for condition in CONDITION_ORDER
        if condition in bands and condition != watch.condition
    ]
    if others:
        lines.append("    other conditions seen:")
        for condition, stat in others:
            if stat.sufficient:
                lines.append(
                    "      %-10s median %s from %s"
                    % (condition.value, money(stat.median, stat.currency), stat.sample_note)
                )
            else:
                lines.append(
                    "      %-10s no band - %s" % (condition.value, stat.reason)
                )
    return lines


def format_price_report(
    blocks: Sequence[Sequence[str]], as_of: str, source_note: str = ""
) -> str:
    out: List[str] = ["gearwatch price bands", "data as of %s" % (as_of or "never synced")]
    if source_note:
        out.append(source_note)
    out.append(RULE)
    for index, block in enumerate(blocks):
        if index:
            out.append("")
        out.extend(block)
    if not blocks:
        out.append("no watches. add one with: gearwatch watch add \"...\"")
    out.append(RULE)
    out.append(
        "Sold prices are signal, asking prices are noise. Every number above is "
        "backed by the comp count printed beside it."
    )
    return "\n".join(out)


def format_watch_list(watches: Sequence[Watch]) -> str:
    if not watches:
        return "no watches yet. add one with: gearwatch watch add \"Sony FE 35mm f/1.4 GM\""
    lines = ["%-4s %-38s %-11s %-9s %s" % ("ID", "QUERY", "CONDITION", "MAX", "REQUIRED")]
    lines.append(RULE)
    for watch in watches:
        lines.append(
            "%-4s %-38s %-11s %-9s %s"
            % (
                watch.id,
                watch.query[:38],
                watch.condition.value,
                ("%.0f %s" % (watch.max_price, watch.currency))
                if watch.max_price is not None
                else "-",
                " ".join(watch.required_tokens),
            )
        )
    return "\n".join(lines)


def format_sync_result(result, source_name: str) -> str:
    counts = result.exclusion_counts()
    parts = [
        "[%s] %s" % (result.watch_id, result.query),
        "    source: %s, pages fetched: %d%s"
        % (
            source_name,
            result.pages_fetched,
            " (PAGE CAP HIT, data is partial)" if result.page_cap_hit else "",
        ),
        "    sold comps: %d seen, %d stored, %d new"
        % (result.comps_seen, result.comps_stored, result.comps_new),
        "    live listings: %d seen, %d stored, %d new"
        % (result.listings_seen, result.listings_stored, result.listings_new),
    ]
    if counts:
        parts.append(
            "    excluded: %s"
            % ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts))
        )
    else:
        parts.append("    excluded: none")
    return "\n".join(parts)
