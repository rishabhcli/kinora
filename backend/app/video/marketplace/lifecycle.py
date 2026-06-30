"""Deprecation / sunset / retirement lifecycle with migration hints.

Once a listing is ``ACTIVE`` it has a *forward* lifecycle that mirrors how real
hosted models age out:

    ACTIVE → DEPRECATED → SUNSET → RETIRED

* **deprecate** — still selectable, but flagged: it names a ``replacement_key``
  (and/or a ``migration_note``) and may set a ``sunset_at`` date.
* **sunset** — no longer selectable for *new* renders, but still visible with a
  loud warning; the replacement pointer is mandatory in practice.
* **retire** — hidden from the default catalog; kept for audit/history.

Every transition runs through the catalog so the listing snapshot and the
registry stay consistent, and each produces an explainable
:class:`LifecycleEvent`. :class:`MigrationHint` turns a deprecated listing into
concrete guidance — the replacement listing (if present in the catalog), a
capability-gap delta, and a price delta — so a reader/renderer can re-route.

Illegal transitions raise :class:`~app.video.marketplace.errors.LifecycleError`.
Pure, offline, deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.video.marketplace.catalog import ModelCatalog
from app.video.marketplace.errors import LifecycleError, ListingNotFoundError
from app.video.marketplace.listing import ModelListing
from app.video.marketplace.types import Capability, ListingStatus

#: Allowed forward transitions in the post-activation lifecycle.
_ALLOWED: dict[ListingStatus, frozenset[ListingStatus]] = {
    ListingStatus.ACTIVE: frozenset({ListingStatus.DEPRECATED, ListingStatus.SUNSET}),
    ListingStatus.DEPRECATED: frozenset({ListingStatus.SUNSET, ListingStatus.RETIRED}),
    ListingStatus.SUNSET: frozenset({ListingStatus.RETIRED}),
    ListingStatus.PREVIEW: frozenset({ListingStatus.DEPRECATED, ListingStatus.RETIRED}),
}


@dataclass(frozen=True)
class LifecycleEvent:
    """An explainable record of one lifecycle transition."""

    key: str
    from_status: ListingStatus
    to_status: ListingStatus
    replacement_key: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MigrationHint:
    """Concrete migration guidance from a deprecated listing to its replacement."""

    from_key: str
    to_key: str | None
    note: str
    sunset_at: datetime | None
    #: Capabilities the *deprecated* model had that the replacement lacks (a risk).
    lost_capabilities: tuple[Capability, ...] = ()
    #: Capabilities the replacement *adds* over the deprecated model (a gain).
    gained_capabilities: tuple[Capability, ...] = ()
    #: Cheapest per-second price delta (replacement - deprecated); None if either unpriced.
    price_delta_per_second_usd: float | None = None
    #: True when a usable replacement listing exists & is selectable.
    replacement_available: bool = False


class LifecycleManager:
    """Runs deprecation/sunset/retire transitions against a :class:`ModelCatalog`."""

    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog

    # ------------------------------------------------------------------ #
    def _transition(
        self,
        key: str,
        to_status: ListingStatus,
        *,
        replacement_key: str | None,
        migration_note: str,
        sunset_at: datetime | None,
        now: datetime | None,
        reasons: list[str],
    ) -> LifecycleEvent:
        listing = self._catalog.get(key)
        allowed = _ALLOWED.get(listing.status, frozenset())
        if to_status not in allowed:
            raise LifecycleError(
                f"cannot move {key!r} from {listing.status.value} to {to_status.value}"
            )
        # validate replacement exists if named
        if replacement_key is not None:
            if replacement_key == key:
                raise LifecycleError("a listing cannot be its own replacement")
            try:
                self._catalog.get(replacement_key)
            except ListingNotFoundError as exc:
                raise LifecycleError(
                    f"replacement {replacement_key!r} is not in the catalog"
                ) from exc

        changes: dict[str, object] = {"status": to_status}
        # carry-forward replacement/sunset/note when provided, else keep existing
        if replacement_key is not None:
            changes["replacement_key"] = replacement_key
        if migration_note:
            changes["migration_note"] = migration_note
        if sunset_at is not None:
            changes["sunset_at"] = sunset_at

        updated = listing.evolve(now=now, **changes)
        self._catalog.upsert(updated)
        return LifecycleEvent(
            key=key,
            from_status=listing.status,
            to_status=to_status,
            replacement_key=updated.replacement_key,
            reasons=tuple(reasons),
        )

    def deprecate(
        self,
        key: str,
        *,
        replacement_key: str | None = None,
        migration_note: str = "",
        sunset_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LifecycleEvent:
        """Mark a listing DEPRECATED (still selectable, but flagged + migration-hinted).

        Requires a ``replacement_key`` or a ``migration_note`` so the listing's
        own validator (and downstream readers) always have a forward path.
        """
        if not replacement_key and not migration_note:
            raise LifecycleError(
                "deprecate requires replacement_key or migration_note (no dead-ends)"
            )
        reasons = ["listing deprecated; prefer the replacement for new renders"]
        if sunset_at is not None:
            reasons.append(f"scheduled sunset at {sunset_at.isoformat()}")
        return self._transition(
            key,
            ListingStatus.DEPRECATED,
            replacement_key=replacement_key,
            migration_note=migration_note,
            sunset_at=sunset_at,
            now=now,
            reasons=reasons,
        )

    def sunset(
        self,
        key: str,
        *,
        replacement_key: str | None = None,
        migration_note: str = "",
        sunset_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LifecycleEvent:
        """Move a listing to SUNSET (no new renders; visible with a loud warning)."""
        return self._transition(
            key,
            ListingStatus.SUNSET,
            replacement_key=replacement_key,
            migration_note=migration_note,
            sunset_at=sunset_at,
            now=now,
            reasons=["listing sunset; not selectable for new renders"],
        )

    def retire(self, key: str, *, now: datetime | None = None) -> LifecycleEvent:
        """Retire a listing (hidden from the default catalog; kept for audit)."""
        return self._transition(
            key,
            ListingStatus.RETIRED,
            replacement_key=None,
            migration_note="",
            sunset_at=None,
            now=now,
            reasons=["listing retired; hidden from the default catalog"],
        )

    # ------------------------------------------------------------------ #
    def migration_hint(self, key: str) -> MigrationHint:
        """Build concrete migration guidance for a (typically deprecated) listing.

        Resolves the replacement from the catalog if present and computes the
        capability gap and price delta so a caller can decide whether the
        replacement is a safe re-route.
        """
        listing = self._catalog.get(key)
        replacement: ModelListing | None = None
        available = False
        if listing.replacement_key:
            try:
                replacement = self._catalog.get(listing.replacement_key)
                available = replacement.status.is_selectable
            except ListingNotFoundError:
                replacement = None

        lost: tuple[Capability, ...] = ()
        gained: tuple[Capability, ...] = ()
        price_delta: float | None = None
        if replacement is not None:
            have = set(listing.capabilities)
            repl = set(replacement.capabilities)
            lost = tuple(c for c in listing.capabilities if c not in repl)
            gained = tuple(c for c in replacement.capabilities if c not in have)
            lp = listing.cheapest_per_second_usd
            rp = replacement.cheapest_per_second_usd
            if lp is not None and rp is not None:
                price_delta = round(rp - lp, 6)

        note = listing.migration_note or (
            f"migrate to {listing.replacement_key}" if listing.replacement_key else
            "no replacement designated; this model is a dead-end"
        )
        return MigrationHint(
            from_key=key,
            to_key=listing.replacement_key,
            note=note,
            sunset_at=listing.sunset_at,
            lost_capabilities=lost,
            gained_capabilities=gained,
            price_delta_per_second_usd=price_delta,
            replacement_available=available,
        )


__all__ = [
    "LifecycleEvent",
    "LifecycleManager",
    "MigrationHint",
]
