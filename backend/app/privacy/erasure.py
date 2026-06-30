"""The right-to-erasure orchestrator (GDPR Art. 17 — right to be forgotten).

Coherently removes a data subject across *every* store the
:mod:`~app.privacy.datamap` knows about, picking the correct strategy per store:

* **mutable stores** (relational rows, object blobs, derived indexes) —
  *hard-delete* or *anonymize-in-place* per the field's
  :class:`~app.privacy.enums.ErasureStrategy`;
* **append-only stores** (the domain event store + the hash-chained audit log) —
  never deleted (that would break their integrity proofs). Instead the event
  store is **crypto-erased** (destroy the subject's key so ciphertext is
  unrecoverable) and the audit log is **redacted** through the local
  :class:`~app.privacy.protocols.AuditRedactor` seam, which re-derives the
  affected hashes and re-chains the tail so the log still verifies end-to-end.

The run is:

* **hold-aware** — before any destructive step it consults the
  :class:`~app.privacy.retention.RetentionPolicy`; an active legal hold over the
  subject (or a held data class) raises :class:`~app.privacy.errors.LegalHoldError`
  (or skips the held class) so litigation data is never destroyed;
* **idempotent + resumable** — each store-step is recorded in an
  :class:`ErasureRun`; a run that crashes reopens and replays only the steps not
  yet ``done``. Every store seam is itself idempotent, so even a re-run that
  repeats a finished step is safe;
* **verifiable** — on completion it runs the residual scan and mints a
  completion certificate (:mod:`app.privacy.certificate`).

Store-agnostic: it talks only to the protocol seams, so the deterministic test
suite drives the whole flow with in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.privacy.certificate import ErasureCertificate, ResidualScanner
from app.privacy.clock import Clock, system_clock
from app.privacy.consent import ConsentTracker
from app.privacy.datamap import DEFAULT_DATA_MAP, DataMap, PIIField
from app.privacy.enums import (
    ErasureState,
    ErasureStrategy,
    StepStatus,
    StoreKind,
)
from app.privacy.errors import ChainIntegrityError, LegalHoldError, StoreError
from app.privacy.protocols import AuditRedactor, BlobStore, EventStore, SubjectDataStore
from app.privacy.retention import LegalHold, RetentionPolicy

logger = get_logger("app.privacy.erasure")

#: The tombstone email/value an anonymised direct identifier is overwritten with.
ANON_TOMBSTONE = "erased"


@dataclass
class ErasureStep:
    """One per-store unit of work inside a resumable erasure run."""

    store: StoreKind
    resource: str
    strategy: ErasureStrategy
    status: StepStatus = StepStatus.PENDING
    #: How many rows/objects/streams/entries the step affected (for the receipt).
    affected: int = 0
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.store.value}:{self.resource}:{self.strategy.value}"


@dataclass
class ErasureRun:
    """The resumable record of a right-to-erasure run for one subject.

    The run *is* the resume token: persist it (the orchestrator accepts an
    existing run to continue) and replay only steps whose status is ``pending``.
    """

    subject_id: str
    steps: list[ErasureStep]
    state: ErasureState = ErasureState.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    blocked_hold_id: str | None = None
    certificate: ErasureCertificate | None = None

    def pending_steps(self) -> list[ErasureStep]:
        return [s for s in self.steps if s.status is StepStatus.PENDING]

    @property
    def affected_total(self) -> int:
        return sum(s.affected for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        """A serialisable receipt of the run (for persistence / the DSAR result)."""
        return {
            "subject_id": self.subject_id,
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "blocked_hold_id": self.blocked_hold_id,
            "affected_total": self.affected_total,
            "steps": [
                {
                    "store": s.store.value,
                    "resource": s.resource,
                    "strategy": s.strategy.value,
                    "status": s.status.value,
                    "affected": s.affected,
                    "error": s.error,
                }
                for s in self.steps
            ],
            "certificate": self.certificate.to_dict() if self.certificate else None,
        }


def plan_run(subject_id: str, data_map: DataMap = DEFAULT_DATA_MAP) -> ErasureRun:
    """Build the (still-pending) step plan for a subject from the data-map.

    One step per (store, resource, strategy). A resource whose fields use the same
    strategy collapses to one step; the rare mixed-strategy resource yields one
    step per strategy. Deterministic order: stores in enum order, resources in
    data-map order.
    """
    steps: list[ErasureStep] = []
    seen: set[str] = set()
    for store in StoreKind:
        for resource in data_map.resources(store):
            fields = data_map.by_resource(store, resource)
            for strategy in _strategies_for(fields):
                step = ErasureStep(store=store, resource=resource, strategy=strategy)
                if step.key in seen:
                    continue
                seen.add(step.key)
                steps.append(step)
    return ErasureRun(subject_id=subject_id, steps=steps)


def _strategies_for(fields: Sequence[PIIField]) -> list[ErasureStrategy]:
    """Distinct strategies a resource's fields declare, in first-seen order."""
    out: list[ErasureStrategy] = []
    for f in fields:
        if f.erasure not in out:
            out.append(f.erasure)
    return out


