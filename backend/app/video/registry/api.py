"""Read-only introspection API over the video-provider registry.

A small, self-contained :class:`~fastapi.APIRouter` (mounted under ``/api`` →
``/api/video/...``) that lets an operator / dashboard / sibling service *discover*
which video models exist and what each can do — without touching the render path
and without any spend (``KINORA_LIVE_VIDEO`` is irrelevant here):

* ``GET /video/providers`` — the catalog: every provider with its kind,
  capabilities, effective enabled state, weight, rollout, and cost-tier ref.
  Optional filters: ``?kind=``, ``?enabled_only=true``, ``?routable_only=true``.
* ``GET /video/providers/{id}`` — one provider (404 if unknown).
* ``GET /video/capabilities?mode=&duration=&resolution=&require_audio=`` — which
  providers can serve *this* request, best-weighted first, plus the ideal
  weighted traffic split (the canary/A-B view) over the matches.

The router resolves its :class:`~app.video.registry.registry.VideoProviderRegistry`
through the :func:`get_registry` dependency, which lazily builds a process-wide
instance from the checked-in catalog. Tests override that dependency (or call
:func:`set_registry`) to inject a fixture registry — so the whole surface runs
under a plain :class:`~fastapi.testclient.TestClient` with no app container.

This is deliberately **read-only**: it never mutates flags/weights or reloads the
catalog (those are operations the composition root / an admin surface owns).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import APIError
from app.video.registry.capabilities import CapabilityProfile
from app.video.registry.catalog import ProviderEntry, ProviderKind
from app.video.registry.registry import VideoProviderRegistry

router = APIRouter(prefix="/video", tags=["video-registry"])

#: Process-wide registry, built lazily from the checked-in catalog on first use.
_REGISTRY: VideoProviderRegistry | None = None


def get_registry() -> VideoProviderRegistry:
    """Return the process-wide registry (lazily built from the default catalog).

    Overridable two ways: FastAPI ``dependency_overrides[get_registry]`` (per
    app/test), or :func:`set_registry` (process-wide). The lazy default keeps
    importing this module cheap and free of I/O until a route is actually hit.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = VideoProviderRegistry.from_default()
    return _REGISTRY


def set_registry(registry: VideoProviderRegistry | None) -> None:
    """Install (or clear) the process-wide registry the API serves.

    The composition root calls this with the container's registry so the
    introspection API and the render path share one instance; passing ``None``
    resets to the lazy default (handy for test isolation).
    """
    global _REGISTRY
    _REGISTRY = registry


RegistryDep = Annotated[VideoProviderRegistry, Depends(get_registry)]


# --------------------------------------------------------------------------- #
# Wire shapes
# --------------------------------------------------------------------------- #


class CapabilityView(BaseModel):
    """JSON projection of a :class:`CapabilityProfile` (sorted, list-typed)."""

    model_config = ConfigDict(extra="forbid")

    modes: list[str]
    resolutions: list[str]
    max_resolution: str
    min_duration_s: float
    max_duration_s: float
    max_fps: int
    supports_audio: bool
    supports_seed: bool
    supports_negative_prompt: bool

    @classmethod
    def of(cls, profile: CapabilityProfile) -> CapabilityView:
        return cls(
            modes=sorted(m.value for m in profile.modes),
            resolutions=sorted((r.value for r in profile.resolutions), key=_res_key),
            max_resolution=profile.max_resolution.value,
            min_duration_s=profile.min_duration_s,
            max_duration_s=profile.max_duration_s,
            max_fps=profile.max_fps,
            supports_audio=profile.supports_audio,
            supports_seed=profile.supports_seed,
            supports_negative_prompt=profile.supports_negative_prompt,
        )


def _res_key(label: str) -> int:
    """Numeric sort key for a resolution label (``"720P"`` → 720)."""
    return int(label.rstrip("Pp") or 0)


