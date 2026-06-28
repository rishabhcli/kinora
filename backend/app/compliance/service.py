"""The :class:`ComplianceService` facade.

Wires the per-concern services (consent, retention, hold, DSAR, ledger, policy)
over a single :class:`~sqlalchemy.ext.asyncio.AsyncSession` and a shared
:class:`~app.compliance.clock.Clock`, and adds the cross-cutting operations that
need several of them at once:

* :meth:`facts` — gather a subject's full compliance picture for the rule engine;
* :meth:`report` — evaluate the policy rules and return a consolidated report;
* :meth:`bootstrap` — idempotently seed the consent catalog + retention schedule.

The DSAR fulfilment seam (``dataportability`` export/erasure) is injected here so
the whole subsystem can run end-to-end with a no-op fulfiller in tests and the
real one in production, without the compliance code importing dataportability.

This is *not* registered in :mod:`app.composition` by this agent (that file is
shared and additive-only); the API layer constructs a service per request from
``container.session_factory`` (see :func:`app.compliance.api.routes.get_service`).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.clock import Clock, system_clock
from app.compliance.consent.policy import DEFAULT_PURPOSE_CATALOG, PurposeSpec
from app.compliance.consent.service import ConsentService
from app.compliance.dsar.service import DSARService, Fulfiller
from app.compliance.enums import ConsentState, ProcessingPurpose
from app.compliance.hold.service import LegalHoldService
from app.compliance.ledger.service import ComplianceLedger
from app.compliance.policy.engine import PolicyEngine
from app.compliance.policy.report import ComplianceReport, build_report
from app.compliance.policy.rules import ComplianceFacts
from app.compliance.repositories.consent import ConsentPolicyRepo, ConsentRecordRepo
from app.compliance.repositories.dsar import DSARRepo
from app.compliance.repositories.hold import LegalHoldRepo
from app.compliance.repositories.ledger import ComplianceLedgerRepo
from app.compliance.repositories.retention import RetentionRuleRepo
from app.compliance.retention.classes import DEFAULT_RETENTION_SCHEDULE, RetentionSpec
from app.compliance.retention.engine import RetentionEngine


class ComplianceService:
    """One-stop facade over the compliance subsystem for a single DB session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Clock = system_clock,
        fulfiller: Fulfiller | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self.session = session
        self._clock = clock

        # Shared consolidated ledger (every service writes to it).
        self.ledger = ComplianceLedger(ComplianceLedgerRepo(session))

        self.consent = ConsentService(
            ConsentPolicyRepo(session),
            ConsentRecordRepo(session),
            self.ledger,
            clock=clock,
        )
        self.holds = LegalHoldService(LegalHoldRepo(session), self.ledger, clock=clock)

        # The retention engine consults consent + holds via injected lookups so it
        # stays decoupled from those services' concrete shapes.
        self.retention = RetentionEngine(
            RetentionRuleRepo(session),
            self.ledger,
            clock=clock,
            consent_lookup=self._consent_state,
            hold_lookup=self.holds.is_held,
        )
        self.dsar = DSARService(
            DSARRepo(session),
            self.ledger,
            self.holds,
            clock=clock,
            fulfiller=fulfiller,
        )
        self._policy_engine = policy_engine or PolicyEngine()

    async def _consent_state(self, subject_id: str, purpose: ProcessingPurpose) -> ConsentState:
        """Adapter exposing consent state as the retention engine's lookup type."""
        return (await self.consent.consent_for(subject_id, purpose)).state

    # --- bootstrap ---------------------------------------------------------- #

    async def bootstrap(
        self,
        *,
        catalog: tuple[PurposeSpec, ...] = DEFAULT_PURPOSE_CATALOG,
        schedule: tuple[RetentionSpec, ...] = DEFAULT_RETENTION_SCHEDULE,
    ) -> None:
        """Idempotently seed the consent catalog and the retention schedule."""
        await self.consent.seed_catalog(catalog)
        await self.retention.seed_schedule(schedule)

    async def required_purposes(self) -> frozenset[ProcessingPurpose]:
        """The purposes whose active policy is marked ``required``."""
        active = await self.consent._policies.list_active()  # noqa: SLF001 - same package
        return frozenset(p.purpose for p in active if p.required)

    # --- cross-cutting: facts + report -------------------------------------- #

    async def facts(self, subject_id: str) -> ComplianceFacts:
        """Gather a subject's full compliance picture for the rule engine."""
        consent = await self.consent.snapshot(subject_id)
        hold = await self.holds.scope(subject_id)
        dsars = await self.dsar.list_for_subject(subject_id)
        required = await self.required_purposes()
        return ComplianceFacts(
            subject_id=subject_id,
            now=self._clock(),
            consent=consent,
            hold=hold,
            dsars=tuple(dsars),
            required_purposes=required,
        )

    async def report(self, subject_id: str) -> ComplianceReport:
        """Evaluate the policy rules for a subject and return a consolidated report."""
        facts = await self.facts(subject_id)
        return build_report(facts, self._policy_engine)


__all__ = ["ComplianceService"]
