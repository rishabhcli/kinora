"""Feature-flags & experimentation API — admin CRUD + evaluation surface.

Two audiences:

* **Admin** (authenticated): author flags/experiments, flip kill switches,
  archive, and read the audit trail. Writes go through
  :class:`~app.flags.service.FlagService` (versioned, audited, cache-invalidated).
* **Evaluation** (authenticated): resolve a flag (or all flags) for a supplied
  context, and assign an experiment arm (logging a durable exposure). This is
  the surface the desktop renderer / SDK calls to gate UI and enroll in studies.

Everything is mounted under ``/flags`` by the gateway. The pure evaluator does
the actual work; these handlers are thin translation between JSON and the
:mod:`app.flags.serialization` model types, so a malformed flag payload is
rejected at the boundary (422 via :class:`~app.flags.errors.FlagValidationError`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.flags.context import EvalContext
from app.flags.errors import FlagError, FlagNotFoundError, FlagValidationError
from app.flags.serialization import (
    experiment_from_dict,
    experiment_to_dict,
    flag_from_dict,
    flag_to_dict,
)

logger = get_logger("app.api.flags")

router = APIRouter(prefix="/flags", tags=["flags"])


# --------------------------------------------------------------------------- #
# Request/response bodies
# --------------------------------------------------------------------------- #


class EvalContextBody(BaseModel):
    """The identity + attributes a flag is evaluated against."""

    key: str
    kind: str = "user"
    attributes: dict[str, Any] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)
    anonymous: bool = False

    def to_context(self) -> EvalContext:
        return EvalContext(
            key=self.key,
            kind=self.kind,
            attributes=self.attributes,
            units=self.units,
            anonymous=self.anonymous,
        )


class EvaluateRequest(BaseModel):
    """Evaluate one flag for a context."""

    context: EvalContextBody
    default: Any = None


class EvaluateAllRequest(BaseModel):
    """Evaluate every active flag for a context (the SDK bootstrap)."""

    context: EvalContextBody


class AssignRequest(BaseModel):
    """Assign an experiment arm for a context (logs a durable exposure)."""

    context: EvalContextBody


class SetEnabledRequest(BaseModel):
    enabled: bool


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bad_request(exc: FlagError) -> APIError:
    return APIError("flag_invalid", str(exc), status=422)


# --------------------------------------------------------------------------- #
# Evaluation surface
# --------------------------------------------------------------------------- #


@router.post("/{flag_key}/evaluate")
async def evaluate_flag(
    flag_key: str,
    body: EvaluateRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Resolve ``flag_key`` for the supplied context (never errors on a bad flag)."""
    evaluation = await container.flag_service.evaluate(
        flag_key, body.context.to_context(), default=body.default
    )
    return evaluation.to_dict()


