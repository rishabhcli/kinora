"""The canon-edit aggregate (kinora.md §5.4 canon editor, §8 memory).

The canon is the versioned "blackboard" the six agents share so a long adaptation
stays visually consistent. The Director can edit it *at any time* (§5.4): change a
character's appearance description, swap a locked reference image, retune a style
token. On save, the editor "computes which shots depend on the changed entity
(via the reference-set in each shot record) and **regenerates only those** —
surgical, not a full re-render."

This aggregate is the write-side authority for **one canon entity's edit stream**.
Every edit is a domain event that *monotonically bumps a canon version* (the
optimistic token the agents read against), records the dependent shots that must
be invalidated, and — through a saga — fans out shot-regeneration commands. Locking
a reference image is also modeled here because the Cinematographer must use the
locked refs verbatim (§10), so a swap is a first-class, audited fact.

Edits are pure decisions: validate (no empty field, no clobbering a different
concurrent canon version) then emit. The dependent-shot computation is supplied
by the caller (the editor already knows the reference-set membership); this
aggregate records and validates it rather than reaching into the shot store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.db.models.enums import EntityType
from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation, ValidationError
from app.eventsourcing.domain.events import DomainEvent, register_events
from app.eventsourcing.domain.identifiers import StreamCategory
from app.eventsourcing.domain.snapshotting import as_bool, as_int, as_str

# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CanonEntityRegistered(DomainEvent):
    """Genesis: a canon entity (character/location/prop/style) was created (§8)."""

    entity_id: str = ""
    book_id: str = ""
    entity_type: str = EntityType.CHARACTER.value
    name: str = ""


@dataclass(frozen=True, slots=True)
class CanonFieldEdited(DomainEvent):
    """A Director edited a canon field (§5.4). Bumps the entity's canon version.

    ``dependent_shot_ids`` are the shots whose reference-set includes this entity,
    captured at edit time so a saga can regenerate exactly those (§5.4 surgical).
    """

    entity_id: str = ""
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    canon_version: int = 0
    dependent_shot_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CanonReferenceImageSwapped(DomainEvent):
    """A locked reference image was swapped (§5.4, §10). Bumps the canon version."""

    entity_id: str = ""
    old_reference_id: str = ""
    new_reference_id: str = ""
    canon_version: int = 0
    dependent_shot_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CanonEvolvedFromConflict(DomainEvent):
    """The Showrunner evolved canon to resolve a §7.2 conflict (textual support).

    Distinct from a Director edit: the actor is an agent and the trigger is an
    arbitration decision, but it likewise bumps the version and invalidates shots.
    """

    entity_id: str = ""
    field_name: str = ""
    new_value: str = ""
    canon_version: int = 0
    conflict_id: str = ""
    dependent_shot_ids: tuple[str, ...] = ()


register_events(
    CanonEntityRegistered,
    CanonFieldEdited,
    CanonReferenceImageSwapped,
    CanonEvolvedFromConflict,
)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CanonEntityAggregate(AggregateRoot):
    """Event-sourced canon entity with a monotonic canon version.

    ``canon_version`` is the read token agents use to detect that a shot was
    rendered against stale canon. Each edit increments it by exactly one; an edit
    that asserts a stale ``expected_canon_version`` is rejected (a domain-level
    optimistic check layered *above* the stream version, because two different
    edits to two different fields may legitimately interleave but a blind
    overwrite of a just-changed field must not).
    """

    category = StreamCategory.CANON

    registered: bool = False
    book_id: str = ""
    entity_type: EntityType = EntityType.CHARACTER
    name: str = ""
    canon_version: int = 0
    fields: dict[str, str] = field(default_factory=dict)
    reference_id: str = ""

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self.registered = False
        self.book_id = ""
        self.entity_type = EntityType.CHARACTER
        self.name = ""
        self.canon_version = 0
        self.fields = {}
        self.reference_id = ""

    # -- decisions ----------------------------------------------------------- #

    def register(
        self,
        *,
        book_id: str,
        entity_type: EntityType,
        name: str,
    ) -> None:
        """Genesis: create the canon entity (canon_version starts at 1)."""
        if self.registered:
            raise CommandRejected(f"canon entity {self.aggregate_id} already registered")
        if not name.strip():
            raise ValidationError("entity name is required", field="name")
        self.emit(
            CanonEntityRegistered(
                entity_id=self.aggregate_id,
                book_id=book_id,
                entity_type=entity_type.value,
                name=name,
            )
        )

    def edit_field(
        self,
        *,
        field_name: str,
        new_value: str,
        dependent_shot_ids: Sequence[str] = (),
        expected_canon_version: int | None = None,
    ) -> bool:
        """Edit a canon field (§5.4). Bumps the canon version; no-op if unchanged.

        Returns whether an edit was emitted. ``expected_canon_version``, when
        given, must match the current version or the edit is rejected (a
        lost-update guard above the stream-level optimistic check).
        """
        self._require_registered()
        if not field_name:
            raise ValidationError("field_name is required", field="field_name")
        self._check_canon_version(expected_canon_version)
        old_value = self.fields.get(field_name, "")
        if old_value == new_value:
            return False
        self.emit(
            CanonFieldEdited(
                entity_id=self.aggregate_id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                canon_version=self.canon_version + 1,
                dependent_shot_ids=tuple(dependent_shot_ids),
            )
        )
        return True

    def swap_reference_image(
        self,
        *,
        new_reference_id: str,
        dependent_shot_ids: Sequence[str] = (),
        expected_canon_version: int | None = None,
    ) -> bool:
        """Swap the locked reference image (§5.4, §10). Bumps the canon version."""
        self._require_registered()
        if not new_reference_id:
            raise ValidationError("new_reference_id is required", field="new_reference_id")
        self._check_canon_version(expected_canon_version)
        if self.reference_id == new_reference_id:
            return False
        self.emit(
            CanonReferenceImageSwapped(
                entity_id=self.aggregate_id,
                old_reference_id=self.reference_id,
                new_reference_id=new_reference_id,
                canon_version=self.canon_version + 1,
                dependent_shot_ids=tuple(dependent_shot_ids),
            )
        )
        return True

    def evolve_from_conflict(
        self,
        *,
        field_name: str,
        new_value: str,
        conflict_id: str,
        dependent_shot_ids: Sequence[str] = (),
    ) -> bool:
        """The Showrunner evolves canon to resolve a §7.2 conflict (with support)."""
        self._require_registered()
        if not field_name:
            raise ValidationError("field_name is required", field="field_name")
        if not conflict_id:
            raise ValidationError("conflict_id is required", field="conflict_id")
        if self.fields.get(field_name, "") == new_value:
            return False
        self.emit(
            CanonEvolvedFromConflict(
                entity_id=self.aggregate_id,
                field_name=field_name,
                new_value=new_value,
                canon_version=self.canon_version + 1,
                conflict_id=conflict_id,
                dependent_shot_ids=tuple(dependent_shot_ids),
            )
        )
        return True

    # -- guards -------------------------------------------------------------- #

    def _require_registered(self) -> None:
        if not self.registered:
            raise CommandRejected(f"canon entity {self.aggregate_id} is not registered")

    def _check_canon_version(self, expected: int | None) -> None:
        if expected is not None and expected != self.canon_version:
            raise InvariantViolation(
                f"stale canon edit: expected version {expected}, "
                f"entity is at {self.canon_version}"
            )

    # -- fold ---------------------------------------------------------------- #

    def apply(self, event: DomainEvent) -> None:
        if isinstance(event, CanonEntityRegistered):
            self.registered = True
            self.book_id = event.book_id
            self.entity_type = EntityType(event.entity_type)
            self.name = event.name
            self.canon_version = 1
        elif isinstance(event, CanonFieldEdited):
            self.fields[event.field_name] = event.new_value
            self.canon_version = event.canon_version
        elif isinstance(event, CanonReferenceImageSwapped):
            self.reference_id = event.new_reference_id
            self.canon_version = event.canon_version
        elif isinstance(event, CanonEvolvedFromConflict):
            self.fields[event.field_name] = event.new_value
            self.canon_version = event.canon_version
        # Unknown events ignored (forward compatibility).

    # -- snapshotting -------------------------------------------------------- #

    def snapshot_state(self) -> dict[str, object]:
        return {
            "registered": self.registered,
            "book_id": self.book_id,
            "entity_type": self.entity_type.value,
            "name": self.name,
            "canon_version": self.canon_version,
            "fields": dict(self.fields),
            "reference_id": self.reference_id,
        }

    def restore_state(self, state: Mapping[str, object], *, version: int) -> None:
        self.registered = as_bool(state.get("registered"))
        self.book_id = as_str(state.get("book_id"))
        self.entity_type = EntityType(as_str(state.get("entity_type"), EntityType.CHARACTER.value))
        self.name = as_str(state.get("name"))
        self.canon_version = as_int(state.get("canon_version"))
        raw_fields = state.get("fields", {})
        self.fields = (
            {str(k): as_str(v) for k, v in raw_fields.items()}
            if isinstance(raw_fields, Mapping)
            else {}
        )
        self.reference_id = as_str(state.get("reference_id"))
        self.version = version
        self._committed_version = version


__all__ = [
    "CanonEntityAggregate",
    "CanonEntityRegistered",
    "CanonEvolvedFromConflict",
    "CanonFieldEdited",
    "CanonReferenceImageSwapped",
]
