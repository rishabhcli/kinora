"""Tests for app.inference.router.dispatcher — the MultiModelRouter façade."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.inference.router.cancellation import CancellationRegistry
from app.inference.router.dispatcher import MultiModelRouter
from app.inference.router.errors import BackendError
from app.inference.router.protocols import InferenceResult
from app.inference.router.request import InferenceRequest
from app.inference.router.router import InferenceRouter
from app.inference.router.worker import WorkerConfig, WorkerPool


class _Backend:
    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        return [
            InferenceResult(request_id=r.request_id, model=self._model, output_tokens=8)
            for r in requests
        ]


def _router_for(model: str) -> InferenceRouter:
    pool = WorkerPool(model)
    pool.add_configured_worker("w0", WorkerConfig(token_capacity=10_000, max_slots=8))
    return InferenceRouter(model, pool, _Backend(model))


def _req(rid: str, model: str, **kw: object) -> InferenceRequest:
    return InferenceRequest(request_id=rid, model=model, prompt_tokens=100, **kw)  # type: ignore[arg-type]


def _multi() -> MultiModelRouter:
    return MultiModelRouter(
        {"qwen-max": _router_for("qwen-max"), "qwen-vl": _router_for("qwen-vl")}
    )


async def test_routes_by_model() -> None:
    multi = _multi()
    fut = await multi.submit(_req("a", "qwen-vl"))
    await multi.run_until_idle()
    res = await fut
    assert res.ok and res.model == "qwen-vl"


async def test_unknown_model_raises() -> None:
    multi = _multi()
    with pytest.raises(BackendError):
        await multi.submit(_req("a", "no-such-model"))


def test_register_rejects_model_mismatch() -> None:
    multi = MultiModelRouter()
    with pytest.raises(BackendError):
        multi.register("qwen-max", _router_for("qwen-vl"))


def test_register_rejects_duplicate() -> None:
    multi = MultiModelRouter()
    multi.register("qwen-max", _router_for("qwen-max"))
    with pytest.raises(BackendError):
        multi.register("qwen-max", _router_for("qwen-max"))


async def test_tick_fans_out_across_models() -> None:
    multi = _multi()
    await multi.submit(_req("a", "qwen-max"))
    await multi.submit(_req("b", "qwen-vl"))
    assert multi.queue_depth == 2
    dispatched = await multi.tick()
    assert dispatched == 2
    assert multi.queue_depth == 0


async def test_stats_are_per_model() -> None:
    multi = _multi()
    await multi.submit(_req("a", "qwen-max"))
    await multi.run_until_idle()
    stats = multi.stats()
    assert set(stats) == {"qwen-max", "qwen-vl"}
    assert stats["qwen-max"]["succeeded"] == 1
    assert stats["qwen-vl"]["succeeded"] == 0


async def test_cancel_scope_broadcasts_across_models() -> None:
    # Drain workers so requests stay queued, then cancel a cross-model scope.
    multi = MultiModelRouter()
    for model in ("qwen-max", "qwen-vl"):
        r = _router_for(model)
        r._pool.drain_worker("w0")  # noqa: SLF001 - test setup
        multi.register(model, r)
    reg = CancellationRegistry()
    f1 = await multi.submit(_req("a", "qwen-max", metadata={"cancel_token": reg.token("sess")}))
    f2 = await multi.submit(_req("b", "qwen-vl", metadata={"cancel_token": reg.token("sess")}))
    n = await multi.cancel_scope("sess", reason="closed")
    assert n == 2
    for f in (f1, f2):
        assert f.done() and f.exception() is not None


def test_empty_dispatcher_tick_is_zero() -> None:
    import asyncio

    multi = MultiModelRouter()
    assert asyncio.run(multi.tick()) == 0
    assert multi.models == []
