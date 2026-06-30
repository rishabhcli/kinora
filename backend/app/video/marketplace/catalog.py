"""The searchable / filterable / rankable in-memory catalog of listings.

:class:`ModelCatalog` is the read model: an ordered registry of
:class:`~app.video.marketplace.listing.ModelListing` keyed by ``key``. It is
**in-memory and self-contained** (no DB, no network) so it is trivially
deterministic to test and cheap to serve read-only.

The public surface is three operations:

* :meth:`ModelCatalog.upsert` / :meth:`ModelCatalog.get` / :meth:`ModelCatalog.remove`
  — registry maintenance (used by the onboarding + lifecycle layers).
* :meth:`ModelCatalog.search` — filter by a :class:`CatalogQuery` (capability,
  modality, region, price ceiling, maturity floor, license, status, free-text)
  then rank by a deterministic, explainable score.
* :meth:`ModelCatalog.compare` — a structured side-by-side of two listings.

Ranking is a transparent weighted sum (see :func:`score_listing`) so a curator
can understand *why* one model outranks another and tests can assert exact
orderings. Ties break by ``key`` for total determinism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from app.video.marketplace.errors import ListingNotFoundError
from app.video.marketplace.listing import ModelListing
from app.video.marketplace.types import (
    Capability,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    Region,
)


class CatalogQuery(BaseModel):
    """A declarative filter+rank request over the catalog.

    Every field is optional; an empty query returns all default-visible
    listings ranked by the default weights. Filters are conjunctive (AND).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = Field(default=None, max_length=256)
    modality: Modality | None = None
    #: All listed capabilities must be present (AND).
    capabilities: tuple[Capability, ...] = ()
    region: Region | None = None
    #: Upper bound on cheapest per-second price (USD); listings above are dropped.
    max_price_per_second_usd: float | None = Field(default=None, ge=0.0)
    #: Minimum maturity grade (e.g. only BETA+).
    min_maturity: Maturity | None = None
    license_class: LicenseClass | None = None
    #: Restrict to these statuses; default = every default-visible status.
    statuses: tuple[ListingStatus, ...] = ()
    #: Include RETIRED listings (hidden by default).
    include_retired: bool = False
    #: Minimum duration ceiling the model must support (seconds).
    min_duration_s: float | None = Field(default=None, gt=0.0)
    limit: int = Field(default=50, ge=1, le=500)


@dataclass(frozen=True)
class RankWeights:
    """Explainable ranking weights (a transparent weighted sum).

    Each component contributes a 0..1 normalized signal scaled by its weight:

    * ``reputation`` — measured quality score (0 if unmeasured).
    * ``maturity`` — release-stability grade / 3.
    * ``price`` — inverse of cheapest per-second price (cheaper ranks higher).
    * ``capability`` — fraction of the *query's* requested capabilities met
      (1.0 when the query asks for none).
    * ``freshness`` — newer ``updated_at`` ranks marginally higher.
    """

    reputation: float = 0.40
    maturity: float = 0.20
    price: float = 0.20
    capability: float = 0.15
    freshness: float = 0.05


@dataclass(frozen=True)
class ScoredListing:
    """A listing plus its computed score and a human-readable breakdown."""

    listing: ModelListing
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


# Reference price used to normalize the inverse-price signal into 0..1.
# A model at $0/s scores 1.0; a model at >= this ceiling scores ~0.
_PRICE_REFERENCE_USD_PER_S = 0.50


def _price_signal(listing: ModelListing) -> float:
    """Normalize cheapest per-second price into a 0..1 'cheaper is better' signal."""
    per_s = listing.cheapest_per_second_usd
    if per_s is None:
        # no per-second pricing => neutral (not penalized, not rewarded)
        return 0.5
    if per_s <= 0.0:
        return 1.0
    # linear decay to 0 at the reference ceiling, clamped
    return max(0.0, 1.0 - per_s / _PRICE_REFERENCE_USD_PER_S)


def _capability_signal(listing: ModelListing, requested: tuple[Capability, ...]) -> float:
    """Fraction of the query's requested capabilities the listing advertises."""
    if not requested:
        return 1.0
    have = sum(1 for c in requested if c in listing.capabilities)
    return have / len(requested)


def _freshness_signal(listing: ModelListing, *, newest_ts: float, oldest_ts: float) -> float:
    """Normalize ``updated_at`` into 0..1 across the candidate set."""
    span = newest_ts - oldest_ts
    if span <= 0:
        return 1.0
    return (listing.updated_at.timestamp() - oldest_ts) / span


