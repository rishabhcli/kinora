"""The retention policy engine.

Given a batch of stored items (each tagged with a data class, a subject, and a
reference timestamp), the engine decides — per item — whether it is an **expiry
candidate** under the active retention schedule, taking into account:

* the per-class TTL (``ttl_days``); ``None`` == retain indefinitely;
* **consent withdrawal**: a class whose only lawful basis is consent expires
  immediately once the relevant consent is withdrawn (Art. 5(1)(e));
* **legal holds**: a held subject/class is *never* an expiry candidate, even if
  the TTL elapsed — the hold suspends retention.

The engine never deletes anything. It produces :class:`ExpiryCandidate` decisions
that the ``dataportability`` eraser (or a retention sweep job) acts on, and writes
a ``retention.sweep`` summary to the compliance ledger. This is the deliberate
split documented in ``DESIGN.md``: compliance *decides*, dataportability *erases*.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.compliance.clock import Clock, ensure_utc, system_clock
from app.compliance.db.models import RetentionRule
from app.compliance.enums import (
    ConsentState,
    DataClass,
    LedgerCategory,
    ProcessingPurpose,
)
from app.compliance.ledger.service import ComplianceLedger
from app.compliance.repositories.retention import RetentionRuleRepo
from app.compliance.retention.classes import (
    DEFAULT_RETENTION_SCHEDULE,
    SCHEDULE_BY_CLASS,
    RetentionSpec,
)


@dataclass(frozen=True)
class RetentionItem:
    """One stored object the engine evaluates for expiry.

    ``reference_at`` is the timestamp the TTL counts from (e.g. last-used for
    media, created-at for a session). ``ref`` is an opaque locator the caller
    later hands to the eraser (a row id, an object-store key, …).
    """

    data_class: DataClass
    subject_id: str
    reference_at: datetime
    ref: str


@dataclass(frozen=True)
class ExpiryCandidate:
    """A retention decision: this item should (or should not) be expired."""

    item: RetentionItem
    expired: bool
    reason: str
    #: When the TTL elapsed (None for indefinite-retention or consent-driven cases).
    expires_at: datetime | None = None
    #: True when a legal hold suspended an otherwise-due expiry.
    held: bool = False


@dataclass(frozen=True)
class RetentionDecision:
    """The outcome of evaluating a batch — candidates + a summary count."""

    candidates: tuple[ExpiryCandidate, ...]
    evaluated: int
    expired: int
    held: int

    @property
    def to_expire(self) -> tuple[ExpiryCandidate, ...]:
        """The subset that are genuine expiry candidates (expired & not held)."""
        return tuple(c for c in self.candidates if c.expired)


#: A predicate the engine calls to learn a subject's consent state for a purpose
#: (injected so the engine does not depend on the consent service directly).
ConsentLookup = Callable[[str, ProcessingPurpose], Awaitable[ConsentState]]
#: A predicate the engine calls to learn whether a (subject, class) is held.
HoldLookup = Callable[[str, DataClass], Awaitable[bool]]


async def _never_withdrawn(_subject: str, _purpose: ProcessingPurpose) -> ConsentState:
    return ConsentState.GRANTED


async def _never_held(_subject: str, _data_class: DataClass) -> bool:
    return False


class RetentionEngine:
    """Evaluate retention rules into expiry candidates (hold- and consent-aware)."""

    def __init__(
        self,
        rules: RetentionRuleRepo,
        ledger: ComplianceLedger,
        *,
        clock: Clock = system_clock,
        consent_lookup: ConsentLookup | None = None,
        hold_lookup: HoldLookup | None = None,
    ) -> None:
        self._rules = rules
        self._ledger = ledger
        self._clock = clock
        self._consent = consent_lookup or _never_withdrawn
        self._hold = hold_lookup or _never_held

    # --- schedule management ------------------------------------------------ #

    async def seed_schedule(
        self, schedule: Sequence[RetentionSpec] = DEFAULT_RETENTION_SCHEDULE
    ) -> list[RetentionRule]:
        """Idempotently upsert the baseline retention schedule into the DB."""
        out: list[RetentionRule] = []
        for spec in schedule:
            rule = await self._rules.upsert(
                data_class=spec.data_class,
                ttl_days=spec.ttl_days,
                lawful_basis=spec.lawful_basis,
                expire_on_consent_withdrawal=spec.expire_on_consent_withdrawal,
                description=spec.description or None,
            )
            out.append(rule)
        return out

    async def _rule_for(self, data_class: DataClass) -> RetentionRule | RetentionSpec | None:
        """The DB rule for a class, falling back to the shipped baseline spec."""
        rule = await self._rules.get(data_class)
        if rule is not None:
            return rule
        return SCHEDULE_BY_CLASS.get(data_class)

    # --- evaluation --------------------------------------------------------- #

    async def evaluate(self, items: Iterable[RetentionItem]) -> RetentionDecision:
        """Decide, per item, whether it is an expiry candidate."""
        now = self._clock()
        candidates: list[ExpiryCandidate] = []
        for item in items:
            candidates.append(await self._evaluate_one(item, now))
        expired = sum(1 for c in candidates if c.expired)
        held = sum(1 for c in candidates if c.held)
        return RetentionDecision(
            candidates=tuple(candidates),
            evaluated=len(candidates),
            expired=expired,
            held=held,
        )

    async def _evaluate_one(self, item: RetentionItem, now: datetime) -> ExpiryCandidate:
        rule = await self._rule_for(item.data_class)
        if rule is None:
            return ExpiryCandidate(item=item, expired=False, reason="no retention rule")

        ttl_days = rule.ttl_days
        expire_on_withdrawal = bool(rule.expire_on_consent_withdrawal)
        consent_purpose = _purpose_for(item.data_class)

        # 1) Consent-withdrawal expiry: a consent-only class with consent gone is
        #    due immediately, regardless of TTL.
        consent_due = False
        if expire_on_withdrawal and consent_purpose is not None:
            state = await self._consent(item.subject_id, consent_purpose)
            consent_due = state != ConsentState.GRANTED

        # 2) TTL expiry: due when reference_at + ttl <= now (skipped if ttl is None).
        expires_at: datetime | None = None
        ttl_due = False
        if ttl_days is not None:
            expires_at = ensure_utc(item.reference_at) + timedelta(days=ttl_days)
            ttl_due = expires_at <= ensure_utc(now)

        due = consent_due or ttl_due
        if not due:
            return ExpiryCandidate(
                item=item,
                expired=False,
                reason="within retention period",
                expires_at=expires_at,
            )

        # 3) Legal hold overrides any due expiry.
        if await self._hold(item.subject_id, item.data_class):
            return ExpiryCandidate(
                item=item,
                expired=False,
                reason="suspended by legal hold",
                expires_at=expires_at,
                held=True,
            )

        reason = "consent withdrawn" if consent_due else "ttl elapsed"
        return ExpiryCandidate(item=item, expired=True, reason=reason, expires_at=expires_at)

    async def sweep(self, items: Iterable[RetentionItem]) -> RetentionDecision:
        """Evaluate a batch and write a ``retention.sweep`` ledger summary."""
        decision = await self.evaluate(items)
        await self._ledger.record(
            category=LedgerCategory.RETENTION,
            event="retention.sweep",
            payload={
                "evaluated": decision.evaluated,
                "expired": decision.expired,
                "held": decision.held,
            },
        )
        return decision


def _purpose_for(data_class: DataClass) -> ProcessingPurpose | None:
    """The consent purpose tied to a data class (from the shipped schedule)."""
    spec = SCHEDULE_BY_CLASS.get(data_class)
    return spec.consent_purpose if spec is not None else None


__all__ = [
    "ConsentLookup",
    "ExpiryCandidate",
    "HoldLookup",
    "RetentionDecision",
    "RetentionEngine",
    "RetentionItem",
]
