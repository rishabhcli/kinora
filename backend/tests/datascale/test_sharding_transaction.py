"""Tests for the distributed-transaction seam: 2PC + saga (no infra)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.datascale.sharding.transaction import (
    SagaCoordinator,
    SagaError,
    SagaStep,
    TransactionOutcome,
    TwoPCError,
    TwoPhaseCommitCoordinator,
    Vote,
)

pytestmark = pytest.mark.asyncio


@dataclass
class FakeParticipant:
    """A scriptable 2PC participant recording the protocol calls it received."""

    shard_id: str
    vote: Vote = Vote.YES
    prepare_raises: bool = False
    commit_raises: int = 0  # number of commit attempts to fail before succeeding
    log: list[str] = field(default_factory=list)
    _commit_attempts: int = 0

    async def prepare(self, gid: str) -> Vote:
        self.log.append("prepare")
        if self.prepare_raises:
            raise RuntimeError("prepare blew up")
        return self.vote

    async def commit(self, gid: str) -> None:
        self._commit_attempts += 1
        self.log.append("commit")
        if self._commit_attempts <= self.commit_raises:
            raise RuntimeError("commit transient failure")

    async def abort(self, gid: str) -> None:
        self.log.append("abort")


async def test_2pc_all_yes_commits_everywhere() -> None:
    parts = [FakeParticipant("a"), FakeParticipant("b"), FakeParticipant("c")]
    result = await TwoPhaseCommitCoordinator().run("gid-1", parts)
    assert result.outcome is TransactionOutcome.COMMITTED
    assert set(result.committed) == {"a", "b", "c"}
    for p in parts:
        assert p.log == ["prepare", "commit"]


async def test_2pc_single_no_aborts_everywhere() -> None:
    parts = [FakeParticipant("a"), FakeParticipant("b", vote=Vote.NO), FakeParticipant("c")]
    with pytest.raises(TwoPCError) as ei:
        await TwoPhaseCommitCoordinator().run("gid-2", parts)
    # 'a' prepared then must be aborted; 'b' voted NO; 'c' never reached.
    assert "a" in ei.value.prepared
    assert parts[0].log == ["prepare", "abort"]
    assert parts[1].log == ["prepare"]  # voted NO, nothing to undo
    assert parts[2].log == []  # short-circuited


async def test_2pc_prepare_error_is_no_vote() -> None:
    parts = [FakeParticipant("a"), FakeParticipant("b", prepare_raises=True)]
    with pytest.raises(TwoPCError):
        await TwoPhaseCommitCoordinator().run("gid-3", parts)
    assert parts[0].log == ["prepare", "abort"]
    assert parts[1].log == ["prepare"]


async def test_2pc_commit_retries_then_succeeds() -> None:
    # 'b' fails its first commit attempt then succeeds; the run still commits.
    parts = [FakeParticipant("a"), FakeParticipant("b", commit_raises=1)]
    result = await TwoPhaseCommitCoordinator(finalize_attempts=3).run("gid-4", parts)
    assert result.outcome is TransactionOutcome.COMMITTED
    assert set(result.committed) == {"a", "b"}
    assert parts[1].log == ["prepare", "commit", "commit"]  # retried once


async def test_2pc_commit_exhausted_marks_in_doubt() -> None:
    # 'b' never succeeds at commit; it is NOT in the committed set (in-doubt).
    parts = [FakeParticipant("a"), FakeParticipant("b", commit_raises=99)]
    result = await TwoPhaseCommitCoordinator(finalize_attempts=2).run("gid-5", parts)
    assert result.outcome is TransactionOutcome.COMMITTED
    assert "a" in result.committed
    assert "b" not in result.committed  # in-doubt, reported not silently lost


async def test_2pc_empty_participants_rejected() -> None:
    with pytest.raises(ValueError):
        await TwoPhaseCommitCoordinator().run("gid", [])


# --- saga ------------------------------------------------------------------ #


async def test_saga_runs_all_steps_forward() -> None:
    order: list[str] = []

    def make(name: str) -> SagaStep:
        async def action() -> str:
            order.append(f"do-{name}")
            return name

        async def comp() -> None:
            order.append(f"undo-{name}")

        return SagaStep(name=name, action=action, compensate=comp)

    result = await SagaCoordinator().run([make("a"), make("b"), make("c")])
    assert result.completed_steps == ("a", "b", "c")
    assert result.results == ["a", "b", "c"]
    assert order == ["do-a", "do-b", "do-c"]  # no compensations


async def test_saga_compensates_in_reverse_on_failure() -> None:
    order: list[str] = []

    def make(name: str, *, fail: bool = False) -> SagaStep:
        async def action() -> None:
            order.append(f"do-{name}")
            if fail:
                raise RuntimeError(f"{name} failed")

        async def comp() -> None:
            order.append(f"undo-{name}")

        return SagaStep(name=name, action=action, compensate=comp)

    with pytest.raises(SagaError) as ei:
        await SagaCoordinator().run([make("a"), make("b"), make("c", fail=True)])
    assert ei.value.failed_step == "c"
    # a and b completed; they compensate in reverse (b then a). c failed mid-action
    # and is NOT compensated (it never completed).
    assert order == ["do-a", "do-b", "do-c", "undo-b", "undo-a"]
    assert ei.value.compensated == ("b", "a")
    assert ei.value.compensation_failures == ()


async def test_saga_records_compensation_failures_but_continues() -> None:
    order: list[str] = []

    def make(name: str, *, fail_action: bool = False, fail_comp: bool = False) -> SagaStep:
        async def action() -> None:
            order.append(f"do-{name}")
            if fail_action:
                raise RuntimeError(f"{name} action failed")

        async def comp() -> None:
            order.append(f"undo-{name}")
            if fail_comp:
                raise RuntimeError(f"{name} comp failed")

        return SagaStep(name=name, action=action, compensate=comp)

    steps = [make("a"), make("b", fail_comp=True), make("c", fail_action=True)]
    with pytest.raises(SagaError) as ei:
        await SagaCoordinator(compensate_attempts=1).run(steps)
    # b's compensation fails (recorded), a's succeeds — one stuck undo doesn't
    # strand the others.
    assert "b" in ei.value.compensation_failures
    assert "a" in ei.value.compensated
    assert "undo-a" in order


async def test_saga_empty_is_noop() -> None:
    result = await SagaCoordinator().run([])
    assert result.completed_steps == ()
    assert result.results == []
