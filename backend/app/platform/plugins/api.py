"""Plugin-platform REST API — marketplace + lifecycle + dispatch surface.

Mounted under ``/plugins`` by the gateway. The router is thin: it translates
JSON to/from the :class:`~app.platform.plugins.service.PluginService` operations
and maps the typed platform errors to HTTP status codes. The service is built
per-request from ``container.session_factory`` (the committing unit-of-work),
mirroring how the feature-flags router stays self-contained.

Audiences:

* **Publishers** (authenticated) — ``POST /plugins`` to publish an artifact,
  ``POST /plugins/{id}/{version}/sign-check`` advisory.
* **Reviewers/admins** — ``POST /plugins/{id}/{version}/review`` decisions.
* **Tenants** — browse the catalog, install/enable/disable/upgrade/rollback,
  rate, and run a dispatch dry-run over an extension point.

Per-tenant isolation: the ``owner`` of an installation is the authenticated
user's id, so one user's plugins never leak into another's dispatch.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.platform.plugins.errors import (
    CapabilityDeniedError,
    DependencyResolutionError,
    LifecycleError,
    PluginError,
    PluginNotFoundError,
    PluginValidationError,
    RegistryError,
    SignatureError,
)
from app.platform.plugins.hooks import ExtensionPoint
from app.platform.plugins.service import PluginPlatformConfig, PluginService, PluginUnitOfWork

logger = get_logger("app.api.plugins")

router = APIRouter(prefix="/plugins", tags=["plugins"])


# --------------------------------------------------------------------------- #
# Service construction (self-contained, per-request)
# --------------------------------------------------------------------------- #


def _build_service(container: Any) -> PluginService:
    """Construct a :class:`PluginService` from the wired container.

    The platform config can be overridden by the container if a host configures
    it (``container.plugin_platform_config``); otherwise a safe default is used
    (no signature requirement, auto-approve low-risk for the dev marketplace).
    The host-services factory is taken from the container if present, else an
    empty broker (every host capability denied) — so a misconfigured deploy
    fails closed rather than open.
    """
    config = getattr(container, "plugin_platform_config", None) or PluginPlatformConfig()
    signer = getattr(container, "plugin_signer", None)
    services_factory = getattr(container, "plugin_host_services_factory", None)

    def uow() -> PluginUnitOfWork:
        return PluginUnitOfWork(container.session_factory)

    return PluginService(
        uow=uow,
        config=config,
        signer=signer,
        host_services_factory=services_factory,
    )


def _map_error(exc: PluginError) -> APIError:
    """Map a typed platform error to an HTTP error."""
    if isinstance(exc, PluginNotFoundError):
        return APIError(exc.code, str(exc), status=404)
    if isinstance(exc, (PluginValidationError, SignatureError, RegistryError)):
        return APIError(exc.code, str(exc), status=422)
    if isinstance(exc, (LifecycleError, DependencyResolutionError)):
        return APIError(exc.code, str(exc), status=409)
    if isinstance(exc, CapabilityDeniedError):
        return APIError(exc.code, str(exc), status=403)
    return APIError(exc.code, str(exc), status=400)


# --------------------------------------------------------------------------- #
# Request/response bodies
# --------------------------------------------------------------------------- #


class PublishBody(BaseModel):
    manifest: dict[str, Any]
    source: str
    signature: dict[str, Any] | None = None


class ReviewBody(BaseModel):
    decision: str
    notes: str = ""


class RateBody(BaseModel):
    stars: int = Field(ge=1, le=5)
    review: str = ""


class InstallBody(BaseModel):
    version: str
    grants: list[str] | None = None
    enable: bool = False


class UpgradeBody(BaseModel):
    to_version: str


class RollbackBody(BaseModel):
    to_version: str | None = None


class DispatchBody(BaseModel):
    point: str
    payload: Any = None
    fail_fast: bool = False


# --------------------------------------------------------------------------- #
# Marketplace
# --------------------------------------------------------------------------- #


@router.get("")
async def list_catalog(
    container: ContainerDep,
    _user: CurrentUser,
    include_pending: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List the marketplace catalog (latest approved version per plugin)."""
    svc = _build_service(container)
    items = await svc.catalog(include_pending=include_pending, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@router.post("", dependencies=[Depends(write_rate_limit)])
async def publish(body: PublishBody, container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    """Publish a plugin artifact (validated, hashed, optionally signed)."""
    svc = _build_service(container)
    try:
        return await svc.publish(
            manifest_data=body.manifest,
            source=body.source,
            signature=body.signature,
            actor=str(user.id),
        )
    except PluginError as exc:
        raise _map_error(exc) from exc


@router.post("/{plugin_id}/{version}/review", dependencies=[Depends(write_rate_limit)])
async def review(
    plugin_id: str, version: str, body: ReviewBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Apply a moderation decision (approve / reject / request_changes / yank)."""
    svc = _build_service(container)
    try:
        return await svc.review(
            plugin_id=plugin_id,
            version=version,
            decision=body.decision,
            reviewer=str(user.id),
            notes=body.notes,
        )
    except PluginError as exc:
        raise _map_error(exc) from exc


@router.post("/{plugin_id}/rate", dependencies=[Depends(write_rate_limit)])
async def rate(
    plugin_id: str, body: RateBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Rate a plugin (1–5 stars); one rating per user (re-rating upserts)."""
    svc = _build_service(container)
    try:
        stats = await svc.rate(
            plugin_id=plugin_id, user_id=str(user.id), stars=body.stars, review=body.review
        )
    except PluginError as exc:
        raise _map_error(exc) from exc
    return {"average": stats.average, "count": stats.count}


# --------------------------------------------------------------------------- #
# Lifecycle (per-tenant)
# --------------------------------------------------------------------------- #


@router.get("/installed")
async def list_installed(container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    """List the authenticated tenant's installed plugins + their state."""
    svc = _build_service(container)
    items = await svc.list_installations(owner=str(user.id))
    return {"items": items, "count": len(items)}


@router.post("/{plugin_id}/install", dependencies=[Depends(write_rate_limit)])
async def install(
    plugin_id: str, body: InstallBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Install (and optionally enable) a plugin for the authenticated tenant."""
    svc = _build_service(container)
    try:
        inst = await svc.install(
            owner=str(user.id),
            plugin_id=plugin_id,
            version=body.version,
            requested_grants=body.grants,
            enable=body.enable,
            actor=str(user.id),
        )
    except PluginError as exc:
        raise _map_error(exc) from exc
    return _installation_body(inst)


@router.post("/{plugin_id}/enable", dependencies=[Depends(write_rate_limit)])
async def enable(plugin_id: str, container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    return await _lifecycle(container, user, "enable", plugin_id)


@router.post("/{plugin_id}/disable", dependencies=[Depends(write_rate_limit)])
async def disable(plugin_id: str, container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    return await _lifecycle(container, user, "disable", plugin_id)


@router.post("/{plugin_id}/uninstall", dependencies=[Depends(write_rate_limit)])
async def uninstall(plugin_id: str, container: ContainerDep, user: CurrentUser) -> dict[str, Any]:
    return await _lifecycle(container, user, "uninstall", plugin_id)


@router.post("/{plugin_id}/upgrade", dependencies=[Depends(write_rate_limit)])
async def upgrade(
    plugin_id: str, body: UpgradeBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _build_service(container)
    try:
        inst = await svc.upgrade(
            owner=str(user.id),
            plugin_id=plugin_id,
            to_version=body.to_version,
            actor=str(user.id),
        )
    except PluginError as exc:
        raise _map_error(exc) from exc
    return _installation_body(inst)


@router.post("/{plugin_id}/rollback", dependencies=[Depends(write_rate_limit)])
async def rollback(
    plugin_id: str, body: RollbackBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    svc = _build_service(container)
    try:
        inst = await svc.rollback(
            owner=str(user.id),
            plugin_id=plugin_id,
            to_version=body.to_version,
            actor=str(user.id),
        )
    except PluginError as exc:
        raise _map_error(exc) from exc
    return _installation_body(inst)


@router.post("/dispatch", dependencies=[Depends(write_rate_limit)])
async def dispatch(
    body: DispatchBody, container: ContainerDep, user: CurrentUser
) -> dict[str, Any]:
    """Run the tenant's enabled hooks at an extension point over a payload.

    A dry-run / inspection surface: returns the per-hook outcomes (value,
    capabilities used, isolated failures) and, for transform points, the folded
    payload. Useful for testing a plugin set without driving the real pipeline.
    """
    try:
        point = ExtensionPoint(body.point)
    except ValueError as exc:
        raise APIError(
            "plugin_invalid", f"unknown extension point {body.point!r}", status=422
        ) from exc
    svc = _build_service(container)
    try:
        report = await svc.dispatch(
            owner=str(user.id), point=point, payload=body.payload, fail_fast=body.fail_fast
        )
    except PluginError as exc:
        raise _map_error(exc) from exc
    return {
        "point": report.point.value,
        "kind": report.kind.value,
        "payload": report.payload,
        "outcomes": [
            {
                "plugin_id": o.plugin_id,
                "hook_id": o.hook_id,
                "ok": o.ok,
                "value": o.value,
                "error_code": o.error_code,
                "error": o.error,
                "host_calls": o.host_calls,
                "capabilities_used": list(o.capabilities_used),
            }
            for o in report.outcomes
        ],
    }


async def _lifecycle(container: Any, user: Any, action: str, plugin_id: str) -> dict[str, Any]:
    svc = _build_service(container)
    fn = {"enable": svc.enable, "disable": svc.disable, "uninstall": svc.uninstall}[action]
    try:
        inst = await fn(owner=str(user.id), plugin_id=plugin_id, actor=str(user.id))
    except PluginError as exc:
        raise _map_error(exc) from exc
    return _installation_body(inst)


def _installation_body(inst: Any) -> dict[str, Any]:
    return {
        "plugin_id": inst.plugin_id,
        "version": str(inst.version),
        "state": inst.state.value,
        "active": inst.is_active,
        "failure_count": inst.failure_count,
    }


__all__ = ["router"]
