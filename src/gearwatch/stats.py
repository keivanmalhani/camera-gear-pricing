"""The pricing engine.

Everything here exists to answer one question honestly: given what this exact
model actually sold for recently, is a given asking price good?

Three properties are load bearing.

1. **It refuses.** Below ``min_comps`` completed sales (default 5) no band is
   emitted at all. A median of three is not a market, it is an anecdote, and
   printing it would be worse than printing nothing. ``PriceStat.sufficient``
   is False and every price field is None.
2. **It never discards silently.** Outliers outside 1.5 IQR are removed from the
   headline band because a single "lens only, no glass" fluke wrecks a median,
   but the count and the actual dropped prices are reported.
3. **It never emits a number without its sample size.** Every verdict string
   carries the comp count, and small samples are labelled as such.

Percentile definition: linear interpolation on the sorted sample at rank
``q * (n - 1)``. This is the same definition numpy uses by default, it agrees
with ``statistics.median`` for the 50th percentile, and it is easy to check by
hand, which is exactly what the tests do.
"""

from __future__ import annotations

import math
import statistics
from typing import List, Sequence, Tuple

from .models import Condition, Deal, Listing, PriceStat, SoldComp, Trend

__all__ = [
    "DEFAULT_MIN_COMPS",
    "DEFAULT_TRIM_FRACTION",
    "DEFAULT_IQR_MULTIPLIER",
    "THIN_DATA_COMPS",
    "percentile",
    "trimmed_mean",
    "iqr_fences",
    "split_outliers",
    "compute_trend",
    "compute_stat",
    "price_percentile",
    "deal_score",
    "verdict_for",
    "score_listing",
]

#: Fewer completed sales than this and gearwatch refuses to publish a band.
DEFAULT_MIN_COMPS = 5

#: Fraction trimmed from each tail for the trimmed mean.
DEFAULT_TRIM_FRACTION = 0.10

DEFAULT_IQR_MULTIPLIER = 1.5

#: At or below this many comps, every verdict is labelled as thin data.
THIN_DATA_COMPS = 10

