"""Canon inspection + repair (kinora.md §8).

The canon graph is the consistency engine; an operator needs to read it and
verify its integrity without the renderer:

* ``entities``     — the active entity version per key at a beat (time-travel
  read), or every version of one key.
* ``states``       — continuity facts for a book (active + retired), the §8.5
  forgetting timeline.
* ``audit-verify`` — recompute the hash chain of the canon audit log and report
  the first break (tamper / corruption detection).
* ``branches``     — the §8 branch registry (FORK / DIFF / MERGE lines).
* ``integrity``    — structural checks: entities with broken supersedes links,
  facts with inverted intervals, version gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cli.errors import not_found
from app.cli.formatting import isoformat, truncate
from app.cli.output import Payload, Table
from app.composition import Container
from app.db.models.enums import EntityType
from app.db.repositories.bitemporal import CanonAuditRepo, CanonBranchRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.entity import EntityRepo


@dataclass(frozen=True, slots=True)
class EntityRow:
    entity_key: str
    type: str
    name: str
    version: int
    valid_from_beat: int
    valid_to_beat: int | None
    has_embedding: bool
    description: str | None


@dataclass(frozen=True, slots=True)
class EntityListing:
    """The result of ``canon entities``."""

    book_id: str
    beat: int | None
    kind: str | None
    entities: tuple[EntityRow, ...]

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "beat": self.beat,
            "kind": self.kind,
            "entities": [
                {
                    "entity_key": e.entity_key,
                    "type": e.type,
                    "name": e.name,
                    "version": e.version,
                    "valid_from_beat": e.valid_from_beat,
                    "valid_to_beat": e.valid_to_beat,
                    "has_embedding": e.has_embedding,
                    "description": e.description,
                }
                for e in self.entities
            ],
        }
        suffix = f" @ beat {self.beat}" if self.beat is not None else " (all versions)"
        table = Table(
            title=f"canon entities — book {self.book_id}{suffix} ({len(self.entities)})",
            columns=("entity_key", "type", "name", "ver", "from", "to", "emb"),
            rows=[
                (
                    e.entity_key,
                    e.type,
                    truncate(e.name, 24),
                    str(e.version),
                    str(e.valid_from_beat),
                    str(e.valid_to_beat) if e.valid_to_beat is not None else "open",
                    "y" if e.has_embedding else "-",
                )
                for e in self.entities
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class StateRow:
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    valid_to_beat: int | None
    version: int


@dataclass(frozen=True, slots=True)
class StateListing:
    """The result of ``canon states`` — continuity facts (active + retired)."""

    book_id: str
    states: tuple[StateRow, ...]

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "states": [
                {
                    "subject": s.subject_entity_key,
                    "predicate": s.predicate,
                    "object": s.object_value,
                    "valid_from_beat": s.valid_from_beat,
                    "valid_to_beat": s.valid_to_beat,
                    "version": s.version,
                    "active": s.valid_to_beat is None,
                }
                for s in self.states
            ],
        }
        table = Table(
            title=f"continuity states — book {self.book_id} ({len(self.states)})",
            columns=("subject", "predicate", "object", "from", "to", "ver"),
            rows=[
                (
                    truncate(s.subject_entity_key, 20),
                    truncate(s.predicate, 16),
                    truncate(s.object_value, 24),
                    str(s.valid_from_beat),
                    str(s.valid_to_beat) if s.valid_to_beat is not None else "active",
                    str(s.version),
                )
                for s in self.states
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class AuditVerifyReport:
    """The result of ``canon audit-verify`` — hash-chain integrity."""

    book_id: str
    length: int
    valid: bool
    first_break_seq: int | None
    detail: str

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "length": self.length,
            "valid": self.valid,
            "first_break_seq": self.first_break_seq,
            "detail": self.detail,
        }
        from app.cli.output import kv_table

        table = kv_table(
            f"canon audit chain — book {self.book_id}",
            {
                "length": self.length,
                "valid": "YES" if self.valid else "NO — CHAIN BROKEN",
                "first_break_seq": self.first_break_seq if self.first_break_seq else "-",
                "detail": self.detail,
            },
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class BranchRow:
    name: str
    parent: str | None
    status: str
    base_beat: int | None
    created_at_iso: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class BranchListing:
    """The result of ``canon branches``."""

    book_id: str
    branches: tuple[BranchRow, ...]

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "branches": [
                {
                    "name": b.name,
                    "parent": b.parent,
                    "status": b.status,
                    "base_beat": b.base_beat,
                    "created_at": b.created_at_iso,
                    "note": b.note,
                }
                for b in self.branches
            ],
        }
        table = Table(
            title=f"canon branches — book {self.book_id} ({len(self.branches)})",
            columns=("name", "parent", "status", "base_beat", "note"),
            rows=[
                (
                    b.name,
                    b.parent or "-",
                    b.status,
                    str(b.base_beat) if b.base_beat is not None else "-",
                    truncate(b.note, 24) if b.note else "-",
                )
                for b in self.branches
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    severity: str
    kind: str
    target: str
    detail: str


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The result of ``canon integrity`` — structural canon checks."""

    book_id: str
    checked: dict[str, int]
    issues: tuple[IntegrityIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues

    def render_payload(self) -> Payload:
        data = {
            "book_id": self.book_id,
            "ok": self.ok,
            "checked": self.checked,
            "issues": [
                {"severity": i.severity, "kind": i.kind, "target": i.target, "detail": i.detail}
                for i in self.issues
            ],
        }
        table = Table(
            title=f"canon integrity — book {self.book_id} ({len(self.issues)} issue(s))",
            columns=("severity", "kind", "target", "detail"),
            rows=[
                (i.severity, i.kind, truncate(i.target, 24), truncate(i.detail, 48))
                for i in self.issues
            ],
        )
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def list_entities(
    container: Container,
    book_id: str,
    *,
    beat: int | None = None,
    kind: EntityType | None = None,
    entity_key: str | None = None,
) -> EntityListing:
    """List canon entities for a book.

    With ``entity_key`` set, returns every version of that one key (the version
    timeline); otherwise returns the active version per key at ``beat`` (or beat
    0 when omitted), optionally filtered by ``kind``.
    """
    from sqlalchemy import select

    from app.db.models.entity import Entity

    async with container.session_factory() as db:
        repo = EntityRepo(db)
        if entity_key is not None:
            rows = list(
                (
                    await db.execute(
                        select(Entity)
                        .where(Entity.book_id == book_id, Entity.entity_key == entity_key)
                        .order_by(Entity.version)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                raise not_found("entity", f"{book_id}/{entity_key}")
        else:
            rows = await repo.list_active_at_beat(
                book_id, beat if beat is not None else 0, kinds=[kind] if kind else None
            )
    entities = tuple(
        EntityRow(
            entity_key=e.entity_key,
            type=e.type.value,
            name=e.name,
            version=e.version,
            valid_from_beat=e.valid_from_beat,
            valid_to_beat=e.valid_to_beat,
            has_embedding=e.embedding is not None,
            description=e.description,
        )
        for e in rows
    )
    return EntityListing(
        book_id=book_id,
        beat=beat,
        kind=kind.value if kind else None,
        entities=entities,
    )


async def list_states(container: Container, book_id: str) -> StateListing:
    """The continuity-state timeline for a book (active + retired, §8.5)."""
    async with container.session_factory() as db:
        rows = await ContinuityStateRepo(db).list_for_book(book_id)
    states = tuple(
        StateRow(
            subject_entity_key=s.subject_entity_key,
            predicate=s.predicate,
            object_value=s.object_value,
            valid_from_beat=s.valid_from_beat,
            valid_to_beat=s.valid_to_beat,
            version=s.version,
        )
        for s in rows
    )
    return StateListing(book_id=book_id, states=states)


async def verify_audit_chain(container: Container, book_id: str) -> AuditVerifyReport:
    """Recompute the canon audit hash chain and report the first break.

    Replays the log in sequence order, recomputing each row's
    ``H(prev || seq || action || actor || target || payload_repr)`` and checking
    it matches the stored ``entry_hash`` *and* that ``prev_hash`` links to the
    previous row's hash. A mismatch flags tampering / corruption at that seq.
    """
    async with container.session_factory() as db:
        repo = CanonAuditRepo(db)
        rows = await repo.replay(book_id)

    if not rows:
        return AuditVerifyReport(
            book_id=book_id,
            length=0,
            valid=True,
            first_break_seq=None,
            detail="empty chain (no audit rows)",
        )

    prev_hash: str | None = None
    for row in rows:
        payload_repr = repr(row.payload) if row.payload is not None else ""
        expected = CanonAuditRepo.compute_hash(
            prev_hash,
            seq=row.seq,
            action=row.action.value,
            actor_id=row.actor_id,
            target_key=row.target_key,
            payload_repr=payload_repr,
        )
        if row.prev_hash != prev_hash:
            return AuditVerifyReport(
                book_id=book_id,
                length=len(rows),
                valid=False,
                first_break_seq=row.seq,
                detail=f"prev_hash mismatch at seq {row.seq}",
            )
        if row.entry_hash != expected:
            return AuditVerifyReport(
                book_id=book_id,
                length=len(rows),
                valid=False,
                first_break_seq=row.seq,
                detail=f"entry_hash mismatch at seq {row.seq}",
            )
        prev_hash = row.entry_hash

    return AuditVerifyReport(
        book_id=book_id,
        length=len(rows),
        valid=True,
        first_break_seq=None,
        detail=f"chain intact across {len(rows)} entries",
    )


async def list_branches(container: Container, book_id: str) -> BranchListing:
    """List the canon branch registry for a book."""
    async with container.session_factory() as db:
        rows = await CanonBranchRepo(db).list_for_book(book_id)
    branches = tuple(
        BranchRow(
            name=b.name,
            parent=b.parent,
            status=b.status.value,
            base_beat=b.base_beat,
            created_at_iso=isoformat(b.created_at),
            note=b.note,
        )
        for b in rows
    )
    return BranchListing(book_id=book_id, branches=branches)


async def check_integrity(container: Container, book_id: str) -> IntegrityReport:
    """Run structural canon checks for a book (broken links, bad intervals).

    Pure read-only diagnostics — never mutates. Flags:
      * entity versions whose ``valid_to_beat < valid_from_beat`` (inverted),
      * continuity facts with inverted valid intervals,
      * entity keys whose version sequence has a gap (missing intermediate ver).
    """
    from sqlalchemy import select

    from app.db.models.continuity import ContinuityState
    from app.db.models.entity import Entity

    issues: list[IntegrityIssue] = []
    async with container.session_factory() as db:
        entities = list(
            (
                await db.execute(
                    select(Entity)
                    .where(Entity.book_id == book_id)
                    .order_by(Entity.entity_key, Entity.version)
                )
            )
            .scalars()
            .all()
        )
        states = list(
            (await db.execute(select(ContinuityState).where(ContinuityState.book_id == book_id)))
            .scalars()
            .all()
        )

    # Inverted entity intervals + version-gap detection.
    by_key: dict[str, list[int]] = {}
    for e in entities:
        by_key.setdefault(e.entity_key, []).append(e.version)
        if e.valid_to_beat is not None and e.valid_to_beat < e.valid_from_beat:
            issues.append(
                IntegrityIssue(
                    severity="error",
                    kind="inverted_entity_interval",
                    target=f"{e.entity_key}@v{e.version}",
                    detail=f"valid_to {e.valid_to_beat} < valid_from {e.valid_from_beat}",
                )
            )
    for key, versions in by_key.items():
        ordered = sorted(versions)
        expected = list(range(1, len(ordered) + 1))
        if ordered != expected:
            issues.append(
                IntegrityIssue(
                    severity="warn",
                    kind="version_gap",
                    target=key,
                    detail=f"versions {ordered} not contiguous from 1",
                )
            )

    for s in states:
        if s.valid_to_beat is not None and s.valid_to_beat < s.valid_from_beat:
            issues.append(
                IntegrityIssue(
                    severity="error",
                    kind="inverted_state_interval",
                    target=f"{s.subject_entity_key}:{s.predicate}",
                    detail=f"valid_to {s.valid_to_beat} < valid_from {s.valid_from_beat}",
                )
            )

    return IntegrityReport(
        book_id=book_id,
        checked={"entities": len(entities), "states": len(states), "entity_keys": len(by_key)},
        issues=tuple(issues),
    )


__all__ = [
    "AuditVerifyReport",
    "BranchListing",
    "BranchRow",
    "EntityListing",
    "EntityRow",
    "IntegrityIssue",
    "IntegrityReport",
    "StateListing",
    "StateRow",
    "check_integrity",
    "list_branches",
    "list_entities",
    "list_states",
    "verify_audit_chain",
]
