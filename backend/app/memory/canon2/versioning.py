"""Append-only, versioned canon entity model with diffs + time-travel reads.

This is the *deepened* canon memory (kinora.md §8.1): the existing
:class:`~app.memory.canon_service.CanonService` versions an entity by valid-from
beat; ``canon2`` adds the **why and the who** — every change to a character's
appearance, a setting, or a style rule is an immutable :class:`Revision` carrying
provenance (who/what/when), and the field-level :class:`FieldDelta` between this
revision and its predecessor. Reads are *time-travel*: "the canon as of page N"
(``as_of_beat``) resolves the latest revision whose ``valid_from_beat <= beat``,
and "as the canon believed it at time T" (``as_of_tx``) resolves the latest
revision committed at or before a transaction instant.

Pure and offline — no DB, no provider. The store layer (:mod:`.store`) holds the
revision log; this module owns the *shape* of a revision and the diffing.

Design notes
------------
* **Append-only.** A revision is never mutated. Correcting a fact appends a new
  revision; the prior one survives for time-travel and provenance. This mirrors
  the bitemporal engine's transaction-time invariant (§8) but at the *entity
  document* granularity the §8.1 canon editor edits.
* **Two clocks.** ``valid_from_beat`` is *valid time* (where in the story the
  change takes effect); ``tx_at`` is *transaction time* (when an agent committed
  the belief). A correction can backdate ``valid_from_beat`` while keeping a fresh
  ``tx_at`` — the canon "always knew" the hero had a scar from beat 3, even though
  the Continuity Supervisor only asserted it at beat 40.
* **Deterministic ordering.** Within one entity, revisions order by
  ``(seq)`` — a monotone per-entity counter the store mints. ``seq`` is the
  tiebreak that keeps time-travel reads total even when two revisions share a
  ``valid_from_beat`` or a ``tx_at`` (deterministic in tests with no wall clock).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.memory.bitemporal import LATEST_BEAT, utcnow


class Canon2Kind(StrEnum):
    """The kinds of canon entity canon2 versions (the §8.1 node types)."""

    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"


#: The attribute fields a revision carries (the diff operates field-by-field over
#: these). ``name``/``aliases`` are identity; ``appearance``/``voice``/
#: ``style_tokens`` are the look-and-sound canon a shot is conditioned on;
#: ``description`` is the prose bible entry.
REVISION_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "aliases",
    "appearance",
    "voice",
    "style_tokens",
)


class Provenance(BaseModel):
    """Who/what/when produced a revision — the §8 audit-trail at entity grain."""

    actor_id: str = "system"
    #: Free-text reason, e.g. "Continuity Supervisor: scar added after the duel".
    reason: str | None = None
    #: The source span the change is grounded in (page / char-range), §8.1.
    source_span: dict[str, Any] | None = None
    #: The agent role that proposed it (continuity_supervisor, director, ...).
    proposed_by: str | None = None


class FieldDelta(BaseModel):
    """One field's change between a revision and its predecessor."""

    field: str
    #: "added" | "removed" | "changed"
    change: str
    before: Any | None = None
    after: Any | None = None


class Revision(BaseModel):
    """One immutable version of a canon entity (append-only).

    A revision is the full materialized state of the entity *as of* its
    ``valid_from_beat`` — not a patch — so a time-travel read is a single lookup,
    not a fold. The :attr:`deltas` are the *derived* field-level diff against the
    immediately-preceding revision, computed once at append time and frozen.
    """

    entity_key: str
    book_id: str
    branch: str = "main"
    kind: Canon2Kind
    #: Monotone per-(book, branch, entity) revision counter (1-based). Total order.
    seq: int
    #: 1-based user-facing version (== ``seq``; kept distinct for wire clarity).
    version: int
    valid_from_beat: int
    tx_at: datetime = Field(default_factory=utcnow)
    # --- materialized attributes -------------------------------------------- #
    name: str
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    appearance: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    style_tokens: dict[str, Any] | None = None
    # --- derived / provenance ----------------------------------------------- #
    deltas: list[FieldDelta] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)
    #: True iff this is the very first revision of the entity (no predecessor).
    is_genesis: bool = False

    def attributes(self) -> dict[str, Any]:
        """The materialized attribute map (the fields the diff compares)."""
        return {f: getattr(self, f) for f in REVISION_FIELDS}


class EntityHistory(BaseModel):
    """The full append-only revision log of one canon entity (time-travel input)."""

    entity_key: str
    book_id: str
    branch: str
    kind: Canon2Kind
    revisions: list[Revision] = Field(default_factory=list)

    @property
    def latest(self) -> Revision | None:
        return self.revisions[-1] if self.revisions else None


def diff_attributes(
    before: dict[str, Any] | None, after: dict[str, Any]
) -> list[FieldDelta]:
    """Field-by-field diff over :data:`REVISION_FIELDS` (genesis when ``before`` is None).

    Lists/dicts compare by value equality; a field present-then-absent is
    ``removed`` (after is falsy/empty), absent-then-present is ``added``, and a
    changed value is ``changed``. Deterministic order (the declared field order).
    """
    deltas: list[FieldDelta] = []
    prev = before or {}
    for field in REVISION_FIELDS:
        old = prev.get(field)
        new = after.get(field)
        if _equal(old, new):
            continue
        if _empty(old) and not _empty(new):
            deltas.append(FieldDelta(field=field, change="added", before=None, after=new))
        elif not _empty(old) and _empty(new):
            deltas.append(FieldDelta(field=field, change="removed", before=old, after=None))
        else:
            deltas.append(FieldDelta(field=field, change="changed", before=old, after=new))
    return deltas


def revision_as_of_beat(history: EntityHistory, beat: int) -> Revision | None:
    """Time-travel read: the entity *as of* a story beat (kinora.md §8.1, §8.4).

    Returns the latest revision whose ``valid_from_beat <= beat``, ordered by
    ``(valid_from_beat, seq)`` so ties break deterministically toward the newer
    revision. ``None`` when the entity did not exist yet at ``beat``.
    """
    candidates = [r for r in history.revisions if r.valid_from_beat <= beat]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.valid_from_beat, r.seq))


def revision_as_of_tx(history: EntityHistory, tx: datetime) -> Revision | None:
    """Belief-time read: the entity as the canon *believed it* at instant ``tx``.

    Returns the latest revision whose ``tx_at <= tx`` (what an agent would have
    retrieved had it queried at ``tx``). ``None`` when nothing was committed yet.
    """
    candidates = [r for r in history.revisions if r.tx_at <= tx]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.tx_at, r.seq))


def _empty(value: Any) -> bool:
    """A field counts as 'absent' when None or an empty container."""
    if value is None:
        return True
    if isinstance(value, (list, dict, str, tuple, set)):
        return len(value) == 0
    return False


def _equal(a: Any, b: Any) -> bool:
    """Value equality treating ``None`` and empty containers as the same absence."""
    if _empty(a) and _empty(b):
        return True
    return a == b


__all__ = [
    "LATEST_BEAT",
    "REVISION_FIELDS",
    "Canon2Kind",
    "EntityHistory",
    "FieldDelta",
    "Provenance",
    "Revision",
    "diff_attributes",
    "revision_as_of_beat",
    "revision_as_of_tx",
]
