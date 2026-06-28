"""Worker-pool autoscaler tests (app.queue.autoscale, kinora.md §4.9/§12.2).

Pure controller logic plus a live-queue observation pass against the fake. A
fake clock drives the asymmetric scale-down cooldown deterministically.
"""

from __future__ import annotations

import pytest

from app.db.models.enums import RenderPriority
from app.queue.autoscale import LaneAutoscaler, LanePolicy, default_policies
from app.queue.fakeredis import FakeRedisClient
from app.queue.redis_queue import RedisRenderQueue

# --- LanePolicy ------------------------------------------------------------- #


def test_policy_scales_with_backlog() -> None:
    p = LanePolicy(min_workers=1, max_workers=8, jobs_per_worker=2)
    assert p.desired_for(depth=0, inflight=0) == 1  # min when idle
    assert p.desired_for(depth=4, inflight=0) == 2  # ceil(4/2)
    assert p.desired_for(depth=5, inflight=0) == 3  # ceil(5/2)
    assert p.desired_for(depth=100, inflight=0) == 8  # clamped to max


def test_policy_counts_inflight_in_backlog() -> None:
    p = LanePolicy(min_workers=1, max_workers=8, jobs_per_worker=1)
    assert p.desired_for(depth=2, inflight=3) == 5


def test_policy_inelastic_pins_to_min() -> None:
    p = LanePolicy(min_workers=2, max_workers=2, jobs_per_worker=4, elastic=False)
    assert p.desired_for(depth=100, inflight=0) == 2


def test_policy_invalid_bounds_raise() -> None:
    with pytest.raises(ValueError):
        LanePolicy(min_workers=5, max_workers=2)
    with pytest.raises(ValueError):
        LanePolicy(min_workers=0, max_workers=4, jobs_per_worker=0)


def test_default_policies_match_section_4_9() -> None:
    pols = default_policies()
    assert pols[RenderPriority.COMMITTED].min_workers == 4
    assert pols[RenderPriority.SPECULATIVE].min_workers == 2
    assert pols[RenderPriority.KEYFRAME].elastic is False


# --- LaneAutoscaler (stateful, anti-flap) ----------------------------------- #


def test_scale_up_is_immediate() -> None:
    t = {"now": 0.0}
    auto = LaneAutoscaler(
        policies={RenderPriority.COMMITTED: LanePolicy(1, 8, jobs_per_worker=1)},
        cooldown_s=30.0,
        clock=lambda: t["now"],
    )
    plan = auto.plan({RenderPriority.COMMITTED: (5, 0)})
    assert plan.desired[RenderPriority.COMMITTED] == 5
    assert plan.deltas[RenderPriority.COMMITTED] == 4
    assert plan.changed


def test_scale_down_waits_for_cooldown() -> None:
    t = {"now": 0.0}
    auto = LaneAutoscaler(
        policies={RenderPriority.COMMITTED: LanePolicy(1, 8, jobs_per_worker=1)},
        cooldown_s=30.0,
        clock=lambda: t["now"],
    )
    auto.plan({RenderPriority.COMMITTED: (5, 0)})  # scale up to 5
    # Backlog drains; a scale-down within cooldown is held.
    held = auto.plan({RenderPriority.COMMITTED: (1, 0)})
    assert held.desired[RenderPriority.COMMITTED] == 5  # held at 5
    # After the cooldown the scale-down lands.
    t["now"] = 31.0
    dropped = auto.plan({RenderPriority.COMMITTED: (1, 0)})
    assert dropped.desired[RenderPriority.COMMITTED] == 1


def test_no_change_yields_zero_deltas() -> None:
    t = {"now": 0.0}
    auto = LaneAutoscaler(
        policies={RenderPriority.COMMITTED: LanePolicy(2, 8, jobs_per_worker=1)},
        clock=lambda: t["now"],
    )
    auto.plan({RenderPriority.COMMITTED: (2, 0)})  # desired==min==current
    plan = auto.plan({RenderPriority.COMMITTED: (2, 0)})
    assert not plan.changed and plan.deltas[RenderPriority.COMMITTED] == 0


def test_plan_total_desired() -> None:
    t = {"now": 0.0}
    auto = LaneAutoscaler(policies=default_policies(), cooldown_s=0.0, clock=lambda: t["now"])
    plan = auto.plan(
        {
            RenderPriority.COMMITTED: (8, 0),
            RenderPriority.SPECULATIVE: (4, 0),
            RenderPriority.KEYFRAME: (10, 0),  # inelastic -> stays at 2
        }
    )
    assert plan.desired[RenderPriority.KEYFRAME] == 2
    assert plan.total_desired == plan.desired[RenderPriority.COMMITTED] + 2 + 2


# --- live observation ------------------------------------------------------- #


async def test_plan_from_queue_observes_live_depth() -> None:
    client = FakeRedisClient()
    queue = RedisRenderQueue(client, namespace="kinora:test:auto")
    for i in range(5):
        await queue.enqueue(
            shot_hash=f"c{i}", priority=RenderPriority.COMMITTED, book_id="b", job_id=f"c{i}"
        )
    auto = LaneAutoscaler(
        policies={RenderPriority.COMMITTED: LanePolicy(1, 8, jobs_per_worker=1)},
        cooldown_s=0.0,
    )
    plan = await auto.plan_from_queue(queue)
    assert plan.desired[RenderPriority.COMMITTED] == 5  # 5 queued, 1 job/worker
