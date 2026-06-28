"""Tests for the dunning retry schedule + state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.billing.dunning import (
    DEFAULT_RETRY_DAYS,
    DunningSchedule,
    DunningState,
    next_retry_at,
)
from app.billing.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus

T = datetime(2026, 6, 1, tzinfo=UTC)


def test_schedule_validation() -> None:
    with pytest.raises(ValueError):
        DunningSchedule(retry_days=())
    with pytest.raises(ValueError):
        DunningSchedule(retry_days=(1, 0, 3))


def test_default_schedule_max_attempts() -> None:
    sched = DunningSchedule()
    assert sched.retry_days == DEFAULT_RETRY_DAYS
    assert sched.max_attempts == len(DEFAULT_RETRY_DAYS) + 1


def test_delay_after_and_exhaustion() -> None:
    sched = DunningSchedule(retry_days=(1, 3))
    assert sched.delay_after(0) == timedelta(days=1)
    assert sched.delay_after(1) == timedelta(days=3)
    assert sched.delay_after(2) is None  # exhausted
    with pytest.raises(ValueError):
        sched.delay_after(-1)


def test_next_retry_at() -> None:
    sched = DunningSchedule(retry_days=(1, 3))
    assert next_retry_at(sched, attempt_index=0, last_attempt_at=T) == T + timedelta(days=1)
    assert next_retry_at(sched, attempt_index=1, last_attempt_at=T) == T + timedelta(days=3)
    assert next_retry_at(sched, attempt_index=2, last_attempt_at=T) is None


def test_next_retry_requires_aware() -> None:
    sched = DunningSchedule()
    with pytest.raises(ValueError):
        next_retry_at(sched, attempt_index=0, last_attempt_at=datetime(2026, 6, 1))  # noqa: DTZ001


def test_success_settles() -> None:
    state = DunningState(schedule=DunningSchedule(retry_days=(1, 3)))
    transition = state.record_attempt(
        PaymentStatus.SUCCEEDED, at=T, current_sub_status=SubscriptionStatus.PAST_DUE
    )
    assert transition.invoice_status is InvoiceStatus.PAID
    assert transition.subscription_status is SubscriptionStatus.ACTIVE
    assert transition.next_retry_at is None
    assert not transition.exhausted


def test_first_failure_schedules_retry() -> None:
    state = DunningState(schedule=DunningSchedule(retry_days=(1, 3)))
    transition = state.record_attempt(
        PaymentStatus.FAILED, at=T, current_sub_status=SubscriptionStatus.ACTIVE
    )
    assert transition.invoice_status is InvoiceStatus.OPEN
    assert transition.subscription_status is SubscriptionStatus.PAST_DUE
    assert transition.next_retry_at == T + timedelta(days=1)
    assert not transition.exhausted


def test_exhaustion_after_all_retries() -> None:
    state = DunningState(schedule=DunningSchedule(retry_days=(1, 3)))
    # attempt 1 (initial) fails -> retry in 1d
    state.record_attempt(PaymentStatus.FAILED, at=T, current_sub_status=SubscriptionStatus.ACTIVE)
    # attempt 2 fails -> retry in 3d
    t2 = state.record_attempt(
        PaymentStatus.FAILED,
        at=T + timedelta(days=1),
        current_sub_status=SubscriptionStatus.PAST_DUE,
    )
    assert t2.next_retry_at == T + timedelta(days=1) + timedelta(days=3)
    # attempt 3 fails -> exhausted (only 2 retry delays).
    t3 = state.record_attempt(
        PaymentStatus.FAILED,
        at=T + timedelta(days=4),
        current_sub_status=SubscriptionStatus.PAST_DUE,
    )
    assert t3.exhausted
    assert t3.invoice_status is InvoiceStatus.UNCOLLECTIBLE
    assert t3.subscription_status is SubscriptionStatus.UNPAID
    assert state.is_exhausted


def test_requires_action_holds() -> None:
    state = DunningState()
    transition = state.record_attempt(
        PaymentStatus.REQUIRES_ACTION, at=T, current_sub_status=SubscriptionStatus.ACTIVE
    )
    assert transition.invoice_status is InvoiceStatus.OPEN
    assert transition.subscription_status is SubscriptionStatus.ACTIVE
    assert transition.next_retry_at is None
    assert not transition.exhausted


def test_record_attempt_requires_aware() -> None:
    state = DunningState()
    with pytest.raises(ValueError):
        state.record_attempt(
            PaymentStatus.FAILED,
            at=datetime(2026, 6, 1),  # noqa: DTZ001
            current_sub_status=SubscriptionStatus.ACTIVE,
        )