def score_listing(
    listing: ModelListing,
    *,
    requested_capabilities: tuple[Capability, ...] = (),
    weights: RankWeights | None = None,
    newest_ts: float,
    oldest_ts: float,
) -> ScoredListing:
    """Compute the transparent weighted-sum score for one listing.

    Returns the listing wrapped with its total score and a per-component
    ``breakdown`` (each already weight-scaled) so the result is fully
    explainable. Pure & deterministic given the timestamp bounds.
    """
    w = weights or RankWeights()
    rep = listing.reputation.score if listing.reputation else 0.0
    mat = listing.maturity.grade / 3.0
    price = _price_signal(listing)
    cap = _capability_signal(listing, requested_capabilities)
    fresh = _freshness_signal(listing, newest_ts=newest_ts, oldest_ts=oldest_ts)

    breakdown = {
        "reputation": round(w.reputation * rep, 6),
        "maturity": round(w.maturity * mat, 6),
        "price": round(w.price * price, 6),
        "capability": round(w.capability * cap, 6),
        "freshness": round(w.freshness * fresh, 6),
    }
    total = round(sum(breakdown.values()), 6)
    return ScoredListing(listing=listing, score=total, breakdown=breakdown)


class ComparisonRow(BaseModel):
    """One attribute compared across two listings."""

    model_config = ConfigDict(frozen=True)

    attribute: str
    left: object | None = None
    right: object | None = None
    #: 'left' | 'right' | 'tie' — which listing is preferable on this attribute
    #: (only set for attributes with a meaningful preference order).
    prefer: str | None = None


class Comparison(BaseModel):
    """A structured side-by-side comparison of two listings."""

    model_config = ConfigDict(frozen=True)

    left_key: str
    right_key: str
    rows: tuple[ComparisonRow, ...]
    #: Overall recommendation derived from per-row preferences ('left'|'right'|'tie').
    recommendation: str


