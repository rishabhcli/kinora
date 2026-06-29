"""Multi-model dispatcher — one :class:`InferenceRouter` per model behind a façade.

A single :class:`InferenceRouter` serves one model (per-model pools + affinity +
bin-packing is what keeps each tractable, see ``router.py``). Real deployments
serve several models at once — the Kinora crew alone spans ``qwen3.7-max``,
``qwen3.7-plus``, ``qwen3-vl`` (§11). :class:`MultiModelRouter` is the thin
dispatcher over them:

* ``submit`` routes by ``request.model`` to the owning router (raising if no
  router is registered for that model — fail fast, never silently drop);
* ``tick`` / ``run_until_idle`` fan out across every model router concurrently;
* ``cancel`` / ``cancel_scope`` broadcast (a session's work may span models);
* ``stats`` aggregates per-model snapshots for the SLO controller / metrics panel.

It owns no scheduling logic of its own — that all lives in the per-model routers
— so it stays a few-dozen-line coordination shell.
"""

from __future__ import annotations

import asyncio

from .errors import BackendError
from .protocols import InferenceResult
from .request import InferenceRequest
from .router import InferenceRouter


class MultiModelRouter:
    """Dispatches requests to a per-model :class:`InferenceRouter`."""

    def __init__(self, routers: dict[str, InferenceRouter] | None = None) -> None:
        self._routers: dict[str, InferenceRouter] = {}
        for model, router in (routers or {}).items():
            self.register(model, router)

    def register(self, model: str, router: InferenceRouter) -> None:
        """Register the router that owns ``model``."""
        if router.model != model:
            raise BackendError(f"router serves {router.model!r}, registered under {model!r}")
        if model in self._routers:
            raise BackendError(f"duplicate router for model {model!r}")
        self._routers[model] = router

    @property
    def models(self) -> list[str]:
        return list(self._routers)

    def router_for(self, model: str) -> InferenceRouter:
        """The router owning ``model`` (raises :class:`BackendError` if unknown)."""
        router = self._routers.get(model)
        if router is None:
            raise BackendError(f"no router registered for model {model!r}")
        return router

    async def submit(self, request: InferenceRequest) -> asyncio.Future[InferenceResult]:
        """Route ``request`` to its model's router and return the result future."""
        return await self.router_for(request.model).submit(request)

    async def tick(self) -> int:
        """Run one scheduling step on every model router; returns total dispatched."""
        if not self._routers:
            return 0
        counts = await asyncio.gather(*(r.tick() for r in self._routers.values()))
        return sum(counts)

    async def run_until_idle(self, *, max_ticks: int = 100_000) -> int:
        """Drive every router until all queues drain (or ``max_ticks``)."""
        total = 0
        for _ in range(max_ticks):
            n = await self.tick()
            total += n
            if n == 0 and self.queue_depth == 0:
                break
        return total

    async def cancel(self, request_id: str, *, reason: str | None = None) -> bool:
        """Cancel ``request_id`` wherever it lives (broadcasts across models)."""
        results = await asyncio.gather(
            *(r.cancel(request_id, reason=reason) for r in self._routers.values())
        )
        return any(results)

    async def cancel_scope(self, scope: str, *, reason: str | None = None) -> int:
        """Cancel a scope across every model router; returns the total cancelled."""
        counts = await asyncio.gather(
            *(r.cancel_scope(scope, reason=reason) for r in self._routers.values())
        )
        return sum(counts)

    @property
    def queue_depth(self) -> int:
        """Total queued requests across all model routers."""
        return sum(r.queue_depth for r in self._routers.values())

    @property
    def inflight(self) -> int:
        """Total in-flight (dispatched/running) requests across all model routers."""
        return sum(r.inflight for r in self._routers.values())

    def stats(self) -> dict[str, dict[str, object]]:
        """Per-model stats snapshots, keyed by model id."""
        return {model: dict(r.stats.snapshot()) for model, r in self._routers.items()}


__all__ = ["MultiModelRouter"]
