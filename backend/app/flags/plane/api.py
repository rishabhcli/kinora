"""The runtime-config admin API — read + write surface for the plane.

Mounted under ``/runtime-config`` (distinct from the §13 experimentation
platform's ``/flags``). All routes require an authenticated user; the writes are
rate-limited and audited by the plane itself.

* **Read:** list the flag catalog, resolve one flag (or the whole effective
  config) for a supplied :class:`~app.flags.plane.context.FlagContext`, and read
  the change history.
* **Write:** set/clear a global override, add/remove a targeting rule, set/clear
  a percentage rollout, revert a flag to base, and import a whole override layer
  (hot-reload). Every value is validated against the flag spec and run through
  the kill-switch guard, so a request that would raise ``kinora.live_video`` is
  rejected with a 409.

Handlers are thin: they translate JSON to the plane's typed calls and map the
plane's typed errors to precise HTTP statuses.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.flags.plane.context import FlagContext
from app.flags.plane.errors import (
    FlagTypeError,
    KillSwitchViolation,
    UnknownFlagError,
)
from app.flags.plane.overrides import TargetingRule
from app.flags.plane.plane import RuntimeConfigPlane

logger = get_logger("app.flags.plane.api")

router = APIRouter(prefix="/runtime-config", tags=["runtime-config"])


# --------------------------------------------------------------------------- #
# Request/response bodies
# --------------------------------------------------------------------------- #


class ContextBody(BaseModel):
    """The four Kinora targeting dimensions for a resolution."""

    book: str | None = None
    user: str | None = None
    cohort: str | None = None
    provider: str | None = None

    def to_context(self) -> FlagContext:
        return FlagContext(
            book=self.book, user=self.user, cohort=self.cohort, provider=self.provider
        )


class ResolveRequest(BaseModel):
    context: ContextBody = Field(default_factory=ContextBody)


class OverrideBody(BaseModel):
    value: Any


class RuleBody(BaseModel):
    """A targeting rule for a flag (a subset of dimensions + a value)."""

    id: str
    value: Any
    book: str | None = None
    user: str | None = None
    cohort: str | None = None
    provider: str | None = None
    priority: int = 0
    description: str = ""


class RolloutBody(BaseModel):
    percent: float = Field(ge=0.0, le=100.0)
    bucket_by: str = "user"
    seed: int = 0


class ImportBody(BaseModel):
    overlays: dict[str, Any] = Field(default_factory=dict)
    version: int = 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _plane(container: ContainerDep) -> RuntimeConfigPlane:
    return container.runtime_config_plane


def _on_write_error(exc: Exception) -> APIError:
    """Map the plane's typed write errors to precise HTTP statuses."""
    if isinstance(exc, KillSwitchViolation):
        return APIError(
            "kill_switch_violation",
            str(exc),
            status=409,
            detail={"flag": exc.key, "base": exc.base, "attempted": exc.attempted},
        )
    if isinstance(exc, UnknownFlagError):
        return APIError("flag_not_found", str(exc), status=404, detail={"flag": exc.key})
    if isinstance(exc, FlagTypeError):
        return APIError("flag_type_error", str(exc), status=422, detail={"flag": exc.key})
    return APIError("flag_invalid", str(exc), status=422)


# --------------------------------------------------------------------------- #
# Read surface
# --------------------------------------------------------------------------- #