@router.post("/evaluate-all")
async def evaluate_all(
    body: EvaluateAllRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Resolve every active flag for a context — the SDK's single bootstrap call."""
    results = await container.flag_service.evaluate_all(body.context.to_context())
    return {key: ev.to_dict() for key, ev in results.items()}


@router.post("/experiments/{experiment_key}/assign")
async def assign_experiment(
    experiment_key: str,
    body: AssignRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Assign an experiment arm and durably (idempotently) log the exposure."""
    assignment = await container.flag_service.assign(
        experiment_key, body.context.to_context()
    )
    if assignment is None:
        raise APIError("experiment_not_found", "no such experiment", status=404)
    return {
        "experiment_key": assignment.experiment_key,
        "variant_key": assignment.variant_key,
        "in_experiment": assignment.in_experiment,
        "reason": assignment.reason,
        "experiment_version": assignment.experiment_version,
    }


# --------------------------------------------------------------------------- #
# Admin: flags
# --------------------------------------------------------------------------- #


@router.get("")
async def list_flags(
    container: ContainerDep,
    user: CurrentUser,
    include_archived: Annotated[bool, Query()] = False,
) -> list[dict[str, Any]]:
    """List every flag definition (optionally including archived)."""
    flags = await container.flag_service.list_flags(include_archived=include_archived)
    return [flag_to_dict(f) for f in flags]


@router.get("/{flag_key}")
async def get_flag(
    flag_key: str, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Fetch one flag definition (404 if absent)."""
    flag = await container.flag_service.get_flag(flag_key)
    if flag is None:
        raise APIError("flag_not_found", "no such flag", status=404)
    return flag_to_dict(flag)


@router.put("/{flag_key}")
async def upsert_flag(
    flag_key: str,
    body: dict[str, Any],
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Create or update a flag (the body is the serialized flag definition)."""
    body = {**body, "key": flag_key}
    try:
        flag = flag_from_dict(body)
    except FlagValidationError as exc:
        raise _bad_request(exc) from exc
    saved = await container.flag_service.upsert_flag(flag, actor=user.id)
    logger.info("flags.upsert", flag_key=flag_key, version=saved.version, actor=user.id)
    return flag_to_dict(saved)


@router.post("/{flag_key}/enabled")
async def set_flag_enabled(
    flag_key: str,
    body: SetEnabledRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Flip a flag's kill switch (a fast, audited toggle)."""
    try:
        saved = await container.flag_service.set_enabled(
            flag_key, body.enabled, actor=user.id
        )
    except FlagNotFoundError as exc:
        raise APIError("flag_not_found", "no such flag", status=404) from exc
    logger.info("flags.toggle", flag_key=flag_key, enabled=body.enabled, actor=user.id)
    return flag_to_dict(saved)


@router.post("/{flag_key}/archive")
async def archive_flag(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Archive a flag (soft-delete; keeps history)."""
    try:
        saved = await container.flag_service.archive_flag(flag_key, actor=user.id)
    except FlagNotFoundError as exc:
        raise APIError("flag_not_found", "no such flag", status=404) from exc
    return flag_to_dict(saved)


@router.delete("/{flag_key}")
async def delete_flag(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, bool]:
    """Hard-delete a flag (the DELETE is itself audited)."""
    existed = await container.flag_service.delete_flag(flag_key, actor=user.id)
    if not existed:
        raise APIError("flag_not_found", "no such flag", status=404)
    return {"deleted": True}


@router.get("/{flag_key}/audit")
async def flag_audit(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """The change history for one flag (newest first)."""
    return await container.flag_service.audit_log(subject_key=flag_key, limit=limit)


# --------------------------------------------------------------------------- #
# Admin: experiments
# --------------------------------------------------------------------------- #


@router.get("/experiments/all")
async def list_experiments(
    container: ContainerDep, user: CurrentUser
) -> list[dict[str, Any]]:
    """List every experiment definition."""
    experiments = await container.flag_service.list_experiments()
    return [experiment_to_dict(e) for e in experiments]


@router.get("/experiments/{experiment_key}")
async def get_experiment(
    experiment_key: str, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Fetch one experiment definition (404 if absent)."""
    exp = await container.flag_service.get_experiment(experiment_key)
    if exp is None:
        raise APIError("experiment_not_found", "no such experiment", status=404)
    return experiment_to_dict(exp)


@router.put("/experiments/{experiment_key}")
async def upsert_experiment(
    experiment_key: str,
    body: dict[str, Any],
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Create or update an experiment (the body is the serialized definition)."""
    body = {**body, "key": experiment_key}
    try:
        exp = experiment_from_dict(body)
    except FlagValidationError as exc:
        raise _bad_request(exc) from exc
    saved = await container.flag_service.upsert_experiment(exp, actor=user.id)
    logger.info(
        "flags.experiment_upsert",
        experiment_key=experiment_key,
        version=saved.version,
        actor=user.id,
    )
    return experiment_to_dict(saved)


@router.get("/experiments/{experiment_key}/exposures")
async def experiment_exposures(
    experiment_key: str, container: ContainerDep, user: CurrentUser
) -> dict[str, int]:
    """Distinct-unit exposure counts per arm for an experiment."""
    return await container.flag_service.exposure_counts(experiment_key)


class DecideRequest(BaseModel):
    """Per-arm metric observations for an experiment decision.

    ``observations`` maps ``variant_key -> metric_key -> {successes, trials}``.
    """

    observations: dict[str, dict[str, dict[str, int]]]
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)


@router.post("/experiments/{experiment_key}/decide")
async def decide_experiment(
    experiment_key: str,
    body: DecideRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Compute the always-valid ship/hold/rollback decision report (§13)."""
    report = await container.flag_service.decide_experiment(
        experiment_key, body.observations, alpha=body.alpha
    )
    if report is None:
        raise APIError("experiment_not_found", "no such experiment", status=404)
    return report


__all__ = ["router"]
