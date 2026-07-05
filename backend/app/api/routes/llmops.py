"""LLM-ops API surface — the prompt registry, guardrails, eval, traces, models.

A read/observe surface over the :mod:`app.llmops` platform (built lazily on the
:class:`~app.composition.Container`). Every route requires an authenticated user
(prompt internals + spend are sensitive) and the whole router is **inert unless
``settings.llmops_enabled``** — disabled, every endpoint returns 404 so wiring it
in changes nothing by default (mirrors ``optim``/``routing`` opt-in posture).

The eval endpoints run the harness with the platform's **deterministic fake
responder + judge**, so they never make a live model call or spend credits — the
numbers are reproducible and free for diagnostics.

Routes (all under ``/api/llmops``):

* ``GET  /prompts`` — every registered prompt key + its active version.
* ``GET  /prompts/{key}`` — every version of a key (semver-ordered) + changelog.
* ``GET  /prompts/{key}/diff?old=&new=`` — structural diff between two versions.
* ``POST /prompts/{key}/register`` — register a candidate version (semver + changelog).
* ``POST /prompts/{key}/rollback`` — roll the active version back.
* ``POST /guardrails/check-input`` / ``check-output`` — run text through the policy.
* ``GET  /models`` — the model registry; ``POST /models/route`` — capability/cost routing.
* ``GET  /datasets`` / ``GET /rubrics`` — the eval fixtures.
* ``POST /eval`` / ``POST /ab`` / ``POST /regression`` — run the offline harness.
* ``GET  /traces`` — query run traces; ``GET /traces/rollup`` — aggregated stats.
* ``GET  /cache/stats`` — response-cache hit/miss accounting.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.composition import Container
from app.core.logging import get_logger
from app.llmops.diff import suggest_bump
from app.llmops.errors import LLMOpsError
from app.llmops.models_registry import Capability, Modality, RoutingRequest
from app.llmops.tracing import TraceQuery

logger = get_logger("app.api.llmops")

router = APIRouter(prefix="/llmops", tags=["llmops"])


def _service(container: Container) -> Any:
    """The wired LLM-ops service, or a 404 when the platform is disabled."""
    if not container.settings.llmops_enabled:
        raise APIError("not_found", "the LLM-ops platform is disabled", status=404)
    return container.llmops


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class RegisterPromptBody(BaseModel):
    system: str = Field(min_length=1)
    bump: str | None = Field(
        default=None, description="major|minor|patch (auto-suggested if omitted)"
    )
    summary: str | None = None


class RollbackBody(BaseModel):
    to: str | None = Field(default=None, description="target version (default: previous)")


class GuardTextBody(BaseModel):
    text: str
    protected_texts: list[str] | None = None


class RouteBody(BaseModel):
    required: list[str] = Field(default_factory=list)
    min_context: int = 0
    modality: str | None = None
    provider: str | None = None
    objective: str = "cost"
    max_cost_per_1k: float | None = None


class EvalBody(BaseModel):
    dataset_name: str
    version: str | None = None
    runs: int = Field(default=3, ge=1, le=10)


class ABBody(BaseModel):
    version_a: str
    version_b: str
    dataset_name: str
    runs: int = Field(default=3, ge=1, le=10)


class RegressionBody(BaseModel):
    candidate_version: str
    dataset_name: str
    baseline_version: str | None = None
    runs: int = Field(default=3, ge=1, le=10)


# --------------------------------------------------------------------------- #
# Prompt registry
# --------------------------------------------------------------------------- #


@router.get("/prompts")
async def list_prompts(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    svc = _service(container)
    out = []
    for key in svc.registry.keys():  # noqa: SIM118 - PromptRegistry.keys() is a method, not a dict
        active = svc.registry.get_active(key)
        out.append(
            {
                "key": key,
                "active_version": active.version,
                "prompt_tag": active.prompt_tag,
                "version_count": len(svc.registry.versions(key)),
            }
        )
    return {"prompts": out}


@router.get("/prompts/{key}")
async def get_prompt(key: str, container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    svc = _service(container)
    try:
        versions = svc.registry.versions(key)
    except LLMOpsError as exc:
        raise APIError("not_found", str(exc), status=404) from exc
    active = svc.registry.get_active(key)
    return {
        "key": key,
        "active_version": active.version,
        "versions": [
            {
                "version": r.version,
                "prompt_tag": r.prompt_tag,
                "status": r.status.value,
                "sha256": r.sha256,
                "created_at": r.created_at.isoformat(),
            }
            for r in versions
        ],
        "changelog": [
            {
                "version": e.version,
                "kind": e.kind.value,
                "summary": e.summary,
                "author": e.author,
                "created_at": e.created_at.isoformat(),
            }
            for e in svc.registry.changelog(key)
        ],
    }


@router.get("/prompts/{key}/diff")
async def diff_prompt(
    key: str,
    old: Annotated[str, Query()],
    new: Annotated[str, Query()],
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    svc = _service(container)
    try:
        d = svc.diff_prompt(key, old=old, new=new)
    except LLMOpsError as exc:
        raise APIError("not_found", str(exc), status=404) from exc
    return {
        "key": key,
        "old": old,
        "new": new,
        "identical": d.identical,
        "summary": d.summary(),
        "suggested_bump": suggest_bump(d),
        "sections": {
            "added": list(d.sections.added),
            "removed": list(d.sections.removed),
            "changed": list(d.sections.changed),
        },
        "tokens": {
            "added": d.tokens.added,
            "removed": d.tokens.removed,
            "jaccard": d.tokens.jaccard,
        },
        "unified": d.unified,
    }


@router.post("/prompts/{key}/register")
async def register_prompt(
    key: str, body: RegisterPromptBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        record = svc.register_prompt(
            key, body.system, bump=body.bump, author=user.email, summary=body.summary
        )
    except LLMOpsError as exc:
        raise APIError("conflict", str(exc), status=409) from exc
    return {"key": key, "version": record.version, "status": record.status.value}


@router.post("/prompts/{key}/rollback")
async def rollback_prompt(
    key: str, body: RollbackBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        record = svc.rollback_prompt(key, to=body.to)
    except LLMOpsError as exc:
        raise APIError("bad_request", str(exc), status=400) from exc
    return {"key": key, "active_version": record.version}


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


@router.post("/guardrails/check-input")
async def check_input(
    body: GuardTextBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    return _service(container).guard_input(body.text)


@router.post("/guardrails/check-output")
async def check_output(
    body: GuardTextBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    return _service(container).guard_output(body.text, protected_texts=body.protected_texts)


# --------------------------------------------------------------------------- #
# Model registry + routing
# --------------------------------------------------------------------------- #


@router.get("/models")
async def list_models(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    svc = _service(container)
    return {
        "models": [
            {
                "id": c.id,
                "provider": c.provider,
                "modality": c.modality.value,
                "capabilities": sorted(cap.value for cap in c.capabilities),
                "context_window": c.context_window,
                "input_per_1k": str(c.input_per_1k),
                "output_per_1k": str(c.output_per_1k),
                "quality": c.quality,
            }
            for c in svc.models.all()
        ]
    }


@router.post("/models/route")
async def route_model(
    body: RouteBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        request = RoutingRequest(
            required=frozenset(Capability(c) for c in body.required),
            min_context=body.min_context,
            modality=Modality(body.modality) if body.modality else None,
            provider=body.provider,
            objective=body.objective,
            max_cost_per_1k=Decimal(str(body.max_cost_per_1k)) if body.max_cost_per_1k else None,
        )
    except (ValueError, KeyError) as exc:
        raise APIError("bad_request", f"invalid routing request: {exc}", status=400) from exc
    try:
        card = svc.models.route(request)
    except LLMOpsError as exc:
        raise APIError("not_found", str(exc), status=404) from exc
    return {
        "model": card.id,
        "provider": card.provider,
        "combined_cost_per_1k": str(card.cost_per_1k_combined()),
    }


# --------------------------------------------------------------------------- #
# Eval / A-B / regression
# --------------------------------------------------------------------------- #


@router.get("/datasets")
async def list_datasets(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    svc = _service(container)
    return {
        "datasets": [
            {
                "name": d.name,
                "rubric": d.rubric_name,
                "cases": len(d),
                "adversarial": d.adversarial_count,
                "description": d.description,
            }
            for d in svc.datasets.values()
        ]
    }


@router.get("/rubrics")
async def list_rubrics(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    svc = _service(container)
    return {
        "rubrics": [
            {
                "name": r.name,
                "threshold": r.threshold,
                "criteria": [
                    {"name": c.name, "weight": c.weight, "required": c.required} for c in r.criteria
                ],
            }
            for r in svc.rubrics().values()
        ]
    }


@router.post("/prompts/{key}/eval")
async def run_eval(
    key: str, body: EvalBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        report = await svc.evaluate(
            prompt_key=key, dataset_name=body.dataset_name, version=body.version, runs=body.runs
        )
    except LLMOpsError as exc:
        raise APIError("bad_request", str(exc), status=400) from exc
    return report.to_dict()


@router.post("/prompts/{key}/ab")
async def run_ab(
    key: str, body: ABBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        result = await svc.ab_test(
            prompt_key=key,
            version_a=body.version_a,
            version_b=body.version_b,
            dataset_name=body.dataset_name,
            runs=body.runs,
        )
    except LLMOpsError as exc:
        raise APIError("bad_request", str(exc), status=400) from exc
    return result.to_dict()


@router.post("/prompts/{key}/regression")
async def run_regression(
    key: str, body: RegressionBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _service(container)
    try:
        verdict, baseline, candidate = await svc.check_regression(
            prompt_key=key,
            candidate_version=body.candidate_version,
            dataset_name=body.dataset_name,
            baseline_version=body.baseline_version,
            runs=body.runs,
        )
    except LLMOpsError as exc:
        raise APIError("bad_request", str(exc), status=400) from exc
    return {
        "verdict": verdict.to_dict(),
        "baseline": baseline.to_dict(),
        "candidate": candidate.to_dict(),
    }


# --------------------------------------------------------------------------- #
# Tracing + cache
# --------------------------------------------------------------------------- #


@router.get("/traces")
async def query_traces(
    container: ContainerDep,
    user: CurrentUser,
    prompt_key: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    book_id: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    svc = _service(container)
    traces = svc.query_traces(
        TraceQuery(
            prompt_key=prompt_key,
            model=model,
            book_id=book_id,
            session_id=session_id,
            limit=limit,
        )
    )
    return {"traces": [t.to_dict() for t in traces]}


@router.get("/traces/rollup")
async def trace_rollup(
    container: ContainerDep,
    user: CurrentUser,
    prompt_key: Annotated[str | None, Query()] = None,
    group: Annotated[str | None, Query(description="prompt_key|model|book_id|session_id")] = None,
) -> dict[str, Any]:
    svc = _service(container)
    return svc.trace_rollup(TraceQuery(prompt_key=prompt_key), group=group)


@router.get("/cache/stats")
async def cache_stats(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    return _service(container).cache.stats().to_dict()


__all__ = ["router"]
