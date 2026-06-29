"""Tests for app.inference.router.affinity — KV-cache-affinity routing.

Pins the two competing pulls: a warm prefix attracts a request to the worker
that already holds it, but a hot (saturated) worker stops attracting more, and
capacity always overrides affinity (a warm worker that can't fit is never
chosen).
"""

from __future__ import annotations

import pytest

from app.inference.router.affinity import (
    AffinityConfig,
    AffinityRouter,
    ResidencyOracle,
)
from app.inference.router.errors import RouterConfigError
from app.inference.router.request import InferenceRequest
from app.inference.router.worker import WorkerConfig, WorkerPool


def _req(prefix: str | None, *, prompt: int = 100) -> InferenceRequest:
    return InferenceRequest(
        request_id="r", model="m", prompt_tokens=prompt, max_output_tokens=0, prefix_key=prefix
    )


def _pool(*ids: str, **cfg: int) -> WorkerPool:
    pool = WorkerPool("m")
    for wid in ids:
        pool.add_configured_worker(wid, WorkerConfig(**cfg))
    return pool


def _router(pool: WorkerPool, config: AffinityConfig | None = None) -> AffinityRouter:
    return AffinityRouter(ResidencyOracle(pool.get), config)


def test_residency_oracle_membership() -> None:
    pool = _pool("w0", token_capacity=10_000, max_slots=10)
    oracle = ResidencyOracle(pool.get)
    assert oracle.warm_fraction("p", "w0") == 0.0
    pool.get("w0").touch_prefix("p")  # type: ignore[union-attr]
    assert oracle.warm_fraction("p", "w0") == 1.0
    assert oracle.warm_fraction(None, "w0") == 0.0
    assert oracle.warm_fraction("p", "missing") == 0.0


def test_warm_worker_preferred() -> None:
    pool = _pool("cold", "warm", token_capacity=10_000, max_slots=10)
    pool.get("warm").touch_prefix("shared")  # type: ignore[union-attr]
    router = _router(pool)
    chosen = router.select(_req("shared"), pool.schedulable_workers())
    assert chosen is not None and chosen.worker_id == "warm"


def test_no_affinity_falls_back_to_least_loaded() -> None:
    pool = _pool("busy", "idle", token_capacity=1000, max_slots=10)
    pool.get("busy").admit(_req("x", prompt=500))  # type: ignore[union-attr]
    router = _router(pool)
    chosen = router.select(_req(None), pool.schedulable_workers())
    assert chosen is not None and chosen.worker_id == "idle"


def test_capacity_overrides_affinity() -> None:
    # 'warm' holds the prefix but is full; the request must go to 'cold'.
    pool = _pool("warm", "cold", token_capacity=100, max_slots=10)
    warm = pool.get("warm")
    assert warm is not None
    warm.touch_prefix("shared")
    warm.admit(_req("filler", prompt=100))  # warm now full
    router = _router(pool)
    chosen = router.select(_req("shared", prompt=100), pool.schedulable_workers())
    assert chosen is not None and chosen.worker_id == "cold"


def test_hot_warm_worker_yields_to_cold_free_worker() -> None:
    # warm worker is past the load ceiling -> its affinity bonus is suppressed,
    # so a cold-but-free worker wins despite the prefix match.
    pool = _pool("warm", "cold", token_capacity=1000, max_slots=10)
    warm = pool.get("warm")
    assert warm is not None
    warm.touch_prefix("shared")
    warm.admit(_req("f1", prompt=450))
    warm.admit(_req("f2", prompt=450))  # warm at 90% > 0.85 ceiling
    router = _router(pool, AffinityConfig(affinity_weight=1.0, load_ceiling=0.85))
    chosen = router.select(_req("shared", prompt=50), pool.schedulable_workers())
    assert chosen is not None and chosen.worker_id == "cold"


def test_select_returns_none_when_nothing_fits() -> None:
    pool = _pool("w0", token_capacity=100, max_slots=10)
    pool.get("w0").admit(_req("f", prompt=100))  # type: ignore[union-attr]
    router = _router(pool)
    assert router.select(_req("p", prompt=50), pool.schedulable_workers()) is None


def test_rank_orders_best_first() -> None:
    pool = _pool("a", "b", "c", token_capacity=1000, max_slots=10)
    pool.get("b").touch_prefix("shared")  # type: ignore[union-attr]
    router = _router(pool)
    ranked = router.rank(_req("shared"), pool.schedulable_workers())
    assert ranked[0].worker_id == "b"
    assert ranked[0].warm_fraction == 1.0


def test_zero_affinity_weight_is_pure_load_balance() -> None:
    pool = _pool("warm", "idle", token_capacity=1000, max_slots=10)
    warm = pool.get("warm")
    assert warm is not None
    warm.touch_prefix("shared")
    warm.admit(_req("f", prompt=300))  # warm slightly loaded
    router = _router(pool, AffinityConfig(affinity_weight=0.0))
    chosen = router.select(_req("shared", prompt=10), pool.schedulable_workers())
    # Affinity disabled -> least-loaded wins.
    assert chosen is not None and chosen.worker_id == "idle"


def test_affinity_config_validation() -> None:
    with pytest.raises(RouterConfigError):
        AffinityConfig(affinity_weight=-1.0)
    with pytest.raises(RouterConfigError):
        AffinityConfig(load_ceiling=0.0)
    with pytest.raises(RouterConfigError):
        AffinityConfig(load_ceiling=1.5)


def test_custom_oracle_protocol() -> None:
    # Facet B can supply any PrefixCacheOracle; the router uses its warmth signal.
    class FakeOracle:
        def warm_fraction(self, prefix_key: str | None, worker_id: str) -> float:
            return 1.0 if worker_id == "chosen" else 0.0

    pool = _pool("chosen", "other", token_capacity=1000, max_slots=10)
    router = AffinityRouter(FakeOracle())
    chosen = router.select(_req("anything"), pool.schedulable_workers())
    assert chosen is not None and chosen.worker_id == "chosen"
