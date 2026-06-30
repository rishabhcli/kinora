"""The capacity oracle — the read surface the router/scheduler asks before submitting.

The scheduler reserves video-seconds and the router picks a backend; both want a
single, cheap question answered for a provider: **"can you take another render of
this cost right now, and if not, when?"** The oracle composes the three governor
sub-systems into that answer:

* the :class:`~app.video.governor.quota.QuotaAccountant` (is there quota headroom on
  every axis?),
* the :class:`~app.video.governor.throttle.ProviderThrottle` (are we paced/parked
  behind a Retry-After?),
* the :class:`~app.video.governor.sla.SlaTracker` (is the provider healthy enough to
  prefer?).

It returns an :class:`CapacityVerdict` with an admit/deny decision, the *reason*
when denied, an estimated *seconds until free*, and the provider's current SLA
grade so a caller can rank providers (lowest grade rank + admitted first). The
oracle is **read-only** — it never reserves quota or consumes a pacing slot; the
governor's ``submit`` path does the mutation. This keeps the router free to poll
the oracle for several providers without side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .clock import Clock, monotonic
from .quota import QuotaAccountant, QuotaDecision, RenderCost
from .sla import SlaGrade, SlaSnapshot, SlaTracker
from .throttle import ProviderThrottle


class DenyReason(StrEnum):
    """Why the oracle would deny an immediate submission."""

    QUOTA = "quota"  # a quota dimension would be exceeded (hard block)
    THROTTLED = "throttled"  # paced / parked behind Retry-After (hard block)
    UNHEALTHY = "unhealthy"  # SLA grade is F (breached) — advisory, deprioritised


@dataclass(frozen=True, slots=True)
class CapacityVerdict:
    """The oracle's answer for one provider and one prospective render cost.

    Two distinct ideas live here. ``admit`` is a *hard* capacity gate: it is False
    only when a quota ceiling would be breached or the provider is paced/parked
    behind a Retry-After — the caller must not submit. The SLA grade is an
    *advisory* health signal: a grade-F provider stays admittable (so it can recover
    via continued, deprioritised traffic — like a breaker's half-open probe), but it
    sorts last via :attr:`rank_key` so a healthy alternative wins routing. ``reason``
    surfaces the dominant signal (health first, for telemetry) even when admittable.
    """

    provider: str
    admit: bool
    #: The dominant signal: a hard block (quota/throttle) or the advisory UNHEALTHY.
    #: ``None`` only when admittable *and* healthy.
    reason: DenyReason | None
    #: Estimated wait until the provider could take this render (0 when admit).
    seconds_until_free: float
    #: The provider's current SLA grade (for ranking even when admitted).
    grade: SlaGrade
    #: Highest quota utilisation across bounded axes (0..1+), for ranking/telemetry.
    quota_utilisation: float
    #: The throttle wait component (so callers can distinguish pacing from quota).
    throttle_wait_s: float

    @property
    def unhealthy(self) -> bool:
        """True when the provider's SLA is breached (grade F) — route elsewhere."""
        return self.grade is SlaGrade.F

    @property
    def rank_key(self) -> tuple[int, int, int, float, float]:
        """A sort key — lower is better. Admitted-and-healthy providers sort first.

        ``(not admit, grade.rank, unhealthy, seconds_until_free, quota_utilisation)``:
        a hard-blocked provider sinks below every admittable one; among admittable
        providers an A-grade beats an F-grade, so a breached-but-admittable provider
        is chosen only when nothing healthier is available. Ties break on how soon
        it'll be free, then how loaded it is.
        """
        return (
            0 if self.admit else 1,
            self.grade.rank,
            1 if self.unhealthy else 0,
            self.seconds_until_free,
            self.quota_utilisation,
        )


class CapacityOracle:
    """Compose quota + throttle + SLA into capacity answers for one provider."""

    def __init__(
        self,
        provider: str,
        accountant: QuotaAccountant,
        throttle: ProviderThrottle,
        sla: SlaTracker,
        *,
        clock: Clock = monotonic,
    ) -> None:
        self.provider = provider
        self._accountant = accountant
        self._throttle = throttle
        self._sla = sla
        self._clock = clock

    async def can_take(self, cost: RenderCost) -> CapacityVerdict:
        """Answer "can you take a render of ``cost`` now? when free?" (read-only).

        Precedence of the deny reason when several apply: an exhausted SLA (grade F)
        means *don't route here at all* and dominates; then a Retry-After/pacing
        park; then a quota ceiling. ``seconds_until_free`` is the throttle wait when
        only pacing blocks; quota windows (rpm/daily/monthly) don't expose a precise
        next-free instant here, so a quota denial reports the throttle wait (often 0)
        and the caller treats quota-blocked as "try another provider".
        """
        quota: QuotaDecision = await self._accountant.check(cost)
        snap: SlaSnapshot = self._sla.snapshot()
        state = self._throttle.state()
        throttle_wait = state.wait_s

        # Hard capacity gate: only quota and throttle block admission.
        hard_block: DenyReason | None = None
        if throttle_wait > 0:
            hard_block = DenyReason.THROTTLED
        elif not quota.admitted:
            hard_block = DenyReason.QUOTA
        admit = hard_block is None

        # Dominant reason for telemetry: health first (advisory), then the hard
        # block. ``None`` only when admittable *and* healthy.
        if snap.grade is SlaGrade.F:
            reason: DenyReason | None = DenyReason.UNHEALTHY
        else:
            reason = hard_block

        seconds_until_free = 0.0 if admit else throttle_wait
        return CapacityVerdict(
            provider=self.provider,
            admit=admit,
            reason=reason,
            seconds_until_free=seconds_until_free,
            grade=snap.grade,
            quota_utilisation=quota.max_utilisation,
            throttle_wait_s=throttle_wait,
        )

    async def utilisation_snapshot(self) -> tuple[QuotaDecision, SlaSnapshot]:
        """Current quota utilisation + SLA snapshot (for the metrics panel)."""
        return await self._accountant.utilisation(), self._sla.snapshot()


def best_provider(verdicts: list[CapacityVerdict]) -> CapacityVerdict | None:
    """Pick the most routable verdict (lowest :attr:`CapacityVerdict.rank_key`)."""
    if not verdicts:
        return None
    return min(verdicts, key=lambda v: v.rank_key)


__all__ = [
    "CapacityOracle",
    "CapacityVerdict",
    "DenyReason",
    "best_provider",
]
