"""In-memory canon2 store + service (append-only versioning, conflict queue).

This is the stateful seam the canon2 MCP tools route through. It deliberately
ships an **in-memory** backing (a dict of revision logs + a flagged-conflict
queue) so the whole subsystem runs with zero infra in tests; the public surface
is small and async, so a DB-backed implementation can replace it later without
touching the tools.

Responsibilities
----------------
* **append-only versioning** — :meth:`upsert_entity` mints the next per-entity
  revision, computes the field-level diff against the predecessor, and appends it.
  Nothing is mutated; ``get_entity`` time-travels over the log.
* **conflict resolution** — :meth:`propose_fact` checks the incoming proposal
  against the current active fact for ``(subject, predicate)`` and runs the §7.2
  policy (:mod:`.conflict`). Auto-resolved (``evolve`` / ``honor``) writes the
  winner; ``flag`` queues a :class:`FlaggedConflict` for arbitration and leaves
  the active fact untouched.
* **arbitration** — :meth:`resolve_conflict` closes a queued conflict with a
  director/Showrunner choice and applies the chosen object as the active fact.
* **consistency audit** — :meth:`audit` runs the :class:`ConsistencyAuditor` over
  the whole accumulated canon.

The store is **not** a CRDT replica set; it reuses the CRDT *stamp* order for the
deterministic LWW tiebreak only. Concurrency within one process is single-threaded
async, so no locking is needed.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.memory.bitemporal import utcnow
from app.memory.canon2.audit import AuditReport, ConsistencyAuditor
from app.memory.canon2.conflict import (
    ConflictPolicy,
    FlaggedConflict,
    Proposal,
    Resolution,
    build_options,
    resolve,
)
from app.memory.canon2.retrieval import CanonFact, CanonRetriever, RetrievedFact
from app.memory.canon2.versioning import (
    Canon2Kind,
    EntityHistory,
    Provenance,
    Revision,
    diff_attributes,
    revision_as_of_beat,
    revision_as_of_tx,
)
from app.memory.contracts import BeatSpan, BitemporalFact, TxSpan, WriteStamp
from app.memory.interfaces import Embedder


def _scope(book_id: str, branch: str, key: str) -> tuple[str, str, str]:
    return (book_id, branch, key)


class _ActiveFact:
    """The current canon belief for one (subject, predicate), with provenance."""

    __slots__ = ("fact_key", "proposal", "valid_from_beat", "tx_at")

    def __init__(self, fact_key: str, proposal: Proposal, valid_from_beat: int) -> None:
        self.fact_key = fact_key
        self.proposal = proposal
        self.valid_from_beat = valid_from_beat
        self.tx_at: datetime = utcnow()


class Canon2Store:
    """In-memory append-only canon store with §7.2 conflict resolution."""

    def __init__(self, embedder: Embedder, *, alpha: float = 0.7) -> None:
        self._embedder = embedder
        self._retriever = CanonRetriever(embedder, alpha=alpha)
        self._auditor = ConsistencyAuditor()
        # (book, branch, entity_key) -> revision log
        self._entities: dict[tuple[str, str, str], list[Revision]] = defaultdict(list)
        # (book, branch, subject, predicate) -> active fact
        self._facts: dict[tuple[str, str, str, str], _ActiveFact] = {}
        # (book, branch) -> conflict_id -> flagged conflict
        self._conflicts: dict[tuple[str, str], dict[str, FlaggedConflict]] = defaultdict(
            dict
        )

    # --- versioning --------------------------------------------------------- #

    async def upsert_entity(
        self,
        *,
        book_id: str,
        entity_key: str,
        kind: Canon2Kind,
        name: str,
        valid_from_beat: int,
        branch: str = "main",
        description: str | None = None,
        aliases: list[str] | None = None,
        appearance: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
        style_tokens: dict[str, Any] | None = None,
        provenance: Provenance | None = None,
    ) -> Revision:
        """Append a new revision of an entity (append-only; never mutates prior)."""
        log = self._entities[_scope(book_id, branch, entity_key)]
        prev = log[-1] if log else None
        seq = (prev.seq + 1) if prev else 1
        attrs: dict[str, Any] = {
            "name": name,
            "description": description,
            "aliases": list(aliases or []),
            "appearance": appearance,
            "voice": voice,
            "style_tokens": style_tokens,
        }
        deltas = diff_attributes(prev.attributes() if prev else None, attrs)
        rev = Revision(
            entity_key=entity_key,
            book_id=book_id,
            branch=branch,
            kind=kind,
            seq=seq,
            version=seq,
            valid_from_beat=valid_from_beat,
            name=name,
            description=description,
            aliases=list(aliases or []),
            appearance=appearance,
            voice=voice,
            style_tokens=style_tokens,
            deltas=deltas,
            provenance=provenance or Provenance(),
            is_genesis=prev is None,
        )
        log.append(rev)
        return rev

    def history(
        self, *, book_id: str, entity_key: str, branch: str = "main"
    ) -> EntityHistory | None:
        log = self._entities.get(_scope(book_id, branch, entity_key))
        if not log:
            return None
        return EntityHistory(
            entity_key=entity_key,
            book_id=book_id,
            branch=branch,
            kind=log[-1].kind,
            revisions=list(log),
        )

    def get_entity(
        self,
        *,
        book_id: str,
        entity_key: str,
        branch: str = "main",
        at_beat: int | None = None,
        as_of_tx: datetime | None = None,
    ) -> Revision | None:
        """Time-travel read: the entity as of a beat (default latest), or a tx instant."""
        hist = self.history(book_id=book_id, entity_key=entity_key, branch=branch)
        if hist is None:
            return None
        if as_of_tx is not None:
            return revision_as_of_tx(hist, as_of_tx)
        if at_beat is None:
            return hist.latest
        return revision_as_of_beat(hist, at_beat)

    def all_histories(
        self, *, book_id: str, branch: str = "main"
    ) -> list[EntityHistory]:
        out: list[EntityHistory] = []
        for (b, br, key), log in self._entities.items():
            if b == book_id and br == branch and log:
                out.append(
                    EntityHistory(
                        entity_key=key,
                        book_id=book_id,
                        branch=branch,
                        kind=log[-1].kind,
                        revisions=list(log),
                    )
                )
        return sorted(out, key=lambda h: h.entity_key)

    # --- conflict resolution ----------------------------------------------- #

    async def propose_fact(
        self,
        *,
        book_id: str,
        proposal: Proposal,
        branch: str = "main",
        valid_from_beat: int = 0,
        current_beat: int | None = None,
    ) -> Resolution:
        """Propose a canon fact; resolve any contradiction under the §7.2 policy.

        No existing fact → accept (HONOR no-op). Existing fact agrees → no-op.
        Contradiction → run :func:`resolve`; auto-resolved outcomes write the
        winner, ``FLAG`` queues a conflict and leaves the active fact in place.
        """
        fkey = (book_id, branch, proposal.subject, proposal.predicate)
        existing = self._facts.get(fkey)

        if existing is None:
            self._facts[fkey] = _ActiveFact(
                fact_key=self._fact_key(proposal),
                proposal=proposal,
                valid_from_beat=valid_from_beat,
            )
            return Resolution(
                subject=proposal.subject,
                predicate=proposal.predicate,
                policy=ConflictPolicy.HONOR,
                winning_object=proposal.object_value,
                winner="incoming",
                incoming_object=proposal.object_value,
                existing_object="",
                reason="first assertion: no prior fact to contradict",
            )

        decision = resolve(proposal, existing.proposal)

        if decision.policy is ConflictPolicy.FLAG:
            conflict = self._enqueue_conflict(
                book_id=book_id,
                branch=branch,
                incoming=proposal,
                existing=existing.proposal,
                current_beat=current_beat,
            )
            decision = decision.model_copy(update={"conflict_id": conflict.conflict_id})
            return decision

        # EVOLVE or HONOR → apply the winner as the active fact.
        if decision.winner == "incoming":
            self._facts[fkey] = _ActiveFact(
                fact_key=self._fact_key(proposal),
                proposal=proposal,
                valid_from_beat=valid_from_beat,
            )
        # winner == "existing" → leave the active fact untouched.
        return decision

    def _enqueue_conflict(
        self,
        *,
        book_id: str,
        branch: str,
        incoming: Proposal,
        existing: Proposal,
        current_beat: int | None,
    ) -> FlaggedConflict:
        conflict = FlaggedConflict(
            conflict_id=f"cf_{uuid.uuid4().hex[:12]}",
            book_id=book_id,
            branch=branch,
            subject=incoming.subject,
            predicate=incoming.predicate,
            incoming=incoming,
            existing=existing,
            current_beat=current_beat,
            options=build_options(incoming, existing),
        )
        self._conflicts[(book_id, branch)][conflict.conflict_id] = conflict
        return conflict

    def list_conflicts(
        self, *, book_id: str, branch: str = "main", include_resolved: bool = False
    ) -> list[FlaggedConflict]:
        bucket = self._conflicts.get((book_id, branch), {})
        out = [
            c for c in bucket.values() if include_resolved or not c.resolved
        ]
        return sorted(out, key=lambda c: c.raised_at)

    async def resolve_conflict(
        self,
        *,
        book_id: str,
        conflict_id: str,
        chosen_object: str,
        branch: str = "main",
        resolved_by: str = "director",
        reasoning: str | None = None,
        valid_from_beat: int = 0,
    ) -> FlaggedConflict:
        """Close a queued conflict with an arbitration choice and apply it as canon."""
        bucket = self._conflicts.get((book_id, branch), {})
        conflict = bucket.get(conflict_id)
        if conflict is None:
            raise KeyError(f"unknown conflict: {conflict_id}")
        winning = (
            conflict.incoming
            if chosen_object == conflict.incoming.object_value
            else conflict.existing
            if chosen_object == conflict.existing.object_value
            else conflict.incoming.model_copy(update={"object_value": chosen_object})
        )
        fkey = (book_id, branch, conflict.subject, conflict.predicate)
        self._facts[fkey] = _ActiveFact(
            fact_key=self._fact_key(winning),
            proposal=winning,
            valid_from_beat=valid_from_beat,
        )
        updated = conflict.model_copy(
            update={
                "resolved": True,
                "chosen_object": chosen_object,
                "resolved_by": resolved_by,
                "reasoning": reasoning,
            }
        )
        bucket[conflict_id] = updated
        return updated

    def active_facts(
        self, *, book_id: str, branch: str = "main"
    ) -> list[BitemporalFact]:
        """The current active fact set as :class:`BitemporalFact` (for the auditor/graph)."""
        out: list[BitemporalFact] = []
        for (b, br, subject, predicate), af in self._facts.items():
            if b != book_id or br != branch:
                continue
            stamp = af.proposal.stamp()
            out.append(
                BitemporalFact(
                    id=af.fact_key,
                    fact_key=af.fact_key,
                    branch=branch,
                    subject_entity_key=subject,
                    predicate=predicate,
                    object_value=af.proposal.object_value,
                    valid=BeatSpan(valid_from_beat=af.valid_from_beat, valid_to_beat=None),
                    tx=TxSpan(tx_from=af.tx_at, tx_to=None),
                    stamp=WriteStamp(
                        wall=stamp.hlc.wall,
                        counter=stamp.hlc.counter,
                        actor_id=stamp.actor_id,
                    ),
                    source_span=af.proposal.source_span,
                    current=True,
                )
            )
        return sorted(out, key=lambda f: (f.subject_entity_key, f.predicate))

    # --- retrieval ---------------------------------------------------------- #

    async def retrieve(
        self,
        *,
        book_id: str,
        query: str,
        branch: str = "main",
        k: int = 5,
        lambda_: float = 0.6,
        extra_candidates: list[CanonFact] | None = None,
    ) -> list[RetrievedFact]:
        """Hybrid keyword+vector recall over the book's active facts (+ extras)."""
        candidates: list[CanonFact] = [
            CanonFact(
                fact_key=f.fact_key,
                subject=f.subject_entity_key,
                predicate=f.predicate,
                object_value=f.object_value,
                valid_from_beat=f.valid.valid_from_beat,
            )
            for f in self.active_facts(book_id=book_id, branch=branch)
        ]
        if extra_candidates:
            candidates.extend(extra_candidates)
        return await self._retriever.retrieve(query, candidates, k=k, lambda_=lambda_)

    # --- audit -------------------------------------------------------------- #

    def audit(
        self,
        *,
        book_id: str,
        branch: str = "main",
        mutually_exclusive: list[tuple[str, str]] | None = None,
    ) -> AuditReport:
        return self._auditor.audit(
            book_id=book_id,
            branch=branch,
            facts=self.active_facts(book_id=book_id, branch=branch),
            histories=self.all_histories(book_id=book_id, branch=branch),
            flagged=self.list_conflicts(
                book_id=book_id, branch=branch, include_resolved=False
            ),
            mutually_exclusive=mutually_exclusive or [],
        )

    @staticmethod
    def _fact_key(proposal: Proposal) -> str:
        return f"f_{proposal.subject}:{proposal.predicate}"


__all__ = ["Canon2Store"]
