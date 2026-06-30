"""The SLO / deep-health API surface (additive router under ``/api/slo``).

Read-only operational endpoints over the process-wide SLO engine + health
registry (:mod:`app.slo.service`). Distinct from the round-1 root ``/health`` /
``/ready`` (never touched): this is the *deep* plane — per-dependency health with
criticality, the live SLI/error-budget status, multi-window burn alerts, and the
release-gate signal the flag/canary systems consult.

* ``GET /slo/live``    — liveness (process flag only; never touches a dep).
* ``GET /slo/health``  — deep readiness; 503 when a critical dep is not ready.
* ``GET /slo/status``  — the full SLO snapshot (SLIs, budgets, alerts, gate).
* ``GET /slo/budgets`` — error-budget accounting only.
* ``GET /slo/alerts``  — the firing/idle multi-window burn alerts.
* ``GET /slo/gate``    — the release-gate decision (can we ship / ramp canary?).
* ``GET /slo/report``  — a plain-text status + health report (CLI / on-call).

Liveness/health are unauthenticated (probes are LB/k8s targets); the SLO views
require an authenticated user (operational data is sensitive), mirroring
``optim``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.deps import ContainerDep, CurrentUser
from app.slo.health import HealthRegistry
from app.slo.report import render_health_text, render_status_text
from app.slo.service import build_health_registry, get_health_registry, get_slo_engine

router = APIRouter(prefix="/slo", tags=["slo"])


def _registry_for(container: Any) -> HealthRegistry:
    """The health registry to probe: the pre-wired process one if it has probes,
    else lazily build the real dependency probes from the container."""
    reg = get_health_registry()
    if reg.probes:
        return reg
    return build_health_registry(container)


@router.get("/live", summary="Liveness probe (process flag only)")
async def live() -> dict[str, object]:
    """Pure liveness — answers whether the process itself is alive/draining.

    Never touches a dependency; a failure here means *restart me*, not *route
    around me*.
    """
    return get_health_registry().liveness()


@router.get("/health", summary="Deep readiness (per-dependency, criticality-aware)")
async def health(container: ContainerDep) -> Response:
    """Run every dependency probe in parallel and aggregate.

    503 when any *critical* dependency is not up (the readiness gate); a degraded
    *optional* dependency yields 200 with ``status: degraded`` (still ready).
    """
    report = await _registry_for(container).readiness()
    body = report.to_dict()
    status_code = 200 if report.ready else 503
    return JSONResponse(body, status_code=status_code)


@router.get("/status", summary="Full SLO snapshot (SLIs, budgets, alerts, gate)")
async def status(user: CurrentUser) -> dict[str, object]:
    """The complete SLO plane: every SLI over the eval window, error-budget
    accounting over each objective's window, burn alerts, and the release gate."""
    return get_slo_engine().status().to_dict()


@router.get("/budgets", summary="Error-budget accounting")
async def budgets(user: CurrentUser) -> dict[str, object]:
    """Per-objective error budget: consumed / remaining / exhausted / met."""
    snap = get_slo_engine().status()
    return {"at": snap.at, "error_budgets": [b.to_dict() for b in snap.budgets]}


@router.get("/alerts", summary="Multi-window burn-rate alerts")
async def alerts(user: CurrentUser) -> dict[str, object]:
    """The fast/slow burn-rate alert per objective (firing or idle)."""
    snap = get_slo_engine().status()
    return {
        "at": snap.at,
        "any_firing": bool(snap.firing_alerts),
        "burn_alerts": [a.to_dict() for a in snap.alerts],
    }


@router.get("/gate", summary="Release gate (is there budget to ship / ramp a canary?)")
async def gate(user: CurrentUser) -> dict[str, object]:
    """The release-gate signal the flag/experiment/canary systems consult."""
    return get_slo_engine().release_gate().to_dict()


@router.get("/report", summary="Plain-text SLO + health report")
async def report(container: ContainerDep, user: CurrentUser) -> Response:
    """A compact text report (SLO status + per-dependency health) for on-call."""
    snap = get_slo_engine().status()
    health_report = await _registry_for(container).readiness()
    text = render_status_text(snap) + "\n\n" + render_health_text(health_report)
    return PlainTextResponse(text)


__all__ = ["router"]
