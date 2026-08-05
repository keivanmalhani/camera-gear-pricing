"""The eBay adapter.

Two sources implement the same tiny interface:

* :class:`EbayApiSource` talks to the official eBay Browse API (live listings)
  and the official Marketplace Insights API (completed sales). Both are
  documented, authenticated, rate limited REST APIs. Nothing here parses HTML,
  loads a page, or pretends to be a browser.
* :class:`FixtureSource` replays canned API-shaped JSON from a file, so the
  whole pipeline, including pagination, runs with no credentials and no network
  at all. This is what the demo and the entire test suite use.

Normalisation rules worth knowing:

* Condition ids are mapped into the small internal :class:`Condition` vocabulary.
  An id we do not recognise becomes ``Condition.UNKNOWN``. It is stored, counted,
  and excluded from condition-specific bands. It never crashes a sync and it is
  never quietly folded into a real bucket.
* Currency is never converted. A comp priced in a currency other than the
  watch's currency is excluded and counted with the reason
  ``currency_mismatch``. Converting at today's rate a sale that happened six
  weeks ago would be inventing data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import match
from .auth import TokenProvider
from .db import utc_now
from .http import HttpClient, join_query
from .models import Condition, Exclusion, Listing, SoldComp, Watch

__all__ = [
    "CONDITION_BY_ID",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_MAX_PAGES",
    "EBAY_BROWSE_SEARCH",
    "EBAY_INSIGHTS_SEARCH",
    "map_condition",
    "parse_money",
    "FetchedPage",
    "EbayApiSource",
    "FixtureSource",
    "normalize_sold",
    "normalize_listing",
    "filter_comps",
    "filter_listings",
    "load_fixture",
]

EBAY_BROWSE_SEARCH = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_INSIGHTS_SEARCH = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)

DEFAULT_PAGE_SIZE = 50

#: Hard stop on pagination. Without this a broad query walks tens of thousands
#: of results and burns the daily API allowance in one run.
DEFAULT_MAX_PAGES = 5

#: eBay condition id -> internal bucket. Ids are strings in the API payloads.
CONDITION_BY_ID: Dict[str, Condition] = {
    "1000": Condition.NEW,               # New
    "1500": Condition.NEW,               # Open box
    "1750": Condition.LIKE_NEW,          # New with defects
    "2000": Condition.LIKE_NEW,          # Certified refurbished
    "2010": Condition.EXCELLENT,         # Excellent refurbished
    "2020": Condition.EXCELLENT,         # Very good refurbished
    "2030": Condition.GOOD,              # Good refurbished
    "2500": Condition.GOOD,              # Seller refurbished
    "2750": Condition.LIKE_NEW,          # Like new
    "3000": Condition.EXCELLENT,         # Used / excellent
    "4000": Condition.GOOD,              # Very good
    "5000": Condition.GOOD,              # Good
    "6000": Condition.FAIR,              # Acceptable
    "7000": Condition.PARTS,             # For parts or not working
}


def map_condition(condition_id: object) -> Condition:
    """Map an eBay condition id to the internal vocabulary.

    Unknown, missing, or malformed ids map to ``Condition.UNKNOWN``. That is the
    safe default: it keeps the record for auditing while keeping it out of every
    condition-specific price band.
    """
    if condition_id is None:
        return Condition.UNKNOWN
    key = str(condition_id).strip()
    return CONDITION_BY_ID.get(key, Condition.UNKNOWN)


def parse_money(node: object) -> Tuple[Optional[float], str]:
    """Pull ``(amount, currency)`` out of an eBay Amount node."""
    if not isinstance(node, dict):
        return None, ""
    raw_value = node.get("value")
    currency = str(node.get("currency") or "").strip().upper()
    if raw_value is None:
        return None, currency
    try:
        return float(raw_value), currency
    except (TypeError, ValueError):
        return None, currency


def _iso(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # eBay sends 2026-06-15T10:00:00.000Z; keep it sortable and second-precision.
    if text.endswith("Z") and "." in text:
        head, _, _tail = text.partition(".")
        return head + "Z"
    return text


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_sold(node: dict, marketplace: str = "") -> Optional[SoldComp]:
    """Turn one ``itemSales`` entry into a :class:`SoldComp`, or None if unusable."""
    item_id = str(node.get("itemId") or "").strip()
    title = str(node.get("title") or "").strip()
    amount, currency = parse_money(node.get("lastSoldPrice") or node.get("price"))
    if not item_id or amount is None or amount <= 0 or not currency:
        return None
    return SoldComp(
        item_id=item_id,
        title=title,
        price=amount,
        currency=currency,
        condition=map_condition(node.get("conditionId")),
        condition_id=str(node.get("conditionId") or ""),
        sold_at=_iso(node.get("lastSoldDate") or node.get("itemEndDate")),
        marketplace=marketplace or str(node.get("marketplaceId") or ""),
        url=str(node.get("itemWebUrl") or ""),
    )


def normalize_listing(node: dict, marketplace: str = "") -> Optional[Listing]:
    """Turn one ``itemSummaries`` entry into a :class:`Listing`, or None."""
    item_id = str(node.get("itemId") or "").strip()
    title = str(node.get("title") or "").strip()
    amount, currency = parse_money(node.get("price"))
    if not item_id or amount is None or amount <= 0 or not currency:
        return None
    seller_node = node.get("seller")
    seller = ""
    if isinstance(seller_node, dict):
        seller = str(seller_node.get("username") or "")
    return Listing(
        item_id=item_id,
        title=title,
        price=amount,
        currency=currency,
        condition=map_condition(node.get("conditionId")),
        condition_id=str(node.get("conditionId") or ""),
        seller=seller,
        listed_at=_iso(node.get("itemCreationDate")),
        marketplace=marketplace or str(node.get("itemLocation", {}).get("country", "")),
        url=str(node.get("itemWebUrl") or ""),
    )


def _negative_tokens(watch: Watch) -> Tuple[str, ...]:
    return tuple(match.DEFAULT_NEGATIVE_TOKENS) + tuple(watch.excluded_tokens)


def filter_comps(
    comps: Sequence[SoldComp], watch: Watch
) -> Tuple[List[SoldComp], List[Exclusion]]:
    """Apply currency and title rules, returning kept comps plus every exclusion."""
    kept: List[SoldComp] = []
    excluded: List[Exclusion] = []
    negatives = _negative_tokens(watch)
    for comp in comps:
        if comp.currency != watch.currency:
            excluded.append(
                Exclusion(
                    comp.item_id,
                    "currency_mismatch",
                    "comp priced in %s, watch tracks %s; gearwatch does not "
                    "convert currencies" % (comp.currency, watch.currency),
                )
            )
            continue
        result = match.match_title(
            comp.title, watch.required_tokens, watch.optional_tokens, negatives
        )
        if not result.matched:
            reason = "negative_token" if result.negative_hits else "title_mismatch"
            excluded.append(Exclusion(comp.item_id, reason, result.reason))
            continue
        if comp.condition is Condition.UNKNOWN:
            # Kept in storage for auditing, flagged so the report can say so.
            excluded.append(
                Exclusion(
                    comp.item_id,
                    "unknown_condition",
                    "condition id %r is not in the mapping table"
                    % (comp.condition_id,),
                )
            )
        kept.append(comp)
    return kept, excluded


def filter_listings(
    listings: Sequence[Listing], watch: Watch
) -> Tuple[List[Listing], List[Exclusion]]:
    kept: List[Listing] = []
    excluded: List[Exclusion] = []
    negatives = _negative_tokens(watch)
    for listing in listings:
        result = match.match_title(
            listing.title, watch.required_tokens, watch.optional_tokens, negatives
        )
        if not result.matched:
            reason = "negative_token" if result.negative_hits else "title_mismatch"
            excluded.append(Exclusion(listing.item_id, reason, result.reason))
            continue
        kept.append(listing)
    return kept, excluded


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchedPage:
    items: Tuple[dict, ...]
    total: int
    offset: int
    limit: int


class _BaseSource:
    """Shared pagination loop.

    Subclasses provide ``_sold_page`` / ``_live_page``; this walks offsets until
    the reported total is consumed, a page comes back empty, or the page cap is
    hit. ``page_cap_hit`` is surfaced so the report can say the data is partial
    rather than pretend it is complete.
    """

    name = "base"
    reference_time: str = ""

    def _sold_page(self, watch: Watch, offset: int, limit: int, days: int) -> FetchedPage:
        raise NotImplementedError

    def _live_page(self, watch: Watch, offset: int, limit: int) -> FetchedPage:
        raise NotImplementedError

    def _paginate(
        self,
        fetch: Callable[[int, int], FetchedPage],
        max_pages: int,
        page_size: int,
    ) -> Tuple[List[dict], int, bool]:
        collected: List[dict] = []
        pages = 0
        offset = 0
        cap_hit = False
        while True:
            if pages >= max_pages:
                cap_hit = True
                break
            page = fetch(offset, page_size)
            pages += 1
            collected.extend(page.items)
            if not page.items:
                break
            offset += max(1, len(page.items))
            if page.total and offset >= page.total:
                break
        return collected, pages, cap_hit

    def sold_comps(
        self,
        watch: Watch,
        days: int = 90,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Tuple[List[SoldComp], int, bool]:
        nodes, pages, cap_hit = self._paginate(
            lambda offset, limit: self._sold_page(watch, offset, limit, days),
            max_pages,
            page_size,
        )
        comps = [normalize_sold(node, watch.marketplace) for node in nodes]
        return [c for c in comps if c is not None], pages, cap_hit

    def live_listings(
        self,
        watch: Watch,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Tuple[List[Listing], int, bool]:
        nodes, pages, cap_hit = self._paginate(
            lambda offset, limit: self._live_page(watch, offset, limit),
            max_pages,
            page_size,
        )
        listings = [normalize_listing(node, watch.marketplace) for node in nodes]
        return [l for l in listings if l is not None], pages, cap_hit


class EbayApiSource(_BaseSource):
    """Live source. Requires an application token from :class:`TokenProvider`."""

    name = "ebay-api"

    def __init__(
        self,
        client: HttpClient,
        tokens: TokenProvider,
        browse_url: str = EBAY_BROWSE_SEARCH,
        insights_url: str = EBAY_INSIGHTS_SEARCH,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._tokens = tokens
        self.browse_url = browse_url
        self.insights_url = insights_url
        self._clock = clock
        self.reference_time = utc_now()

    def __repr__(self) -> str:
        return "EbayApiSource(browse_url=%r)" % (self.browse_url,)

    def _headers(self, watch: Watch) -> Dict[str, str]:
        headers = dict(self._tokens.authorization_header())
        headers["X-EBAY-C-MARKETPLACE-ID"] = watch.marketplace or "EBAY_US"
        headers["Accept"] = "application/json"
        return headers

    def _sold_page(self, watch: Watch, offset: int, limit: int, days: int) -> FetchedPage:
        end = self._clock()
        start = end - timedelta(days=max(1, days))
        window = "[%s..%s]" % (
            start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        )
        params = [
            ("q", watch.query),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("filter", "lastSoldDate:%s,priceCurrency:%s" % (window, watch.currency)),
        ]
        payload = self._client.get_json(
            join_query(self.insights_url, params), headers=self._headers(watch)
        )
        return FetchedPage(
            items=tuple(payload.get("itemSales") or ()),
            total=int(payload.get("total") or 0),
            offset=int(payload.get("offset") or offset),
            limit=int(payload.get("limit") or limit),
        )

    def _live_page(self, watch: Watch, offset: int, limit: int) -> FetchedPage:
        params = [
            ("q", watch.query),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("filter", "priceCurrency:%s,buyingOptions:{FIXED_PRICE}" % watch.currency),
        ]
        payload = self._client.get_json(
            join_query(self.browse_url, params), headers=self._headers(watch)
        )
        return FetchedPage(
            items=tuple(payload.get("itemSummaries") or ()),
            total=int(payload.get("total") or 0),
            offset=int(payload.get("offset") or offset),
            limit=int(payload.get("limit") or limit),
        )


def load_fixture(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class FixtureSource(_BaseSource):
    """Replays canned API-shaped JSON. No credentials, no network, no surprises.

    Fixture layout::

        {
          "generated_at": "2026-08-05T00:00:00Z",
          "marketplace": "EBAY_US",
          "queries": {
            "<normalised query>": {
              "sold_pages": [ {"total": N, "itemSales":   [...]}, ... ],
              "live_pages": [ {"total": N, "itemSummaries": [...]}, ... ]
            }
          }
        }

    Queries are looked up by :func:`gearwatch.match.model_key`, so the fixture
    matches a watch regardless of how the user spelled the query.
    """

    name = "fixture"

    def __init__(self, path: str) -> None:
        self.path = path
        if not os.path.exists(path):
            raise FileNotFoundError("fixture not found: %s" % path)
        self.data = load_fixture(path)
        self.reference_time = str(self.data.get("generated_at") or "")
        self._index: Dict[str, dict] = {}
        for raw_query, payload in (self.data.get("queries") or {}).items():
            self._index[match.model_key(raw_query)] = payload
        #: Pages actually requested, useful for asserting the cap in tests.
        self.page_requests: List[Tuple[str, int, int]] = []

    def __repr__(self) -> str:
        return "FixtureSource(path=%r, queries=%d)" % (self.path, len(self._index))

    def _entry(self, watch: Watch) -> dict:
        key = watch.model_key or match.model_key(watch.query)
        return self._index.get(key) or {}

    @staticmethod
    def _page(pages: Sequence[dict], index: int, key: str, offset: int, limit: int) -> FetchedPage:
        total = 0
        for page in pages:
            total = max(total, int(page.get("total") or 0))
        if total == 0:
            total = sum(len(page.get(key) or ()) for page in pages)
        if index >= len(pages):
            return FetchedPage(items=(), total=total, offset=offset, limit=limit)
        page = pages[index]
        return FetchedPage(
            items=tuple(page.get(key) or ()),
            total=int(page.get("total") or total),
            offset=offset,
            limit=limit,
        )

    def _sold_page(self, watch: Watch, offset: int, limit: int, days: int) -> FetchedPage:
        pages = self._entry(watch).get("sold_pages") or []
        index = len(self.page_requests_for("sold"))
        self.page_requests.append(("sold", offset, limit))
        return self._page(pages, index, "itemSales", offset, limit)

    def _live_page(self, watch: Watch, offset: int, limit: int) -> FetchedPage:
        pages = self._entry(watch).get("live_pages") or []
        index = len(self.page_requests_for("live"))
        self.page_requests.append(("live", offset, limit))
        return self._page(pages, index, "itemSummaries", offset, limit)

    def page_requests_for(self, kind: str) -> List[Tuple[str, int, int]]:
        return [req for req in self.page_requests if req[0] == kind]

    def reset_page_requests(self) -> None:
        self.page_requests = []

    # The pagination loop is shared, but fixture pages are per-watch, so reset
    # the page counter between watches.
    def sold_comps(self, watch: Watch, days: int = 90, max_pages: int = DEFAULT_MAX_PAGES, page_size: int = DEFAULT_PAGE_SIZE):
        self.reset_page_requests()
        comps, pages, cap_hit = super().sold_comps(watch, days, max_pages, page_size)
        return self._within_window(comps, days), pages, cap_hit

    def live_listings(self, watch: Watch, max_pages: int = DEFAULT_MAX_PAGES, page_size: int = DEFAULT_PAGE_SIZE):
        self.reset_page_requests()
        return super().live_listings(watch, max_pages, page_size)

    def _within_window(self, comps: Sequence[SoldComp], days: int) -> List[SoldComp]:
        """Apply the day window relative to the fixture's own snapshot time.

        Using wall-clock now would make a checked-in fixture silently expire.
        The fixture declares when it was captured and that is the reference.
        """
        reference = self.reference_time
        if not reference or days <= 0:
            return list(comps)
        try:
            end = datetime.strptime(reference[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return list(comps)
        start = end - timedelta(days=days)
        out: List[SoldComp] = []
        for comp in comps:
            if not comp.sold_at:
                out.append(comp)
                continue
            try:
                sold = datetime.strptime(comp.sold_at[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                out.append(comp)
                continue
            if start <= sold <= end:
                out.append(comp)
        return out