class ModelCatalog:
    """An ordered, in-memory registry of listings with search/rank/compare.

    Insertion order is preserved (it is the stable tie-context for equal scores,
    though final tie-break is by ``key``). Not thread-safe by design — the API
    layer builds a fresh catalog per process from the seed; concurrent writes
    are not part of the read-only contract.
    """

    def __init__(self, listings: list[ModelListing] | None = None) -> None:
        self._by_key: dict[str, ModelListing] = {}
        for listing in listings or []:
            self._by_key[listing.key] = listing

    # ----------------------------- registry ----------------------------- #
    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def keys(self) -> list[str]:
        return list(self._by_key.keys())

    def all(self) -> list[ModelListing]:
        """Every listing in insertion order (including retired)."""
        return list(self._by_key.values())

    def get(self, key: str) -> ModelListing:
        """Fetch a listing by key or raise :class:`ListingNotFoundError`."""
        try:
            return self._by_key[key]
        except KeyError:
            raise ListingNotFoundError(f"no listing with key {key!r}") from None

    def upsert(self, listing: ModelListing) -> None:
        """Insert or replace a listing by its ``key`` (preserves position on update)."""
        self._by_key[listing.key] = listing

    def remove(self, key: str) -> ModelListing:
        """Remove and return a listing, or raise if absent."""
        try:
            return self._by_key.pop(key)
        except KeyError:
            raise ListingNotFoundError(f"no listing with key {key!r}") from None

    # ------------------------------ search ------------------------------ #
    def _matches(self, listing: ModelListing, q: CatalogQuery) -> bool:
        # status / visibility
        if q.statuses:
            if listing.status not in q.statuses:
                return False
        elif not q.include_retired and not listing.status.is_visible_by_default:
            return False

        if q.modality is not None and not listing.supports(modality=q.modality):
            return False

        for cap in q.capabilities:
            if cap not in listing.capabilities:
                return False

        if q.region is not None and not listing.region.serves(q.region):
            return False

        if q.max_price_per_second_usd is not None:
            per_s = listing.cheapest_per_second_usd
            # a listing with no per-second pricing cannot satisfy a per-second ceiling
            if per_s is None or per_s > q.max_price_per_second_usd:
                return False

        if q.min_maturity is not None and listing.maturity.grade < q.min_maturity.grade:
            return False

        if q.license_class is not None and listing.license_class != q.license_class:
            return False

        if q.min_duration_s is not None and listing.max_duration_s < q.min_duration_s:
            return False

        if q.text:
            needle = q.text.lower()
            haystack = " ".join(
                [
                    listing.key,
                    listing.provider,
                    listing.model_id,
                    listing.display_name,
                    listing.summary,
                    " ".join(listing.tags),
                ]
            ).lower()
            if needle not in haystack:
                return False

        return True

    def search(
        self, query: CatalogQuery | None = None, *, weights: RankWeights | None = None
    ) -> list[ScoredListing]:
        """Filter by ``query`` then return scored listings, best first.

        Determinism: results are sorted by descending score, then ascending
        ``key`` to break ties. The score breakdown is attached for explainability.
        """
        q = query or CatalogQuery()
        candidates = [li for li in self._by_key.values() if self._matches(li, q)]
        if not candidates:
            return []

        timestamps = [li.updated_at.timestamp() for li in candidates]
        newest_ts, oldest_ts = max(timestamps), min(timestamps)

        scored = [
            score_listing(
                li,
                requested_capabilities=q.capabilities,
                weights=weights,
                newest_ts=newest_ts,
                oldest_ts=oldest_ts,
            )
            for li in candidates
        ]
        scored.sort(key=lambda s: (-s.score, s.listing.key))
        return scored[: q.limit]

    # ----------------------------- compare ------------------------------ #
    def compare(self, left_key: str, right_key: str) -> Comparison:
        """Build a structured side-by-side of two listings (raises if either absent)."""
        left = self.get(left_key)
        right = self.get(right_key)
        rows: list[ComparisonRow] = []

        def _row(
            attr: str, lv: object | None, rv: object | None, prefer: str | None = None
        ) -> None:
            rows.append(ComparisonRow(attribute=attr, left=lv, right=rv, prefer=prefer))

        _row("provider", left.provider, right.provider)
        _row("version", left.version, right.version)
        _row("status", left.status.value, right.status.value)

        # maturity: higher grade preferred
        _row(
            "maturity",
            left.maturity.value,
            right.maturity.value,
            _prefer_by(left.maturity.grade, right.maturity.grade, higher_is_better=True),
        )

        # price: lower per-second preferred (None treated as worst)
        lp, rp = left.cheapest_per_second_usd, right.cheapest_per_second_usd
        _row(
            "price_per_second_usd",
            lp,
            rp,
            _prefer_price(lp, rp),
        )

        # reputation: higher preferred (None = 0)
        lr = left.reputation.score if left.reputation else None
        rr = right.reputation.score if right.reputation else None
        _row(
            "reputation",
            lr,
            rr,
            _prefer_by((lr or 0.0), (rr or 0.0), higher_is_better=True),
        )

        # duration ceiling: higher preferred
        _row(
            "max_duration_s",
            left.max_duration_s,
            right.max_duration_s,
            _prefer_by(left.max_duration_s, right.max_duration_s, higher_is_better=True),
        )

        # capabilities: the superset is preferred (count-based)
        _row(
            "capabilities",
            sorted(c.value for c in left.capabilities),
            sorted(c.value for c in right.capabilities),
            _prefer_by(len(left.capabilities), len(right.capabilities), higher_is_better=True),
        )

        # license: commercial_ok preferred over the rest
        _row(
            "license_class",
            left.license_class.value,
            right.license_class.value,
            _prefer_by(
                int(left.license_class.commercial_safe),
                int(right.license_class.commercial_safe),
                higher_is_better=True,
            ),
        )

        recommendation = _aggregate_recommendation(rows)
        return Comparison(
            left_key=left_key,
            right_key=right_key,
            rows=tuple(rows),
            recommendation=recommendation,
        )


def _prefer_by(left: float, right: float, *, higher_is_better: bool) -> str:
    if left == right:
        return "tie"
    if higher_is_better:
        return "left" if left > right else "right"
    return "left" if left < right else "right"


def _prefer_price(lp: float | None, rp: float | None) -> str:
    # lower is better; a missing per-second price is treated as worse than any price
    if lp is None and rp is None:
        return "tie"
    if lp is None:
        return "right"
    if rp is None:
        return "left"
    return _prefer_by(lp, rp, higher_is_better=False)


def _aggregate_recommendation(rows: list[ComparisonRow]) -> str:
    """Majority of per-row preferences; ties resolve to 'tie'."""
    left = sum(1 for r in rows if r.prefer == "left")
    right = sum(1 for r in rows if r.prefer == "right")
    if left > right:
        return "left"
    if right > left:
        return "right"
    return "tie"


__all__ = [
    "CatalogQuery",
    "Comparison",
    "ComparisonRow",
    "ModelCatalog",
    "RankWeights",
    "ScoredListing",
    "score_listing",
]
