"""Dunning — the failed-payment retry schedule + state machine (pure).

When an invoice payment fails, the subscription goes ``past_due`` and a
**dunning** schedule begins: a fixed sequence of retry delays (e.g. 1d, 3d, 5d,
7d). Each retry either settles (invoice paid, subscription active again) or fails
and advances to the next delay. When the schedule is exhausted, the invoice is
marked ``uncollectible`` and the subscription becomes ``unpaid`` (access gated by
the entitlements layer).

This mirrors the render queue's backoff discipline (§12.1: 2s/8s/30s with a
dead-letter path) but at the billing cadence. The schedule and the state
transitions are **pure** — given a current attempt count and the last failure
time, :func:`next_retry_at` says when to try again, and :class:`DunningState`
tracks where we are. Persisting the attempts is the repository's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.billing.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus

#: Default retry cadence in days after each failed attempt (Stripe-like).
DEFAULT_RETRY_DAYS: tuple[int, ...] = (1, 3, 5, 7)


@dataclass(frozen=True, slots=True)
class DunningSchedule:
    """A retry cadence: delays (in days) after each successive failure."""

    retry_days: tuple[int, ...] = DEFAULT_RETRY_DAYS

    def __post_init__(self) -> None:
        if not self.retry_days:
            raise ValueError("dunning schedule needs at least one retry delay")
        if any(d <= 0 for d in self.retry_days):
            raise ValueError("retry delays must be positive")

    @property
    def max_attempts(self) -> int:
        """Total payment attempts before giving up (initial + each retry)."""
        return len(self.retry_days) + 1

    def delay_after(self, attempt_index: int) -> timedelta | None:
        """Delay to wait after the ``attempt_index``-th failed attempt (0-based).

        Returns ``None`` when the schedule is exhausted (no further retry).
        """
        if attempt_index < 0:
            raise ValueError("attempt_index must be >= 0")
        if attempt_index >= len(self.retry_days):
            return None
        return timedelta(days=self.retry_days[attempt_index])


def next_retry_at(
    schedule: DunningSchedule, *, attempt_index: int, last_attempt_at: datetime
) -> datetime | None:
    """When to retry after the ``attempt_index``-th failure (None if exhausted)."""
    if last_attempt_at.tzinfo is None:
        raise ValueError("last_attempt_at must be timezone-aware (UTC)")
    delay = schedule.delay_after(attempt_index)
    return None if delay is None else last_attempt_at + delay


@dataclass(frozen=True, slots=True)
class DunningTransition:
    """The outcome of recording one payment attempt against an invoice."""

    invoice_status: InvoiceStatus
    subscription_status: SubscriptionStatus
    next_retry_at: datetime | None
    exhausted: bool


@dataclass
class DunningState:
    """Tracks dunning progress for one invoice; computes the next transition."""

    schedule: DunningSchedule = field(default_factory=DunningSchedule)
    attempts: int = 0  # number of attempts already made (failed or otherwise)

    def record_attempt(
        self,
        outcome: PaymentStatus,
        *,
        at: datetime,
        current_sub_status: SubscriptionStatus,
    ) -> DunningTransition:
        """Apply a payment ``outcome`` and compute the resulting transition.

        * SUCCEEDED -> invoice paid, subscription active, dunning ends.
        * FAILED with retries left -> invoice stays open, subscription past_due,
          a ``next_retry_at`` is scheduled.
        * FAILED with no retries left -> invoice uncollectible, subscription
          unpaid, dunning exhausted.
        """
        if at.tzinfo is None:
            raise ValueError("attempt time must be timezone-aware (UTC)")
        self.attempts += 1

        if outcome is PaymentStatus.SUCCEEDED:
            return DunningTransition(
                invoice_status=InvoiceStatus.PAID,
                subscription_status=SubscriptionStatus.ACTIVE,
                next_retry_at=None,
                exhausted=False,
            )

        if outcome is PaymentStatus.REQUIRES_ACTION:
            # Awaiting customer action (SCA); keep open, no auto-retry yet.
            return DunningTransition(
                invoice_status=InvoiceStatus.OPEN,
                subscription_status=current_sub_status,
                next_retry_at=None,
                exhausted=False,
            )

        # FAILED / CANCELED / PENDING-as-failed: this failed attempt is index
        # (attempts - 1); decide whether a retry remains.
        failed_index = self.attempts - 1
        retry_at = next_retry_at(self.schedule, attempt_index=failed_index, last_attempt_at=at)
        if retry_at is None:
            return DunningTransition(
                invoice_status=InvoiceStatus.UNCOLLECTIBLE,
                subscription_status=SubscriptionStatus.UNPAID,
                next_retry_at=None,
                exhausted=True,
            )
        return DunningTransition(
            invoice_status=InvoiceStatus.OPEN,
            subscription_status=SubscriptionStatus.PAST_DUE,
            next_retry_at=retry_at,
            exhausted=False,
        )

    @property
    def is_exhausted(self) -> bool:
        """True once the number of attempts has reached the schedule's cap."""
        return self.attempts >= self.schedule.max_attempts


__all__ = [
    "DEFAULT_RETRY_DAYS",
    "DunningSchedule",
    "DunningState",
    "DunningTransition",
    "next_retry_at",
]
