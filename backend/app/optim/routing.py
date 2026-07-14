"""Model router — route each call-site to the cheapest Qwen model that holds the quality bar.

**Behavior-preserving by construction.** Today every agent hard-binds its model at construction
(``showrunner→qwen3.7-max``, ``continuity→qwen3.7-plus``, ``adapter``/``cinematographer→
qwen3.5-plus``, ``critic→qwen-vl-max``). The router is the *identity* unless an operator both
enables it (``optim_routing_enabled``) and supplies explicit per-site overrides
(``optim_routing_overrides_json``). Wiring the router in alone changes nothing — savings are an
opt-in, auditable decision.

We ship **no** default overrides: a model downshift trades quality, and that trade needs the §13
eval to confirm it holds. :data:`SUGGESTED_OVERRIDES` is a *documented menu* of quality-guarded
candidates an operator can adopt after evaluating them — never applied automatically.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.optim.routing")

#: Canonical call-site keys (one per model-bound agent + the comment classifier).
SITES: tuple[str, ...] = (
    "showrunner",
    "continuity",
    "adapter",
    "cinematographer",
    "critic",
    "comment_classifier",
)

#: A documented menu of candidate downshifts (cheaper model that *plausibly* holds quality for the
#: site). NOT applied by default — opt in after the §13 eval confirms no quality regression.
SUGGESTED_OVERRIDES: dict[str, str] = {
    # The cinematographer turns an already-structured beat into shot directions — a constrained,
    # template-shaped task the cheap adapter tier handles well. (It already uses the adapter model;
    # this pins it so a future default bump to a pricier tier can't silently raise cost.)
    "cinematographer": "qwen3.5-plus",
    # The Director comment classifier is a short, low-entropy intent label — cheapest tier suffices.
    "comment_classifier": "qwen3.5-plus",
}


class ModelRouter:
    """Resolve ``(call-site, default model) -> model``. Identity unless enabled + overridden."""

    def __init__(
        self, *, enabled: bool = False, overrides: Mapping[str, str] | None = None
    ) -> None:
        self._enabled = enabled
        self._overrides: dict[str, str] = dict(overrides or {})

    @property
    def enabled(self) -> bool:
        return self._enabled

    def route(self, site: str, default_model: str) -> str:
        """Return the model to use for ``site``. Disabled ⇒ ``default_model`` (never raises)."""
        if not self._enabled:
            return default_model
        chosen = self._overrides.get(site, default_model)
        if chosen != default_model:
            logger.debug("routing.override", site=site, was=default_model, now=chosen)
        return chosen

    @classmethod
    def from_settings(cls, settings: Any) -> ModelRouter:
        """Build from ``optim_routing_enabled`` + ``optim_routing_overrides_json`` (optional)."""
        enabled = bool(getattr(settings, "optim_routing_enabled", False))
        raw = getattr(settings, "optim_routing_overrides_json", None)
        overrides: dict[str, str] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, Mapping):
                    overrides = {str(k): str(v) for k, v in parsed.items()}
            except (ValueError, TypeError):
                logger.warning("routing.bad_overrides_json", chars=len(str(raw)))
        return cls(enabled=enabled, overrides=overrides)


def models_for_sites(router: ModelRouter, defaults: Mapping[str, str]) -> dict[str, str]:
    """Map a ``{site: default_model}`` table through the router (for cost projections/tests)."""
    return {site: router.route(site, default) for site, default in defaults.items()}


__all__ = ["SITES", "SUGGESTED_OVERRIDES", "ModelRouter", "models_for_sites"]