class ProviderView(BaseModel):
    """JSON projection of one provider, with effective runtime state applied."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    kind: ProviderKind
    rollout: str
    cost_tier: str
    provider_backend: str
    tags: list[str]
    enabled: bool = Field(description="Effective enabled state (overrides applied).")
    weight: float = Field(description="Effective routing weight (overrides applied).")
    routable: bool = Field(description="Eligible for routing under effective flags.")
    capabilities: CapabilityView

    @classmethod
    def of(cls, entry: ProviderEntry, registry: VideoProviderRegistry) -> ProviderView:
        enabled = registry.is_enabled(entry.id)
        weight = registry.effective_weight(entry.id)
        return cls(
            id=entry.id,
            display_name=entry.label,
            kind=entry.kind,
            rollout=entry.rollout.value,
            cost_tier=entry.cost_tier,
            provider_backend=entry.provider_backend,
            tags=list(entry.tags),
            enabled=enabled,
            weight=weight,
            routable=weight > 0.0 and enabled,
            capabilities=CapabilityView.of(entry.capabilities),
        )


class ProviderListResponse(BaseModel):
    """The provider catalog (filtered)."""

    model_config = ConfigDict(extra="forbid")

    count: int
    providers: list[ProviderView]


class CapabilityMatch(BaseModel):
    """One provider that can serve a capability query, with its traffic share."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderView
    expected_share: float = Field(
        description="Ideal weighted traffic share among the matches (0..1)."
    )


class CapabilityQueryResponse(BaseModel):
    """Which providers can serve a request + the canary/A-B traffic split."""

    model_config = ConfigDict(extra="forbid")

    query: dict[str, object]
    count: int
    matches: list[CapabilityMatch]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/providers", response_model=ProviderListResponse)
async def list_providers(
    registry: RegistryDep,
    kind: ProviderKind | None = None,
    enabled_only: bool = False,
    routable_only: bool = False,
) -> ProviderListResponse:
    """List the video-provider catalog with effective runtime state.

    ``enabled_only`` keeps providers whose effective enabled flag is on;
    ``routable_only`` additionally requires a positive effective weight and a
    non-disabled rollout (the stricter "would actually get traffic" filter).
    """
    entries = registry.routable() if routable_only else registry.all()
    if kind is not None:
        entries = [e for e in entries if e.kind is kind]
    views = [ProviderView.of(e, registry) for e in entries]
    if enabled_only:
        views = [v for v in views if v.enabled]
    return ProviderListResponse(count=len(views), providers=views)


@router.get("/providers/{provider_id}", response_model=ProviderView)
async def get_provider(provider_id: str, registry: RegistryDep) -> ProviderView:
    """One provider's full record (404 if unknown)."""
    entry = registry.get(provider_id)
    if entry is None:
        raise APIError("provider_not_found", f"no such video provider: {provider_id}", status=404)
    return ProviderView.of(entry, registry)


@router.get("/capabilities", response_model=CapabilityQueryResponse)
async def query_capabilities(
    registry: RegistryDep,
    mode: Annotated[
        str | None, Query(description="Render mode (t2v/i2v/r2v/flf/continuation/edit or WanMode).")
    ] = None,
    duration: Annotated[
        float | None, Query(gt=0, description="Required clip duration in seconds.")
    ] = None,
    resolution: Annotated[
        str | None, Query(description="Minimum acceptable resolution (e.g. 720P).")
    ] = None,
    require_audio: Annotated[
        bool, Query(description="Only providers that emit a synchronized audio track.")
    ] = False,
    kind: ProviderKind | None = None,
) -> CapabilityQueryResponse:
    """Which routable providers can serve this request (+ the weighted split).

    A bad ``mode``/``resolution`` is a 400 (the message lists accepted values).
    Results are best-weighted first; ``expected_share`` is each match's ideal
    slice of traffic among the matches — the canary/A-B view.
    """
    try:
        entries = registry.query(
            mode=mode,
            duration_s=duration,
            resolution=resolution,
            require_audio=require_audio,
            kind=kind,
        )
        split = registry.expected_split(
            mode=mode,
            duration_s=duration,
            resolution=resolution,
            require_audio=require_audio,
            kind=kind,
        )
    except ValueError as exc:  # unknown mode / resolution from coerce()
        raise APIError("invalid_query", str(exc), status=400) from exc
    matches = [
        CapabilityMatch(
            provider=ProviderView.of(e, registry),
            expected_share=split.get(e.id, 0.0),
        )
        for e in entries
    ]
    query = {
        "mode": mode,
        "duration_s": duration,
        "resolution": resolution,
        "require_audio": require_audio,
        "kind": kind.value if kind is not None else None,
    }
    return CapabilityQueryResponse(query=query, count=len(matches), matches=matches)


__all__ = [
    "CapabilityMatch",
    "CapabilityQueryResponse",
    "CapabilityView",
    "ProviderListResponse",
    "ProviderView",
    "get_registry",
    "router",
    "set_registry",
]
