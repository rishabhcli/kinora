"""Integration tests for app.inference.router.router — the InferenceRouter.

These drive the full submit→tick→complete loop against deterministic fakes
(an injected clock + a recording backend), asserting the composed behaviour:
end-to-end success, admission rejection, SLA expiry, priority ordering through
the live router, coalescing fan-out, KV-affinity stickiness, backend-failure
handling, and capacity-bounded dispatch.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.inference.router.admission import AdmissionConfig
from app.inference.router.cancellation import (
    CancellationRegistry,
    CancellationToken,
    CancelledError,
)
from app.inference.router.errors import AdmissionRejected, BackendError, QueueTimeSLAExpired
from app.inference.router.protocols import InferenceResult
from app.inference.router.request import InferenceRequest, RequestPriority, RequestState
from app.inference.router.router import InferenceRouter, RouterConfig
from app.inference.router.worker import WorkerConfig, WorkerPool


class RecordingBackend:
    """Fake backend: records every batch it was handed; echoes token counts."""

    def __init__(self, model: str = "m", *, fail_ids: frozenset[str] = frozenset()) -> None:
        self._model = model
        self._fail_ids = fail_ids
        self.batches: list[list[str]] = []
        self.raise_on_batch = False

    @property
    def model(self) -> str:
        return self._model

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        self.batches.append([r.request_id for r in requests])
        if self.raise_on_batch:
            raise RuntimeError("whole-batch fault")
        out: list[InferenceResult] = []
        for r in requests:
            if r.request_id in self._fail_ids:
                out.append(
                    InferenceResult(
                        request_id=r.request_id, model=self._model, output_tokens=0, error="boom"
                    )
                )
            else:
                out.append(
                    InferenceResult(
                        request_id=r.request_id,
                        model=self._model,
                        output_tokens=r.max_output_tokens or 8,
                        prompt_tokens=r.prompt_tokens,
                    )
                )
        return out


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(n: int = 1, **cfg: int) -> WorkerPool:
    pool = WorkerPool("m")
    for i in range(n):
        pool.add_configured_worker(f"w{i}", WorkerConfig(**cfg))
    return pool


def _req(rid: str, **kw: object) -> InferenceRequest:
    base: dict[str, object] = {
        "request_id": rid,
        "model": "m",
        "prompt_tokens": 100,
        "max_output_tokens": 0,
    }
    base.update(kw)
    return InferenceRequest(**base)  # type: ignore[arg-type]


def _router(
    pool: WorkerPool | None = None, backend: RecordingBackend | None = None, **kw: object
) -> tuple[InferenceRouter, RecordingBackend, Clock]:
    pool = pool or _pool(1, token_capacity=10_000, max_slots=8)
    backend = backend or RecordingBackend()
    clock = Clock()
    config = kw.pop("config", None) or RouterConfig()
    router = InferenceRouter("m", pool, backend, config=config, clock=clock, **kw)  # type: ignore[arg-type]
    return router, backend, clock


async def test_single_request_succeeds_end_to_end() -> None:
    router, backend, _ = _router()
    fut = await router.submit(_req("a", max_output_tokens=16))
    await router.tick()
    result = await fut
    assert result.ok
    assert result.request_id == "a"
    assert result.output_tokens == 16
    assert backend.batches == [["a"]]
    assert router.state_of("a") in (None, RequestState.SUCCEEDED)
    assert router.stats.succeeded == 1


async def test_admission_rejection_raises_synchronously() -> None:
    router, _, _ = _router(config=RouterConfig(admission=AdmissionConfig(max_queue_depth=1)))
    await router.submit(_req("a"))  # fills the queue
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("b"))
    assert exc.value.request_id == "b"
    assert router.stats.rejected == 1


async def test_unservable_request_rejected() -> None:
    router, _, _ = _router(pool=_pool(1, token_capacity=500, max_slots=8))
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("huge", prompt_tokens=1000))
    assert exc.value.reason == "unservable"
    assert exc.value.retry_after_s is None


async def test_queue_time_sla_expiry() -> None:
    # No workers -> nothing dispatches; the request ages out at the next tick.
    pool = WorkerPool("m")  # empty pool
    pool.add_configured_worker("w0", WorkerConfig(token_capacity=10_000, max_slots=8))
    pool.drain_worker("w0")  # make it non-schedulable so nothing dispatches
    router, _, clock = _router(pool=pool, config=RouterConfig(default_queue_sla_s=1.0))
    fut = await router.submit(_req("late"))
    clock.t = 2.0  # past the 1.0s SLA
    await router.tick()
    assert router.stats.expired == 1
    with pytest.raises(QueueTimeSLAExpired):
        await fut


async def test_priority_ordering_through_router() -> None:
    # One slot at a time so order is observable across ticks.
    router, backend, _ = _router(pool=_pool(1, token_capacity=10_000, max_slots=1))
    await router.submit(_req("bulk", priority=RequestPriority.BULK))
    await router.submit(_req("commit", priority=RequestPriority.COMMITTED))
    await router.submit(_req("inter", priority=RequestPriority.INTERACTIVE))
    await router.run_until_idle()
    dispatched_order = [b[0] for b in backend.batches]
    assert dispatched_order == ["inter", "commit", "bulk"]


async def test_coalescing_serves_followers_off_one_leader() -> None:
    router, backend, _ = _router()
    f1 = await router.submit(_req("a", coalesce_key="shared"))
    f2 = await router.submit(_req("b", coalesce_key="shared"))
    f3 = await router.submit(_req("c", coalesce_key="shared"))
    await router.run_until_idle()
    r1, r2, r3 = await f1, await f2, await f3
    # Only the leader actually ran on the backend.
    assert backend.batches == [["a"]]
    assert {r1.request_id, r2.request_id, r3.request_id} == {"a", "b", "c"}
    assert r2.cache_hit and r3.cache_hit
    assert router.stats.coalesced == 2


async def test_affinity_routes_same_prefix_to_same_worker() -> None:
    router, backend, _ = _router(pool=_pool(2, token_capacity=10_000, max_slots=4))
    # Warm w0 with the prefix via a first request, let it complete.
    await router.submit(_req("warm", prefix_key="P"))
    await router.run_until_idle()
    # Subsequent same-prefix requests should stick to the worker that holds P.
    await router.submit(_req("p1", prefix_key="P"))
    await router.submit(_req("p2", prefix_key="P"))
    await router.run_until_idle()
    # Exactly one worker is warm for P (same-prefix requests never split it).
    pool_workers = router._pool.workers()  # noqa: SLF001 - test introspection
    warm_workers = [w for w in pool_workers if w.has_prefix("P")]
    assert len(warm_workers) == 1


async def test_per_request_backend_failure_rejects_future() -> None:
    router, _, _ = _router(backend=RecordingBackend(fail_ids=frozenset({"bad"})))
    good = await router.submit(_req("good"))
    bad = await router.submit(_req("bad"))
    await router.run_until_idle()
    assert (await good).ok
    with pytest.raises(BackendError):
        await bad
    assert router.stats.failed == 1


async def test_whole_batch_fault_fails_every_request_and_releases_capacity() -> None:
    backend = RecordingBackend()
    backend.raise_on_batch = True
    pool = _pool(1, token_capacity=1000, max_slots=4)
    router, _, _ = _router(pool=pool, backend=backend)
    f = await router.submit(_req("a", prompt_tokens=200))
    await router.tick()
    with pytest.raises(BackendError):
        await f
    # Capacity must be released so the worker isn't permanently leaked.
    assert pool.get("w0").tokens_in_use == 0  # type: ignore[union-attr]
    assert router.stats.failed == 1


async def test_capacity_bounds_batch_then_drains_over_ticks() -> None:
    # 3 requests of 400 tokens, worker holds 1000 -> 2 fit per tick.
    router, backend, _ = _router(pool=_pool(1, token_capacity=1000, max_slots=8))
    futs = [await router.submit(_req(f"r{i}", prompt_tokens=400)) for i in range(3)]
    total = await router.run_until_idle()
    assert total == 3
    for f in futs:
        assert (await f).ok
    # First tick took 2 (800 <= 1000), the third waited for capacity.
    assert backend.batches[0] == ["r0", "r1"]
    assert backend.batches[1] == ["r2"]


async def test_tenant_concurrency_cap_rejects_excess() -> None:
    cfg = RouterConfig(admission=AdmissionConfig(max_tenant_inflight=2))
    router, _, _ = _router(pool=_pool(1, token_capacity=10_000, max_slots=8), config=cfg)
    await router.submit(_req("a", tenant="hog"))
    await router.submit(_req("b", tenant="hog"))
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("c", tenant="hog"))
    assert exc.value.reason == "tenant_concurrency"


async def test_transition_hook_observes_lifecycle() -> None:
    seen: list[tuple[str, RequestState]] = []
    router, _, _ = _router(on_transition=lambda rid, st: seen.append((rid, st)))
    fut = await router.submit(_req("a"))
    await router.tick()
    await fut
    states = [st for rid, st in seen if rid == "a"]
    assert RequestState.QUEUED in states
    assert RequestState.DISPATCHED in states
    assert RequestState.SUCCEEDED in states


async def test_model_mismatch_rejected() -> None:
    router, _, _ = _router()
    with pytest.raises(BackendError):
        await router.submit(InferenceRequest(request_id="x", model="other"))


async def test_run_until_idle_is_noop_when_empty() -> None:
    router, _, _ = _router()
    assert await router.run_until_idle() == 0
    assert router.queue_depth == 0


async def test_cancel_queued_request_drops_and_rejects() -> None:
    # Non-schedulable pool so the request stays queued; cancel it before dispatch.
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")
    router, _, _ = _router(pool=pool)
    fut = await router.submit(_req("a"))
    assert router.queue_depth == 1
    cancelled = await router.cancel("a", reason="seeked away")
    assert cancelled
    assert router.queue_depth == 0
    assert router.stats.cancelled == 1
    with pytest.raises(CancelledError) as exc:
        await fut
    assert exc.value.reason == "seeked away"


async def test_cancel_unknown_request_is_false() -> None:
    router, _, _ = _router()
    assert await router.cancel("nope") is False


async def test_cancel_running_request_discards_result() -> None:
    # A token attached via metadata; tripped while "running" -> result discarded.
    token = CancellationToken(scope="sess-1")
    router, _, _ = _router()
    req = _req("a", metadata={"cancel_token": token})
    fut = await router.submit(req)
    # Trip the token, then run the tick: backend returns a result but the router
    # settles the request as cancelled because the token is tripped.
    token.cancel("reader left")
    await router.tick()
    assert router.stats.cancelled == 1
    with pytest.raises(CancelledError):
        await fut


async def test_cancel_scope_cancels_all_tagged_queued_requests() -> None:
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")  # keep everything queued
    router, _, _ = _router(pool=pool)
    reg = CancellationRegistry()
    futs = []
    for i in range(3):
        tok = reg.token("session-X")
        futs.append(await router.submit(_req(f"r{i}", metadata={"cancel_token": tok})))
    # An untagged request in a different (default) scope must survive.
    other = await router.submit(_req("other"))
    n = await router.cancel_scope("session-X", reason="closed book")
    assert n == 3
    for f in futs:
        with pytest.raises(CancelledError):
            await f
    assert router.state_of("other") == RequestState.QUEUED
    assert not other.done()


async def test_committed_preempts_speculative_under_backpressure() -> None:
    # Queue cap of 1, filled by a speculative request; a committed request then
    # preempts it (§12.2) instead of being rejected.
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")  # nothing dispatches; the queue stays full
    cfg = RouterConfig(admission=AdmissionConfig(max_queue_depth=1))
    router, _, _ = _router(pool=pool, config=cfg)
    spec = await router.submit(_req("spec", priority=RequestPriority.SPECULATIVE))
    # The committed request would be QUEUE_FULL, but it preempts the speculative.
    commit = await router.submit(_req("commit", priority=RequestPriority.COMMITTED))
    assert router.state_of("commit") == RequestState.QUEUED
    assert router.stats.preempted == 1
    with pytest.raises(AdmissionRejected) as exc:
        await spec
    assert exc.value.reason == "preempted"
    assert not commit.done()


async def test_speculative_never_preempts() -> None:
    # A speculative request facing a full queue is rejected, not granted a victim.
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")
    cfg = RouterConfig(admission=AdmissionConfig(max_queue_depth=1))
    router, _, _ = _router(pool=pool, config=cfg)
    await router.submit(_req("bulk", priority=RequestPriority.BULK))
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("spec2", priority=RequestPriority.SPECULATIVE))
    assert exc.value.reason == "queue_full"
    assert router.stats.preempted == 0


async def test_preemption_disabled_falls_back_to_rejection() -> None:
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")
    cfg = RouterConfig(admission=AdmissionConfig(max_queue_depth=1), preemption_enabled=False)
    router, _, _ = _router(pool=pool, config=cfg)
    await router.submit(_req("spec", priority=RequestPriority.SPECULATIVE))
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("commit", priority=RequestPriority.COMMITTED))
    assert exc.value.reason == "queue_full"
    assert router.stats.preempted == 0


async def test_committed_not_preemptible_by_committed() -> None:
    # When the queue holds only committed work, a new committed request can't
    # preempt it (only strictly-lower-priority victims are evictable) -> rejected.
    pool = _pool(1, token_capacity=10_000, max_slots=8)
    pool.drain_worker("w0")
    cfg = RouterConfig(admission=AdmissionConfig(max_queue_depth=1))
    router, _, _ = _router(pool=pool, config=cfg)
    await router.submit(_req("c1", priority=RequestPriority.COMMITTED))
    with pytest.raises(AdmissionRejected) as exc:
        await router.submit(_req("c2", priority=RequestPriority.COMMITTED))
    assert exc.value.reason == "queue_full"