#: A trend split needs at least this many comps in each half to be worth
#: printing without a "weak signal" label.
TREND_STRONG_HALF = 6


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile. ``q`` is a fraction in [0, 1]."""
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be within [0, 1], got %r" % (q,))
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    position = q * (n - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[int(position)])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def trimmed_mean(
    values: Sequence[float], fraction: float = DEFAULT_TRIM_FRACTION
) -> float:
    """Mean after dropping ``floor(n * fraction)`` values from each tail.

    With fewer than ``1 / fraction`` values the trim count is zero and this is
    simply the arithmetic mean. If a trim would leave nothing, it is skipped.
    """
    if not values:
        raise ValueError("trimmed mean of an empty sample is undefined")
    if not 0.0 <= fraction < 0.5:
        raise ValueError("fraction must be within [0, 0.5), got %r" % (fraction,))
    ordered = sorted(values)
    n = len(ordered)
    cut = int(math.floor(n * fraction))
    if n - 2 * cut < 1:
        cut = 0
    kept = ordered[cut : n - cut] if cut else ordered
    return float(sum(kept)) / float(len(kept))


def iqr_fences(
    values: Sequence[float], multiplier: float = DEFAULT_IQR_MULTIPLIER
) -> Tuple[float, float, float]:
    """Return ``(iqr, lower_fence, upper_fence)`` from the full sample."""
    if not values:
        raise ValueError("IQR of an empty sample is undefined")
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    iqr = q3 - q1
    return iqr, q1 - multiplier * iqr, q3 + multiplier * iqr


def split_outliers(
    values: Sequence[float], multiplier: float = DEFAULT_IQR_MULTIPLIER
) -> Tuple[List[float], List[float], float, float, float]:
    """Split into ``(kept, dropped, iqr, lower_fence, upper_fence)``.

    Fences are inclusive, so a sample of identical prices (IQR of zero) keeps
    every value instead of declaring the entire sample an outlier.
    """
    iqr, low, high = iqr_fences(values, multiplier)
    kept: List[float] = []
    dropped: List[float] = []
    for value in values:
        if value < low or value > high:
            dropped.append(float(value))
        else:
            kept.append(float(value))
    return kept, dropped, iqr, low, high


def compute_trend(comps: Sequence[SoldComp]) -> Trend:
    """Median of the later half minus median of the earlier half, by sale date.

    This is a coarse instrument and it is labelled as such. With an odd number
    of comps the middle sale is assigned to the later half.
    """
    usable = [c for c in comps if c.sold_at]
    if len(usable) < 4:
        return Trend(
            available=False,
            earlier_count=0,
            later_count=len(usable),
            weak=True,
            note="need at least 4 dated comps to split a trend",
        )
    ordered = sorted(usable, key=lambda c: (c.sold_at, c.item_id))
    cut = len(ordered) // 2
    earlier = ordered[:cut]
    later = ordered[cut:]
    earlier_median = float(statistics.median([c.price for c in earlier]))
    later_median = float(statistics.median([c.price for c in later]))
    delta = later_median - earlier_median
    pct = (delta / earlier_median * 100.0) if earlier_median else None
    weak = min(len(earlier), len(later)) < TREND_STRONG_HALF
    note = (
        "weak signal: only %d and %d comps per half"
        % (len(earlier), len(later))
        if weak
        else "%d and %d comps per half" % (len(earlier), len(later))
    )
    return Trend(
        available=True,
        earlier_count=len(earlier),
        later_count=len(later),
        earlier_median=earlier_median,
        later_median=later_median,
        delta=delta,
        pct_change=pct,
        weak=weak,
        note=note,
    )


def compute_stat(
    comps: Sequence[SoldComp],
    model_key: str = "",
    condition: Condition = Condition.UNKNOWN,
    currency: str = "USD",
    min_comps: int = DEFAULT_MIN_COMPS,
    trim_fraction: float = DEFAULT_TRIM_FRACTION,
    iqr_multiplier: float = DEFAULT_IQR_MULTIPLIER,
    as_of: str = "",
) -> PriceStat:
    """Build a :class:`PriceStat` from a set of sold comps.

    The caller is responsible for having already filtered ``comps`` to a single
    model, condition, and currency. This function does not convert currencies
    and will not guess.
    """
    prices = [float(c.price) for c in comps]
    raw_count = len(prices)

    if raw_count < min_comps:
        return PriceStat(
            model_key=model_key,
            condition=condition,
            currency=currency,
            sufficient=False,
            reason=(
                "insufficient data: %d completed sale%s, need at least %d"
                % (raw_count, "" if raw_count == 1 else "s", min_comps)
            ),
            raw_count=raw_count,
            count=raw_count,
            min_comps=min_comps,
            prices=tuple(sorted(prices)),
            trend=Trend(False, note="not computed: insufficient data"),
            as_of=as_of,
        )

    kept, dropped, iqr, low, high = split_outliers(prices, iqr_multiplier)

    if len(kept) < min_comps:
        return PriceStat(
            model_key=model_key,
            condition=condition,
            currency=currency,
            sufficient=False,
            reason=(
                "insufficient data after outlier removal: %d of %d comps survived "
                "the %.1f IQR fence, need at least %d"
                % (len(kept), raw_count, iqr_multiplier, min_comps)
            ),
            raw_count=raw_count,
            count=len(kept),
            iqr=iqr,
            lower_fence=low,
            upper_fence=high,
            outliers_dropped=len(dropped),
            outlier_prices=tuple(sorted(dropped)),
            min_comps=min_comps,
            prices=tuple(sorted(kept)),
            trend=Trend(False, note="not computed: insufficient data"),
            as_of=as_of,
        )

    dropped_set = list(dropped)
    kept_comps: List[SoldComp] = []
    for comp in comps:
        price = float(comp.price)
        if price in dropped_set:
            dropped_set.remove(price)
            continue
        kept_comps.append(comp)

    return PriceStat(
        model_key=model_key,
        condition=condition,
        currency=currency,
        sufficient=True,
        reason="ok",
        raw_count=raw_count,
        count=len(kept),
        minimum=float(min(kept)),
        p25=percentile(kept, 0.25),
        median=percentile(kept, 0.50),
        p75=percentile(kept, 0.75),
        maximum=float(max(kept)),
        trimmed_mean=trimmed_mean(kept, trim_fraction),
        iqr=iqr,
        lower_fence=low,
        upper_fence=high,
        outliers_dropped=len(dropped),
        outlier_prices=tuple(sorted(dropped)),
        min_comps=min_comps,
        prices=tuple(sorted(kept)),
        trend=compute_trend(kept_comps),
        as_of=as_of,
    )


def price_percentile(prices: Sequence[float], price: float) -> float:
    """Where ``price`` falls in ``prices``, 0-100, using the mid-rank method.

    * below every comp  -> 0.0
    * above every comp  -> 100.0
    * equal to the only distinct value -> 50.0 (no spread, no opinion)
    """
    if not prices:
        raise ValueError("cannot locate a price in an empty distribution")
    n = len(prices)
    below = sum(1 for p in prices if p < price)
    equal = sum(1 for p in prices if p == price)
    return (below + 0.5 * equal) / n * 100.0


def deal_score(pct: float) -> int:
    """Percentile inverted into a 0-100 score where higher is a better buy."""
    return int(round(100.0 - pct))


def verdict_for(pct: float, count: int, min_comps: int = DEFAULT_MIN_COMPS) -> str:
    """Plain-english verdict. Always ends with the sample size."""
    if pct < 25.0:
        band = "under p25 of recent sold"
    elif pct < 50.0:
        band = "between p25 and the median"
    elif pct == 50.0:
        band = "at the median"
    elif pct <= 75.0:
        band = "above the median"
    else:
        band = "above p75 of recent sold"
    text = "%s, %d comps" % (band, count)
    if count < min_comps:
        return "%s, insufficient data, do not trust this" % text
    if count <= THIN_DATA_COMPS:
        return "%s, thin data, treat with caution" % text
    return text


def score_listing(
    listing: Listing, stat: PriceStat, min_comps: int = DEFAULT_MIN_COMPS
) -> Deal:
    """Score one live listing against one band.

    A listing in a different currency than the band is never scored. Neither is
    a listing scored against an insufficient band: that is the entire point of
    the ``sufficient`` flag.
    """
    if not stat.sufficient or not stat.prices:
        return Deal(
            listing=listing,
            stat=stat,
            scored=False,
            score=0,
            percentile=None,
            verdict="not scored: %s" % stat.reason,
            under_max_price=False,
        )
    if listing.currency != stat.currency:
        return Deal(
            listing=listing,
            stat=stat,
            scored=False,
            score=0,
            percentile=None,
            verdict=(
                "not scored: listing is in %s, band is in %s, and gearwatch "
                "does not convert currencies" % (listing.currency, stat.currency)
            ),
            under_max_price=False,
        )
    pct = price_percentile(stat.prices, float(listing.price))
    verdict = verdict_for(pct, stat.count, min_comps)
    if (
        stat.condition is not Condition.UNKNOWN
        and listing.condition is not stat.condition
    ):
        # Comparing a like-new listing against an excellent-condition band is a
        # real comparison, but the reader has to be told which band it is.
        verdict += "; note this listing is graded %s and the band is %s" % (
            listing.condition.value,
            stat.condition.value,
        )
    return Deal(
        listing=listing,
        stat=stat,
        scored=True,
        score=deal_score(pct),
        percentile=pct,
        verdict=verdict,
        under_max_price=False,
    )
