"""Core value objects for gearwatch.

Everything that crosses a module boundary is one of these frozen dataclasses.
They carry no behaviour beyond trivial derivations so that the adapter, the
storage layer, and the pricing engine cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

__all__ = [
    "Condition",
    "Watch",
    "SoldComp",
    "Listing",
    "PriceStat",
    "Trend",
    "Deal",
    "Exclusion",
    "SyncResult",
    "CONDITION_ORDER",
]


class Condition(str, Enum):
    """Internal, deliberately small condition vocabulary.

    Marketplace condition taxonomies are large, inconsistent, and
    seller-reported. We collapse them into six buckets plus UNKNOWN. UNKNOWN is
    a first class value, not an error: an unmapped marketplace condition id must
    never crash a sync and must never be silently folded into a real bucket.
    """

    NEW = "new"
    LIKE_NEW = "like_new"
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    PARTS = "parts"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> "Condition":
        """Parse a user supplied condition name, tolerating dashes and case."""
        key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
        for member in cls:
            if member.value == key:
                return member
        raise ValueError(
            "unknown condition %r (expected one of: %s)"
            % (raw, ", ".join(m.value for m in cls))
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Best to worst. Used for stable ordering in reports.
CONDITION_ORDER: Tuple[Condition, ...] = (
    Condition.NEW,
    Condition.LIKE_NEW,
    Condition.EXCELLENT,
    Condition.GOOD,
    Condition.FAIR,
    Condition.PARTS,
    Condition.UNKNOWN,
)


@dataclass(frozen=True)
class Watch:
    """A thing the user is hunting for."""

    query: str
    max_price: Optional[float] = None
    condition: Condition = Condition.EXCELLENT
    currency: str = "USD"
    marketplace: str = "EBAY_US"
    required_tokens: Tuple[str, ...] = ()
    optional_tokens: Tuple[str, ...] = ()
    excluded_tokens: Tuple[str, ...] = ()
    model_key: str = ""
    created_at: str = ""
    id: Optional[int] = None

    @property
    def label(self) -> str:
        return "%s (%s, %s)" % (self.query, self.condition.value, self.currency)


@dataclass(frozen=True)
class SoldComp:
    """One completed sale, as reported by the marketplace API."""

    item_id: str
    title: str
    price: float
    currency: str
    condition: Condition
    sold_at: str
    marketplace: str = ""
    condition_id: str = ""
    url: str = ""
    watch_id: Optional[int] = None
    fetched_at: str = ""


@dataclass(frozen=True)
class Listing:
    """One live listing, as reported by the marketplace API."""

    item_id: str
    title: str
    price: float
    currency: str
    condition: Condition
    seller: str = ""
    listed_at: str = ""
    marketplace: str = ""
    condition_id: str = ""
    url: str = ""
    watch_id: Optional[int] = None
    seen_at: str = ""
    active: bool = True


@dataclass(frozen=True)
class Trend:
    """Direction of travel, split by sale time into an earlier and later half."""

    available: bool
    earlier_count: int = 0
    later_count: int = 0
    earlier_median: Optional[float] = None
    later_median: Optional[float] = None
    delta: Optional[float] = None
    pct_change: Optional[float] = None
    weak: bool = True
    note: str = "not computed"

    @property
    def arrow(self) -> str:
        if not self.available or self.delta is None:
            return "?"
        if self.delta > 0:
            return "up"
        if self.delta < 0:
            return "down"
        return "flat"


@dataclass(frozen=True)
class PriceStat:
    """The headline result of the pricing engine.

    ``sufficient`` is the single most important field in this program. When it
    is False there is no band: minimum/p25/median/p75/maximum are all None and
    ``reason`` explains why. A caller that renders a number without checking
    this flag is lying to the user.
    """

    model_key: str
    condition: Condition
    currency: str
    sufficient: bool
    reason: str
    raw_count: int
    count: int
    minimum: Optional[float] = None
    p25: Optional[float] = None
    median: Optional[float] = None
    p75: Optional[float] = None
    maximum: Optional[float] = None
    trimmed_mean: Optional[float] = None
    iqr: Optional[float] = None
    lower_fence: Optional[float] = None
    upper_fence: Optional[float] = None
    outliers_dropped: int = 0
    outlier_prices: Tuple[float, ...] = ()
    min_comps: int = 5
    prices: Tuple[float, ...] = ()
    trend: Trend = field(default_factory=lambda: Trend(False))
    as_of: str = ""

    @property
    def spread(self) -> Optional[float]:
        if self.p25 is None or self.p75 is None:
            return None
        return self.p75 - self.p25

    @property
    def sample_note(self) -> str:
        """Never show a price without this next to it."""
        if self.outliers_dropped:
            return "%d comps (%d outlier%s dropped)" % (
                self.count,
                self.outliers_dropped,
                "" if self.outliers_dropped == 1 else "s",
            )
        return "%d comps" % self.count


@dataclass(frozen=True)
class Deal:
    """A live listing scored against a sold-price band."""

    listing: Listing
    stat: PriceStat
    scored: bool
    score: int
    percentile: Optional[float]
    verdict: str
    under_max_price: bool = False

    @property
    def is_good(self) -> bool:
        return self.scored and self.score >= 75


@dataclass(frozen=True)
class Exclusion:
    """Why a record from the API did not make it into the band.

    Nothing is ever discarded silently. Every exclusion is counted and every
    count is reported.
    """

    item_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SyncResult:
    watch_id: int
    query: str
    comps_seen: int = 0
    comps_stored: int = 0
    comps_new: int = 0
    listings_seen: int = 0
    listings_stored: int = 0
    listings_new: int = 0
    pages_fetched: int = 0
    page_cap_hit: bool = False
    exclusions: Tuple[Exclusion, ...] = ()
    started_at: str = ""
    finished_at: str = ""

    def exclusion_counts(self) -> dict:
        counts: dict = {}
        for exc in self.exclusions:
            counts[exc.reason] = counts.get(exc.reason, 0) + 1
        return counts
