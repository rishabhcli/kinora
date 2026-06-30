"""The dialect registry — look a :class:`PromptDialect` up by model name.

A small, explicit registry (no import-time scanning magic) so the set of dialects
is auditable and deterministic. Built-ins are registered eagerly the first time a
default registry is requested; callers may register additional dialects on a
private registry instance.

Aliases let common model ids and family names resolve to the right dialect
("gen3" → runway, "dream-machine" → luma, "hailuo"/"minimax" → the generic
dialect until a dedicated one lands). Unknown names fall back to ``generic``.

Pure / deterministic apart from the lazy build of the default singleton.
"""

from __future__ import annotations

import structlog

from .base import PromptDialect, RenderedPrompt
from .canonical import ShotDescription
from .dialects.generic import GenericDialect
from .dialects.kling import KlingDialect
from .dialects.luma import LumaDialect
from .dialects.pika import PikaDialect
from .dialects.runway import RunwayDialect
from .dialects.sora import SoraDialect
from .dialects.veo import VeoDialect
from .dialects.wan import WanDialect

_log = structlog.get_logger(__name__)

#: The name returned (and used) when a requested model is unknown.
FALLBACK_DIALECT = "generic"

#: Free-text aliases → canonical dialect name. Lower-cased on lookup.
_ALIASES: dict[str, str] = {
    # Wan / DashScope family
    "wan2.1-t2v-turbo": "wan",
    "wan2.1-i2v-turbo": "wan",
    "wan2.5-t2v-preview": "wan",
    "wan2.2-i2v-plus": "wan",
    "dashscope": "wan",
    # Runway
    "gen3": "runway",
    "gen-3": "runway",
    "gen3a_turbo": "runway",
    "runwayml": "runway",
    # Luma
    "dream-machine": "luma",
    "dream_machine": "luma",
    "ray-2": "luma",
    # Kling
    "kuaishou": "kling",
    "kling-v2": "kling",
    # Veo
    "google-veo": "veo",
    "veo-3": "veo",
    # Sora
    "openai-sora": "sora",
    # Models without a dedicated dialect (yet) → the safe generic baseline.
    "minimax": FALLBACK_DIALECT,
    "hailuo": FALLBACK_DIALECT,
    "mochi": FALLBACK_DIALECT,
    "cogvideo": FALLBACK_DIALECT,
    "hunyuan": FALLBACK_DIALECT,
    "ltx": FALLBACK_DIALECT,
}


class DialectRegistry:
    """A registry of :class:`PromptDialect` instances keyed by ``spec.name``."""

    def __init__(self) -> None:
        self._dialects: dict[str, PromptDialect] = {}
        self._aliases: dict[str, str] = {}

    def register(self, dialect: PromptDialect, *, aliases: tuple[str, ...] = ()) -> None:
        """Register ``dialect`` under its ``spec.name`` (+ optional aliases).

        Re-registering the same name replaces the prior dialect (last wins) — a
        caller can override a built-in with a tuned variant.
        """
        self._dialects[dialect.name] = dialect
        for alias in aliases:
            self._aliases[alias.strip().lower()] = dialect.name

    def names(self) -> list[str]:
        """The registered canonical dialect names, sorted."""
        return sorted(self._dialects)

    def has(self, name: str) -> bool:
        """True when ``name`` (or an alias) resolves to a registered dialect."""
        key = name.strip().lower()
        return key in self._dialects or key in self._aliases

    def resolve_name(self, name: str) -> str:
        """The canonical dialect name for ``name`` (alias-aware), or the fallback.

        Never raises: an unknown name resolves to :data:`FALLBACK_DIALECT` (logged
        once at debug so a typo'd model id is visible without breaking a render).
        """
        key = name.strip().lower()
        if key in self._dialects:
            return key
        if key in self._aliases:
            return self._aliases[key]
        _log.debug("prompt_dialect.unknown_model", requested=name, fallback=FALLBACK_DIALECT)
        return FALLBACK_DIALECT

    def get(self, name: str) -> PromptDialect:
        """The dialect for ``name`` (alias-aware), falling back to ``generic``."""
        return self._dialects[self.resolve_name(name)]

    def render(
        self, name: str, shot: ShotDescription, *, budget: int | None = None
    ) -> RenderedPrompt:
        """Resolve ``name`` and render ``shot`` through that dialect (convenience)."""
        return self.get(name).render(shot, budget=budget)


def build_default_registry() -> DialectRegistry:
    """A fresh registry with every built-in dialect + the standard aliases registered."""
    registry = DialectRegistry()
    registry.register(WanDialect())
    registry.register(RunwayDialect())
    registry.register(PikaDialect())
    registry.register(KlingDialect())
    registry.register(LumaDialect())
    registry.register(VeoDialect())
    registry.register(SoraDialect())
    registry.register(GenericDialect())
    for alias, target in _ALIASES.items():
        registry._aliases[alias] = target  # noqa: SLF001 — building the default registry
    return registry


_DEFAULT: DialectRegistry | None = None


def default_registry() -> DialectRegistry:
    """The process-wide default registry (built lazily on first use)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_default_registry()
    return _DEFAULT


def get_dialect(name: str) -> PromptDialect:
    """Look up a dialect by model name on the default registry (alias-aware)."""
    return default_registry().get(name)


def render_for(name: str, shot: ShotDescription, *, budget: int | None = None) -> RenderedPrompt:
    """Render ``shot`` for model ``name`` on the default registry (convenience)."""
    return default_registry().render(name, shot, budget=budget)


__all__ = [
    "FALLBACK_DIALECT",
    "DialectRegistry",
    "build_default_registry",
    "default_registry",
    "get_dialect",
    "render_for",
]