@router.get("")
async def list_flags(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    """The full flag catalog (specs) + the current override-layer version."""
    plane = _plane(container)
    return {
        **plane.registry.to_dict(),
        "layer_version": plane.export_overrides()["version"],
    }


@router.post("/snapshot")
async def snapshot(
    body: ResolveRequest, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """The effective configuration for a context (every flag's resolved value)."""
    return _plane(container).snapshot(body.context.to_context())


@router.get("/overrides")
async def export_overrides(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    """The current override layer as a round-trippable dict (backup/export)."""
    return _plane(container).export_overrides()


@router.get("/audit")
async def audit(
    container: ContainerDep,
    user: CurrentUser,
    flag_key: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Recent runtime-config change history (newest first)."""
    plane = _plane(container)
    return [r.to_dict() for r in plane.history(flag_key=flag_key, limit=limit)]


@router.get("/{flag_key}")
async def get_flag(
    flag_key: str, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """A flag's spec + its overlay + its base-context resolution (404 if unknown)."""
    plane = _plane(container)
    spec = plane.registry.try_get(flag_key)
    if spec is None:
        raise APIError("flag_not_found", "no such flag", status=404)
    return {
        "spec": spec.to_dict(),
        "overlay": plane.export_overrides()["overlays"].get(flag_key, {}),
        "resolution": plane.get(flag_key).to_dict(),
    }


@router.post("/{flag_key}/resolve")
async def resolve_flag(
    flag_key: str,
    body: ResolveRequest,
    container: ContainerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    """Resolve one flag for a supplied context (404 if the flag is unknown)."""
    plane = _plane(container)
    if flag_key not in plane.registry:
        raise APIError("flag_not_found", "no such flag", status=404)
    return plane.get(flag_key, body.context.to_context()).to_dict()


# --------------------------------------------------------------------------- #
# Write surface (validated + audited + notified by the plane)
# --------------------------------------------------------------------------- #


@router.put("/{flag_key}/override")
async def set_override(
    flag_key: str,
    body: OverrideBody,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Set a global static override (rejected if it would raise a kill-switch)."""
    try:
        resolution = _plane(container).set_override(flag_key, body.value, actor=user.id)
    except (KillSwitchViolation, UnknownFlagError, FlagTypeError) as exc:
        raise _on_write_error(exc) from exc
    logger.info("runtime_config.override", flag=flag_key, actor=user.id)
    return resolution.to_dict()


@router.delete("/{flag_key}/override")
async def clear_override(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, bool]:
    """Remove the global static override for a flag."""
    try:
        _plane(container).clear_override(flag_key, actor=user.id)
    except UnknownFlagError as exc:
        raise _on_write_error(exc) from exc
    return {"cleared": True}


@router.post("/{flag_key}/rules")
async def add_rule(
    flag_key: str,
    body: RuleBody,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Add (or replace by id) a targeting rule for a flag."""
    rule = TargetingRule(
        id=body.id,
        value=body.value,
        book=body.book,
        user=body.user,
        cohort=body.cohort,
        provider=body.provider,
        priority=body.priority,
        description=body.description,
    )
    try:
        resolution = _plane(container).add_rule(flag_key, rule, actor=user.id)
    except (KillSwitchViolation, UnknownFlagError, FlagTypeError) as exc:
        raise _on_write_error(exc) from exc
    return resolution.to_dict()


@router.delete("/{flag_key}/rules/{rule_id}")
async def remove_rule(
    flag_key: str,
    rule_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, bool]:
    """Remove a targeting rule from a flag."""
    try:
        _plane(container).remove_rule(flag_key, rule_id, actor=user.id)
    except UnknownFlagError as exc:
        raise _on_write_error(exc) from exc
    return {"removed": True}


@router.put("/{flag_key}/rollout")
async def set_rollout(
    flag_key: str,
    body: RolloutBody,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Set a sticky percentage rollout for a flag (rejected on a kill-switch)."""
    try:
        resolution = _plane(container).set_rollout(
            flag_key,
            body.percent,
            bucket_by=body.bucket_by,
            seed=body.seed,
            actor=user.id,
        )
    except (KillSwitchViolation, UnknownFlagError) as exc:
        raise _on_write_error(exc) from exc
    except ValueError as exc:
        raise APIError("flag_invalid", str(exc), status=422) from exc
    return resolution.to_dict()


@router.delete("/{flag_key}/rollout")
async def clear_rollout(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, bool]:
    """Remove the percentage rollout for a flag."""
    try:
        _plane(container).clear_rollout(flag_key, actor=user.id)
    except UnknownFlagError as exc:
        raise _on_write_error(exc) from exc
    return {"cleared": True}


@router.delete("/{flag_key}")
async def clear_flag(
    flag_key: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, bool]:
    """Revert a flag fully to its base (drop every overlay)."""
    try:
        _plane(container).clear_flag(flag_key, actor=user.id)
    except UnknownFlagError as exc:
        raise _on_write_error(exc) from exc
    return {"reverted": True}


@router.post("/import")
async def import_overrides(
    body: ImportBody,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> dict[str, Any]:
    """Replace the whole override layer (hot-reload), validating every value."""
    try:
        _plane(container).import_overrides(body.model_dump(), actor=user.id)
    except (KillSwitchViolation, UnknownFlagError, FlagTypeError) as exc:
        raise _on_write_error(exc) from exc
    logger.info("runtime_config.import", actor=user.id, flags=len(body.overlays))
    return _plane(container).export_overrides()


__all__ = ["router"]