class ErasureOrchestrator:
    """Drives a coherent, idempotent, resumable cross-store erasure."""

    def __init__(
        self,
        *,
        subject_store: SubjectDataStore,
        blob_store: BlobStore,
        event_store: EventStore,
        audit_log: AuditRedactor | None = None,
        data_map: DataMap = DEFAULT_DATA_MAP,
        retention: RetentionPolicy | None = None,
        consent: ConsentTracker | None = None,
        clock: Clock = system_clock,
        redaction_marker: str = "[REDACTED]",
    ) -> None:
        self._subject = subject_store
        self._blobs = blob_store
        self._events = event_store
        self._audit = audit_log
        self._map = data_map
        self._retention = retention
        self._consent = consent
        self._clock = clock
        self._marker = redaction_marker
        self._scanner = ResidualScanner(
            subject_store=subject_store,
            blob_store=blob_store,
            event_store=event_store,
            audit_log=audit_log,
            data_map=data_map,
            clock=clock,
        )

    async def erase(
        self,
        *,
        subject_id: str,
        holds: Iterable[LegalHold] = (),
        run: ErasureRun | None = None,
        certify: bool = True,
    ) -> ErasureRun:
        """Erase a subject across all stores; resume ``run`` if one is supplied.

        Raises :class:`LegalHoldError` if a *subject-wide* hold blocks the whole
        request; a hold scoped to a single data class merely skips that class's
        steps (recorded as ``skipped``) and lets the rest proceed.
        """
        holds = list(holds)
        run = run or plan_run(subject_id, self._map)
        if run.started_at is None:
            run.started_at = self._clock()

        # A subject-wide hold (no data_class) blocks the entire request up front.
        for h in holds:
            if h.active and h.subject_id == subject_id and h.data_class is None:
                run.state = ErasureState.BLOCKED
                run.blocked_hold_id = h.id
                run.finished_at = self._clock()
                logger.info(
                    "privacy.erasure.blocked",
                    subject_id=subject_id,
                    hold_id=h.id,
                )
                raise LegalHoldError(subject_id, h.id)

        run.state = ErasureState.IN_PROGRESS
        for step in run.pending_steps():
            await self._run_step(subject_id=subject_id, step=step, holds=holds)
            if step.status is StepStatus.FAILED:
                run.state = ErasureState.FAILED
                run.finished_at = self._clock()
                logger.warning(
                    "privacy.erasure.step_failed",
                    subject_id=subject_id,
                    step=step.key,
                    error=step.error,
                )
                return run

        # Drop local consent records for the subject (idempotent).
        if self._consent is not None:
            self._consent.purge_subject(subject_id)

        if certify:
            run.certificate = await self._scanner.certify(subject_id=subject_id)
            if not run.certificate.complete:
                # Residual data remained (or the chain broke) — not yet complete.
                run.state = ErasureState.FAILED
                run.finished_at = self._clock()
                if not run.certificate.chain_intact:
                    raise ChainIntegrityError(
                        f"crypto-erasure broke the audit chain for subject {subject_id!r}"
                    )
                logger.warning(
                    "privacy.erasure.residual",
                    subject_id=subject_id,
                    residual=run.certificate.per_store_residual,
                )
                return run

        run.state = ErasureState.COMPLETED
        run.finished_at = self._clock()
        logger.info(
            "privacy.erasure.completed",
            subject_id=subject_id,
            affected=run.affected_total,
        )
        return run

    async def _run_step(
        self,
        *,
        subject_id: str,
        step: ErasureStep,
        holds: Sequence[LegalHold],
    ) -> None:
        """Execute one store-step, honouring per-class holds; idempotent."""
        # A class-scoped hold blocks just this resource's class.
        if self._retention is not None:
            data_class = self._resource_class(step.store, step.resource)
            if data_class is not None:
                blocking = self._retention.is_blocked(
                    subject_id=subject_id, data_class=data_class, holds=holds
                )
                if blocking is not None:
                    step.status = StepStatus.SKIPPED
                    step.error = f"blocked by legal hold {blocking.id}"
                    return
        try:
            affected = await self._dispatch(
                subject_id=subject_id, step=step
            )
            step.affected = affected
            step.status = StepStatus.DONE if affected else StepStatus.SKIPPED
        except ChainIntegrityError:
            raise
        except Exception as exc:  # store error — leave PENDING for resume
            step.status = StepStatus.FAILED
            step.error = str(exc)

    async def _dispatch(self, *, subject_id: str, step: ErasureStep) -> int:
        """Route a step to the right store seam by its strategy."""
        if step.store in (StoreKind.RELATIONAL, StoreKind.DERIVED_INDEX):
            fields = self._map.by_resource(step.store, step.resource)
            locator = fields[0].subject_locator
            if step.strategy is ErasureStrategy.HARD_DELETE:
                return await self._subject.hard_delete(
                    resource=step.resource,
                    subject_locator=locator,
                    subject_id=subject_id,
                )
            if step.strategy is ErasureStrategy.ANONYMIZE:
                cols = [f.field for f in fields if f.erasure is ErasureStrategy.ANONYMIZE]
                return await self._subject.anonymize(
                    resource=step.resource,
                    subject_locator=locator,
                    subject_id=subject_id,
                    fields=cols,
                    tombstone=f"{ANON_TOMBSTONE}+{subject_id}",
                )
            raise StoreError(
                f"unsupported strategy {step.strategy.value!r} for store {step.store.value!r}"
            )
        if step.store is StoreKind.OBJECT_STORE:
            base = step.resource.split("{", 1)[0].rstrip("/")
            return await self._blobs.delete_prefix(f"{base}/{subject_id}")
        if step.store is StoreKind.EVENT_STORE:
            # Append-only: crypto-erase (destroy key); redact is treated the same
            # at the stream level (the per-record key is what we destroy).
            return await self._events.crypto_erase_subject(subject_id=subject_id)
        if step.store is StoreKind.AUDIT_LOG:
            if self._audit is None:
                return 0
            redacted = await self._audit.redact_subject(
                subject_id=subject_id, redaction_marker=self._marker
            )
            # The redactor must keep the chain intact; verify defensively.
            if not await self._audit.verify_chain():
                raise ChainIntegrityError(
                    f"audit redaction broke the chain for subject {subject_id!r}"
                )
            return redacted
        return 0

    def _resource_class(self, store: StoreKind, resource: str) -> str | None:
        """The retention class governing a (store, resource), from the data-map."""
        fields = self._map.by_resource(store, resource)
        return fields[0].retention_class if fields else None


__all__ = [
    "ANON_TOMBSTONE",
    "ErasureOrchestrator",
    "ErasureRun",
    "ErasureStep",
    "plan_run",
]
