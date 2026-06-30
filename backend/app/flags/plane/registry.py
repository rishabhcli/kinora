"""The flag registry — the typed catalog of every runtime flag + its live base.

:class:`FlagRegistry` is an immutable map of flag key -> :class:`FlagSpec`. It is
the catalog half of the plane (the override layer is the runtime half). The
registry is built from a list of specs whose ``setting`` field names the
:class:`~app.core.config.Settings` attribute the flag mirrors; :func:`bind_settings`
stamps each spec's ``default`` with the *live* Settings value so the plane's base
layer literally *is* Settings — there is one source of truth, not two.

:func:`build_default_registry` enumerates Kinora's real gated knobs (the
``KINORA_LIVE_VIDEO`` spend gate, the provider gateway, the video backend, the
§9.7 render-hardening toggles, the scheduler watermarks, analytics / llmops
gates) so the existing scattered ``if settings.x`` checks have a single typed
home to migrate toward — additively, at each call site's own pace.

Pure: the *catalog* needs no infra; binding reads a Settings object the caller
passes in (so tests bind a stub).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.flags.plane.errors import UnknownFlagError
from app.flags.plane.spec import FlagSpec, FlagType

if TYPE_CHECKING:  # avoid importing Settings at module import (keeps the core pure)
    from app.core.config import Settings


class FlagRegistry:
    """An immutable, typed catalog of runtime flag specs."""

    def __init__(self, specs: tuple[FlagSpec, ...]) -> None:
        by_key: dict[str, FlagSpec] = {}
        for spec in specs:
            if spec.key in by_key:
                raise ValueError(f"duplicate flag key {spec.key!r} in registry")
            by_key[spec.key] = spec
        self._specs = by_key

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, key: str) -> FlagSpec:
        """The spec for ``key`` (raises :class:`UnknownFlagError` if absent)."""
        spec = self._specs.get(key)
        if spec is None:
            raise UnknownFlagError(key)
        return spec

    def try_get(self, key: str) -> FlagSpec | None:
        """The spec for ``key`` or ``None`` (non-raising lookup)."""
        return self._specs.get(key)

    def keys(self) -> tuple[str, ...]:
        """All registered flag keys (sorted)."""
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[FlagSpec, ...]:
        """All specs (sorted by key)."""
        return tuple(self._specs[k] for k in self.keys())

    def kill_switches(self) -> tuple[FlagSpec, ...]:
        """The subset of specs marked as guarded kill-switches."""
        return tuple(s for s in self.specs() if s.kill_switch)

    def bind_settings(self, settings: Settings) -> FlagRegistry:
        """Return a registry whose defaults are the *live* Settings values.

        For each spec with a ``setting`` field, the matching Settings attribute
        becomes the spec's base value (coerced through the spec's type). A spec
        with no ``setting`` (or a name not present on Settings) keeps its static
        default. This is what makes the plane read-through: the base layer is
        always whatever Settings currently says.
        """
        bound: list[FlagSpec] = []
        for spec in self.specs():
            if spec.setting and hasattr(settings, spec.setting):
                live = getattr(settings, spec.setting)
                bound.append(spec.with_default(spec.coerce(live)))
            else:
                bound.append(spec)
        return FlagRegistry(tuple(bound))

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe catalog projection for the admin API / snapshot."""
        return {"flags": [s.to_dict() for s in self.specs()]}


# --------------------------------------------------------------------------- #
# The default Kinora flag catalog.
#
# Each spec mirrors an existing Settings field (``setting=``). Adding a flag here
# never changes behaviour — the base value is still whatever Settings says; it
# merely makes the knob addressable through the unified runtime plane (overrides,
# targeting, rollout, audit). KINORA_LIVE_VIDEO is the one guarded kill-switch.
# --------------------------------------------------------------------------- #

