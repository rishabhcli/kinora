"""Config-driven construction of a :class:`RoutingVideoRouter` (additive seam).

Turns a set of hosted Wan model ids (and optional MiniMax / second-region
backends) on a shared :class:`~app.providers.base.ProviderClient` into a v2 router
with sensible per-id profiles derived from the model name, behind a single
:func:`build_routing_router` call. This is the opt-in DI seam the composition root
can wire when ``video_router_v2_enabled`` is set; by default nothing constructs
it, so the round-1 single-backend path is unchanged.

Profiles are inferred heuristically from the model id (a ``turbo`` id is cheaper /
lower quality / faster than a ``plus`` / ``preview`` quality id) so a router built
purely from ids still ranks meaningfully without hand-tuned tables.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.providers.base import ProviderClient
from app.providers.types import WanMode
from app.providers.video import VideoProvider
from app.providers.video_router import VideoBackend

from .capabilities import ALL_MODES, ProviderProfile
from .concurrency import GateConfig
from .policy import PolicyKind, build_policy
from .router import RouterV2Policy, RoutingVideoRouter

#: Substrings in a model id that mark a *cheaper / faster / lower-fidelity* tier.
_TURBO_MARKERS = ("turbo", "fast", "flash", "lite")
#: Substrings that mark a *quality / higher-fidelity* tier.
_QUALITY_MARKERS = ("plus", "preview", "pro", "max", "quality")


def infer_profile(model_id: str, *, modes: frozenset[WanMode] = ALL_MODES) -> ProviderProfile:
    """Heuristically derive a :class:`ProviderProfile` from a hosted model id.

    Turbo-class ids → cheaper, faster, lower quality; quality-class ids → pricier,
    slower, higher quality; anything else → a neutral middle tier. Pure + stable.
    """
    lowered = model_id.lower()
    is_turbo = any(marker in lowered for marker in _TURBO_MARKERS)
    is_quality = any(marker in lowered for marker in _QUALITY_MARKERS)
    if is_turbo and not is_quality:
        return ProviderProfile(
            modes=modes, cost_per_s=1.0, quality=0.45, est_latency_s=25.0, weight=1.0
        )
    if is_quality and not is_turbo:
        return ProviderProfile(
            modes=modes, cost_per_s=4.0, quality=0.9, est_latency_s=70.0, weight=1.0
        )
    return ProviderProfile(modes=modes, cost_per_s=2.0, quality=0.65, est_latency_s=45.0)


def build_routing_router(
    client: ProviderClient,
    *,
    model_ids: Sequence[str],
    policy_kind: PolicyKind | str = PolicyKind.WEIGHTED,
    hedge: int = 1,
    sticky: bool = True,
    per_backend_concurrency: int = 4,
    profiles: Mapping[str, ProviderProfile] | None = None,
    name: str = "video-router-v2",
) -> RoutingVideoRouter:
    """Build a v2 router over several hosted Wan ids on one shared client.

    Each id becomes a :class:`~app.providers.video.VideoProvider` named
    ``video:<id>`` (its ``WanSpec.model`` resolves to that id when the spec leaves
    ``model`` unset). The first id is the preferred backend; profiles are inferred
    from the id unless overridden via ``profiles`` (keyed by the ``video:<id>``
    name). All backends share the one resilient transport, so cost/budget stays
    unified.
    """
    if not model_ids:
        raise ValueError("build_routing_router requires at least one model id")
    backends: list[VideoBackend] = [
        VideoProvider(client, name=f"video:{model_id}") for model_id in model_ids
    ]
    inferred = {f"video:{mid}": infer_profile(mid) for mid in model_ids}
    if profiles:
        inferred.update(profiles)
    gates = {b.name: GateConfig(max_concurrency=per_backend_concurrency) for b in backends}
    return RoutingVideoRouter(
        backends,
        policy=RouterV2Policy(selection=build_policy(policy_kind), hedge=hedge, sticky=sticky),
        profiles=inferred,
        gates=gates,
        name=name,
    )


__all__ = [
    "build_routing_router",
    "infer_profile",
]
