"""Deal detection and alert rendering.

A deal is a live listing whose asking price sits low in that exact model's own
recent sold distribution. Not low against a list price, not low against a
"typical" price for the category, and never low against a band that does not
exist. If the band is insufficient the listing is reported as unscored with the
reason attached, which is the honest answer.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from . import stats
from .models import Condition, Deal, Listing, PriceStat, SoldComp, Watch

__all__ = [
    "DEFAULT_MIN_SCORE",
    "build_bands",
    "headline_band",
    "score_listings",
    "rank_deals",
    "render_deal_line",
    "render_alerts",
]

#: A listing has to be at or below roughly the 40th percentile of recent sold to
#: be worth a nudge. Configurable on the command line.
DEFAULT_MIN_SCORE = 60


def build_bands(
    comps: Sequence[SoldComp],
    watch: Watch,
    min_comps: int = stats.DEFAULT_MIN_COMPS,
    as_of: str = "",
) -> Dict[Condition, PriceStat]:
    """One :class:`PriceStat` per condition present, all in the watch currency."""
    buckets: Dict[Condition, List[SoldComp]] = {}
    for comp in comps:
        if comp.currency != watch.currency:
            continue
        buckets.setdefault(comp.condition, []).append(comp)
    return {
        condition: stats.compute_stat(
            group,
            model_key=watch.model_key,
            condition=condition,
            currency=watch.currency,
            min_comps=min_comps,
            as_of=as_of,
        )
        for condition, group in buckets.items()
    }


def headline_band(
    bands: Dict[Condition, PriceStat], watch: Watch, min_comps: int, as_of: str = ""
) -> PriceStat:
    """The band for the watch's target condition, or an explicit empty refusal."""
    existing = bands.get(watch.condition)
    if existing is not None:
        return existing
    return stats.compute_stat(
        [],
        model_key=watch.model_key,
        condition=watch.condition,
        currency=watch.currency,
        min_comps=min_comps,
        as_of=as_of,
    )


def score_listings(
    listings: Sequence[Listing],
    band: PriceStat,
    watch: Watch,
    min_comps: int = stats.DEFAULT_MIN_COMPS,
) -> List[Deal]:
    out: List[Deal] = []
    for listing in listings:
        deal = stats.score_listing(listing, band, min_comps=min_comps)
        under = (
            watch.max_price is not None
            and listing.currency == watch.currency
            and listing.price <= watch.max_price
        )
        out.append(replace(deal, under_max_price=bool(under)))
    return out


def rank_deals(deals: Sequence[Deal], min_score: int = DEFAULT_MIN_SCORE) -> List[Deal]:
    """Scored deals at or above ``min_score``, best first, ties broken by price."""
    qualifying = [d for d in deals if d.scored and d.score >= min_score]
    return sorted(qualifying, key=lambda d: (-d.score, d.listing.price, d.listing.item_id))


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "n/a"
    return "%.2f %s" % (value, currency)


def render_deal_line(deal: Deal, indent: str = "  ") -> str:
    listing = deal.listing
    if not deal.scored:
        head = "%s[--] %s  %s" % (
            indent,
            _money(listing.price, listing.currency),
            deal.verdict,
        )
    else:
        head = "%s[%3d] %s  %s  %s" % (
            indent,
            deal.score,
            _money(listing.price, listing.currency),
            listing.condition.value,
            deal.verdict,
        )
    flags = []
    if deal.under_max_price:
        flags.append("under your max price")
    if deal.is_good:
        flags.append("strong")
    tail = "  [%s]" % ", ".join(flags) if flags else ""
    title = listing.title if len(listing.title) <= 78 else listing.title[:75] + "..."
    return "%s%s\n%s      %s\n%s      item %s" % (
        head,
        tail,
        indent,
        title,
        indent,
        listing.item_id,
    )


def render_alerts(
    watch: Watch,
    band: PriceStat,
    deals: Sequence[Deal],
    min_score: int = DEFAULT_MIN_SCORE,
) -> str:
    """Human readable alert block for one watch."""
    lines: List[str] = []
    lines.append("[%s] %s" % (watch.id, watch.label))
    if band.sufficient:
        lines.append(
            "    band: p25 %s | median %s | p75 %s  (%s)"
            % (
                _money(band.p25, band.currency),
                _money(band.median, band.currency),
                _money(band.p75, band.currency),
                band.sample_note,
            )
        )
    else:
        lines.append("    band: NOT REPORTED - %s" % band.reason)
    ranked = rank_deals(deals, min_score)
    if not ranked:
        lines.append("    no live listing scored at or above %d" % min_score)
    for deal in ranked:
        lines.append(render_deal_line(deal, indent="    "))
    return "\n".join(lines)
