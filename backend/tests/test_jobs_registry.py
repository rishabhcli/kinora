"""Unit tests for the job registry + ``@job`` decorator (no infra)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from app.jobs.backoff import BackoffPolicy
from app.jobs.registry import JobRegistry, default_idempotency_key, job
from app.jobs.triggers import ManualTrigger, cron, every
from app.jobs.types import JobContext, JobResult, ScheduledJobState


async def _noop(ctx: JobContext) -> JobResult:
    return JobResult.ok()


def test_default_idempotency_key_truncates_to_minute() -> None:
    a = default_idempotency_key("j", datetime(2026, 1, 1, 3, 30, 15, tzinfo=UTC), {})
    b = default_idempotency_key("j", datetime(2026, 1, 1, 3, 30, 59, tzinfo=UTC), {})
    assert a == b
    assert a.startswith("j@2026-01-01T03:30")


def test_register_and_lookup() -> None:
    reg = JobRegistry()

    @job("alpha", trigger=every(60), registry=reg)
    async def _h(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    assert "alpha" in reg
    assert len(reg) == 1
    assert reg.require("alpha").name == "alpha"
    assert reg.names() == ["alpha"]


def test_duplicate_registration_rejected() -> None:
    reg = JobRegistry()
    job("dup", registry=reg)(_noop)
    with pytest.raises(ValueError, match="duplicate"):
        job("dup", registry=reg)(_noop)


def test_require_missing_raises_keyerror() -> None:
    reg = JobRegistry()
    assert reg.get("nope") is None
    with pytest.raises(KeyError):
        reg.require("nope")


def test_max_attempts_shorthand_overrides_backoff_cap() -> None:
    reg = JobRegistry()
    job("retry5", max_attempts=5, registry=reg)(_noop)
    assert reg.require("retry5").backoff.max_attempts == 5
    assert reg.require("retry5").max_attempts == 5


def test_explicit_backoff_is_used() -> None:
    reg = JobRegistry()
    policy = BackoffPolicy(max_attempts=7, base_delay_s=1.0, factor=2.0)
    job("custom", backoff=policy, registry=reg)(_noop)
    assert reg.require("custom").backoff is policy


def test_no_trigger_defaults_to_manual_and_excluded_from_scheduled() -> None:
    reg = JobRegistry()
    job("manualjob", registry=reg)(_noop)
    job("cronjob", trigger=cron("0 * * * *"), registry=reg)(_noop)
    definition = reg.require("manualjob")
    assert isinstance(definition.trigger, ManualTrigger)
    scheduled_names = [d.name for d in reg.scheduled()]
    assert scheduled_names == ["cronjob"]


def test_description_falls_back_to_docstring_first_line() -> None:
    reg = JobRegistry()

    @job("documented", registry=reg)
    async def _h(ctx: JobContext) -> JobResult:
        """First line of docs.

        More detail.
        """
        return JobResult.ok()

    assert reg.require("documented").description == "First line of docs."


def test_idempotency_key_override() -> None:
    reg = JobRegistry()

    def custom_key(name: str, scheduled_for: datetime, payload: Mapping[str, object]) -> str:
        return f"{name}:{payload.get('book_id', 'none')}"

    job("bybook", idempotency_key=custom_key, registry=reg)(_noop)
    definition = reg.require("bybook")
    key = definition.idempotency_key(datetime(2026, 1, 1, tzinfo=UTC), {"book_id": "b1"})
    assert key == "bybook:b1"


def test_default_state_carried_through() -> None:
    reg = JobRegistry()
    job("paused", default_state=ScheduledJobState.PAUSED, registry=reg)(_noop)
    assert reg.require("paused").default_state is ScheduledJobState.PAUSED


def test_decorator_returns_handler_unchanged() -> None:
    reg = JobRegistry()

    @job("identity", registry=reg)
    async def handler(ctx: JobContext) -> JobResult:
        return JobResult.ok()

    assert handler is reg.require("identity").handler
