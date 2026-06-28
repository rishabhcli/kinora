"""Admission control tests — backpressure + per-session fairness (§12.2).

Pure-decision cases need no infra; the :class:`SessionFairness` Redis tally and
the :class:`AdmissionController` run against the in-process fake.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderPriority
from app.queue.admission import (
    AdmissionController,
    AdmissionReason,
    SessionFairness,
    decide_admission,
)
from app.queue.fakeredis import FakeRedisClient
from app.queue.redis_queue import RedisRenderQueue

# --- pure decision ---------------------------------------------------------- #


def test_committed_always_admitted_even_over_threshold() -> None:
    d = decide_admission(priority=RenderPriority.COMMITTED, total_depth=999, backpressure_depth=10)
    assert d.admit and d.reason is AdmissionReason.ADMIT_COMMITTED


def test_keyframe_always_admitted() -> None:
    d = decide_admission(priority=RenderPriority.KEYFRAME, total_depth=999, backpressure_depth=10)
    assert d.admit and d.reason is AdmissionReason.ADMIT_KEYFRAME


def test_speculative_shed_under_backpressure() -> None:
    d = decide_admission(priority=RenderPriority.SPECULATIVE, total_depth=10, backpressure_depth=10)
    assert not d.admit and d.reason is AdmissionReason.SHED_BACKPRESSURE


def test_speculative_admitted_under_threshold() -> None:
    d = decide_admission(priority=RenderPriority.SPECULATIVE, total_depth=3, backpressure_depth=10)
    assert d.admit and d.reason is AdmissionReason.ADMIT_UNDER_LIMITS


def test_speculative_shed_by_session_cap() -> None:
    d = decide_admission(
        priority=RenderPriority.SPECULATIVE,
        total_depth=1,
        backpressure_depth=10,
        session_inflight=6,
        session_cap=6,
    )
    assert not d.admit and d.reason is AdmissionReason.SHED_SESSION_CAP


def test_backpressure_takes_priority_over_session_cap() -> None:
    # When both fire, depth backpressure is reported first (cheapest global guard).
    d = decide_admission(
        priority=RenderPriority.SPECULATIVE,
        total_depth=10,
        backpressure_depth=10,
        session_inflight=99,
        session_cap=6,
    )
    assert d.reason is AdmissionReason.SHED_BACKPRESSURE


def test_decision_truthiness() -> None:
    assert (
        bool(
            decide_admission(priority=RenderPriority.COMMITTED, total_depth=0, backpressure_depth=1)
        )
        is True
    )


# --- session fairness (Redis tally) ----------------------------------------- #


@pytest.fixture
def client() -> FakeRedisClient:
    return FakeRedisClient()


async def test_session_fairness_tracks_inflight(client: FakeRedisClient) -> None:
    fair = SessionFairness(client, namespace="ns", session_cap=2)
    assert await fair.inflight("s1") == 0
    await fair.acquire("s1", "j1")
    await fair.acquire("s1", "j2")
    assert await fair.inflight("s1") == 2
    assert await fair.would_admit("s1") is False  # at cap
    await fair.release("s1", "j1")
    assert await fair.inflight("s1") == 1
    assert await fair.would_admit("s1") is True


async def test_session_fairness_is_per_session(client: FakeRedisClient) -> None:
    fair = SessionFairness(client, namespace="ns", session_cap=1)
    await fair.acquire("s1", "j1")
    # One session at cap does not block another.
    assert await fair.would_admit("s1") is False
    assert await fair.would_admit("s2") is True


async def test_session_fairness_release_idempotent(client: FakeRedisClient) -> None:
    fair = SessionFairness(client, namespace="ns", session_cap=2)
    await fair.acquire("s1", "j1")
    await fair.release("s1", "j1")
    await fair.release("s1", "j1")  # second release is a no-op
    assert await fair.inflight("s1") == 0


# --- controller orchestration ----------------------------------------------- #


@pytest.fixture
def queue(client: FakeRedisClient) -> RedisRenderQueue:
    return RedisRenderQueue(client, namespace="kinora:test:adm", backpressure_depth=3)


async def test_controller_admits_committed_without_redis(queue: RedisRenderQueue) -> None:
    ctrl = AdmissionController(queue)
    d = await ctrl.check(priority=RenderPriority.COMMITTED, session_id="s1")
    assert d.admit and d.reason is AdmissionReason.ADMIT_COMMITTED


async def test_controller_sheds_speculative_at_depth(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    ctrl = AdmissionController(queue)
    # Fill the queue to the backpressure threshold (3) via committed (always admitted).
    for i in range(3):
        await queue.enqueue(
            shot_hash=f"c{i}", priority=RenderPriority.COMMITTED, book_id="b", job_id=f"c{i}"
        )
    d = await ctrl.check(priority=RenderPriority.SPECULATIVE, session_id="s1")
    assert not d.admit and d.reason is AdmissionReason.SHED_BACKPRESSURE


async def test_controller_sheds_speculative_by_session_cap(
    queue: RedisRenderQueue, client: FakeRedisClient
) -> None:
    fair = SessionFairness(client, namespace="kinora:test:adm", session_cap=1)
    ctrl = AdmissionController(queue, fairness=fair)
    await fair.acquire("s1", "j1")  # s1 already holds its single slot
    d = await ctrl.check(priority=RenderPriority.SPECULATIVE, session_id="s1")
    assert not d.admit and d.reason is AdmissionReason.SHED_SESSION_CAP
    # A different session under depth + its own cap is still admitted.
    d2 = await ctrl.check(priority=RenderPriority.SPECULATIVE, session_id="s2")
    assert d2.admit


async def test_controller_default_backpressure_matches_queue(
    queue: RedisRenderQueue,
) -> None:
    ctrl = AdmissionController(queue)
    assert ctrl._backpressure_depth == queue._backpressure_depth == 3
