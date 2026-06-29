"""Factory helpers — build a wired router/dispatcher with sensible defaults.

These are the convenience constructors the composition root (and tests) call so
the wiring lives in one place. They default to the network-free
:class:`~app.inference.router.backends.EchoBackend`, so building a router never
makes a live call or spends a credit (``KINORA_LIVE_VIDEO`` stays OFF); a caller
that wants real transport passes a
:class:`~app.inference.router.protocols.InferenceBackend` (e.g. a
:class:`~app.inference.router.backends.ChatProviderBackend` over the existing
provider gateway).
"""

from __future__ import annotations

from collections.abc import Mapping

from .backends import EchoBackend
from .dispatcher import MultiModelRouter
from .protocols import InferenceBackend
from .router import InferenceRouter, RouterConfig
from .worker import Worker, WorkerConfig, WorkerPool


def build_router(
    model: str,
    *,
    n_workers: int = 2,
    worker: WorkerConfig | None = None,
    backend: InferenceBackend | None = None,
    config: RouterConfig | None = None,
) -> InferenceRouter:
    """Build a single-model router with ``n_workers`` identical workers.

    Defaults to an :class:`EchoBackend` (no network). ``worker`` sets each
    worker's capacity; ``config`` overrides the admission/fairshare/affinity
    tunables.
    """
    worker_cfg = worker or WorkerConfig()
    pool = WorkerPool(model, [Worker(f"{model}-w{i}", model, worker_cfg) for i in range(n_workers)])
    return InferenceRouter(
        model,
        pool,
        backend or EchoBackend(model),
        config=config,
    )


def build_multi_model_router(
    models: Mapping[str, int],
    *,
    worker: WorkerConfig | None = None,
    config: RouterConfig | None = None,
) -> MultiModelRouter:
    """Build a :class:`MultiModelRouter` over ``{model: n_workers}``.

    Every model gets its own :class:`InferenceRouter` (Echo backend by default),
    sharing the same worker capacity + router config. The mapping mirrors the
    Kinora model stack (§11): e.g. ``{"qwen3.7-max": 1, "qwen3-vl": 2}``.
    """
    routers = {
        model: build_router(model, n_workers=n, worker=worker, config=config)
        for model, n in models.items()
    }
    return MultiModelRouter(routers)


__all__ = ["build_multi_model_router", "build_router"]