_DEFAULT_SPECS: tuple[FlagSpec, ...] = (
    FlagSpec(
        key="kinora.live_video",
        type=FlagType.BOOL,
        default=False,
        setting="kinora_live_video",
        kill_switch=True,
        owner="platform",
        description=(
            "The live-video go-live gate (real Wan/MiniMax spend). A guarded "
            "kill-switch: the runtime plane can only ever force it OFF, never ON."
        ),
        tags=("spend", "video", "safety"),
    ),
    FlagSpec(
        key="provider.gateway_enabled",
        type=FlagType.BOOL,
        default=False,
        setting="provider_gateway_enabled",
        owner="providers",
        description="Hardened provider gateway (breakers/retries/hedging/cache).",
        tags=("providers", "resilience"),
    ),
    FlagSpec(
        key="provider.gateway_cache_enabled",
        type=FlagType.BOOL,
        default=True,
        setting="provider_gateway_cache_enabled",
        owner="providers",
        description="Provider-gateway response cache + in-flight dedup.",
        tags=("providers", "cache"),
    ),
    FlagSpec(
        key="video.backend",
        type=FlagType.STRING,
        default="dashscope",
        setting="video_backend",
        owner="render",
        choices=("dashscope", "minimax"),
        description="Which hosted video provider the render pipeline uses.",
        tags=("video", "render", "providers"),
    ),
    FlagSpec(
        key="render.checkpoint_enabled",
        type=FlagType.BOOL,
        default=True,
        setting="render_checkpoint_enabled",
        owner="render",
        description="§9.7 render checkpoints (resume mid-render across restarts).",
        tags=("render", "hardening"),
    ),
    FlagSpec(
        key="render.poison_threshold",
        type=FlagType.INT,
        default=3,
        setting="render_poison_threshold",
        owner="render",
        description="Render crashes before a shot is quarantined to the bottom rung.",
        tags=("render", "hardening"),
    ),
    FlagSpec(
        key="ingest.ocr_enabled",
        type=FlagType.BOOL,
        default=False,
        setting="ingest_ocr_enabled",
        owner="ingest",
        description="OCR fallback for scanned/image PDFs during ingest.",
        tags=("ingest",),
    ),
    FlagSpec(
        key="ingest.layout_reorder",
        type=FlagType.BOOL,
        default=True,
        setting="ingest_layout_reorder",
        owner="ingest",
        description="Reorder PDF text blocks into reading order during ingest.",
        tags=("ingest",),
    ),
    FlagSpec(
        key="analytics.enabled",
        type=FlagType.BOOL,
        default=True,
        setting="analytics_enabled",
        owner="data",
        description="Product-analytics event pipeline.",
        tags=("analytics",),
    ),
    FlagSpec(
        key="llmops.enabled",
        type=FlagType.BOOL,
        default=False,
        setting="llmops_enabled",
        owner="llmops",
        description="LLM-ops surface (prompt registry / eval / guardrails).",
        tags=("llmops",),
    ),
    FlagSpec(
        key="translation.enabled",
        type=FlagType.BOOL,
        default=True,
        setting="translation_enabled",
        owner="content",
        description="Content-translation subsystem.",
        tags=("translation",),
    ),
    FlagSpec(
        key="scheduler.watermark_low_s",
        type=FlagType.FLOAT,
        default=25.0,
        setting="watermark_low_s",
        owner="scheduler",
        description="Low watermark (reading-seconds) that triggers buffer refill.",
        tags=("scheduler", "watermark"),
    ),
    FlagSpec(
        key="scheduler.watermark_high_s",
        type=FlagType.FLOAT,
        default=75.0,
        setting="watermark_high_s",
        owner="scheduler",
        description="High watermark (reading-seconds) the buffer fills toward.",
        tags=("scheduler", "watermark"),
    ),
    FlagSpec(
        key="budget.ceiling_usd",
        type=FlagType.FLOAT,
        default=30.0,
        setting="budget_ceiling_usd",
        owner="finops",
        kill_switch=True,
        description=(
            "Hard USD spend ceiling. A guarded kill-switch: the runtime plane can "
            "only ever lower it, never raise the cap."
        ),
        tags=("budget", "finops", "spend", "safety"),
    ),
)


def build_default_registry() -> FlagRegistry:
    """The default Kinora runtime-flag catalog (static defaults; unbound)."""
    return FlagRegistry(_DEFAULT_SPECS)


def bind_settings(registry: FlagRegistry, settings: Settings) -> FlagRegistry:
    """Convenience: bind ``registry`` to ``settings`` (delegates to the method)."""
    return registry.bind_settings(settings)


__all__ = ["FlagRegistry", "bind_settings", "build_default_registry"]
