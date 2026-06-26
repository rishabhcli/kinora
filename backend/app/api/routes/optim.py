"""Optimization observability — the cost/perf surface a dev HUD reads.

Named ``optim`` (not ``metrics``) because ``routes/metrics.py`` is already the ``/eval`` surface
and ``observability.metrics`` exposes the Prometheus ``/metrics`` exposition. This adds the *money*
view nothing else provides: per book / session / model / operation USD, from the process-wide
:class:`~app.optim.cost_meter.CostMeter` (populated when the cost-meter usage sink is wired in).

Both endpoints are read-only and require an authenticated user (spend is sensitive). Latency
histograms live in Prometheus ``/metrics``; ``/optim/perf`` is a compact JSON complement for a HUD.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.optim.cost_meter import CostMeter, get_cost_meter

router = APIRouter(prefix="/optim", tags=["optim"])

#: Process start (monotonic) for an uptime read in the perf summary.
_STARTED_AT = time.monotonic()


def build_cost_report(meter: CostMeter) -> dict[str, Any]:
    """The full cost rollup + the meter's priced-model list (pure; for direct testing)."""
    return {"priced_models": meter.priced_models, "rollup": meter.snapshot()}


def build_perf_report(meter: CostMeter, *, uptime_s: float) -> dict[str, Any]:
    """A compact HUD summary: uptime + totals + per-operation spend (pure)."""
    snap = meter.snapshot()
    return {
        "uptime_s": round(uptime_s, 1),
        "priced_model_count": len(meter.priced_models),
        "totals": snap["total"],
        "by_operation": snap["by_operation"],
    }


@router.get("/cost")
async def get_cost(user: CurrentUser) -> dict[str, Any]:
    """Per book / session / model / operation USD rollup (and physical units)."""
    return build_cost_report(get_cost_meter())


@router.get("/perf")
async def get_perf(user: CurrentUser) -> dict[str, Any]:
    """Compact cost/uptime summary for an in-app HUD (full latency in Prometheus /metrics)."""
    return build_perf_report(get_cost_meter(), uptime_s=time.monotonic() - _STARTED_AT)


__all__ = ["build_cost_report", "build_perf_report", "router"]
