"""Extension points + their typed payload contracts.

An **extension point** is a named seam in Kinora where plugin code may attach.
Each point has a *direction* and a *typed contract* — what the host passes in
and what (if anything) the plugin may return — so a hook is a pure function of
its payload and the registry can type-check a registration against the point it
targets.

The four families the task calls for:

* :attr:`ExtensionPoint.INGEST_FILTER` — runs in Phase-A ingest (§9.1) over a
  page's extracted text/beats; may *transform* the payload (a filter pipeline).
* :attr:`ExtensionPoint.CUSTOM_AGENT` — a plugin-provided agent step that
  receives a typed request and returns a typed response (the §7 contract shape).
* :attr:`ExtensionPoint.RENDER_POSTPROCESS` — runs after a shot renders (§9.7);
  may annotate the artifact or request a degradation, never mutate pixels.
* :attr:`ExtensionPoint.WEBHOOK_ACTION` — a fire-and-forget reaction to a
  platform event (book ready, shot accepted); side-effecting, no return value.

A hook's :class:`HookKind` says whether it *transforms* (returns a replacement
payload), *observes* (returns nothing), or *produces* (returns a fresh value).
The dispatcher uses this to compose results deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ExtensionPoint(StrEnum):
    """The closed set of seams a plugin may attach to."""

    INGEST_FILTER = "ingest.filter"
    CUSTOM_AGENT = "agent.custom"
    RENDER_POSTPROCESS = "render.postprocess"
    WEBHOOK_ACTION = "webhook.action"


class HookKind(StrEnum):
    """How a hook's return value is composed by the dispatcher.

    * ``TRANSFORM`` — returns a replacement payload; the dispatcher threads the
      output of one hook into the input of the next (a pipeline / fold).
    * ``OBSERVE`` — returns nothing; called for side effect (e.g. webhooks).
    * ``PRODUCE`` — returns a fresh value collected alongside others (e.g. a
      custom agent's response, a render annotation).
    """

    TRANSFORM = "transform"
    OBSERVE = "observe"
    PRODUCE = "produce"


#: The kind each extension point uses — fixes how its results compose.
EXTENSION_POINT_KIND: dict[ExtensionPoint, HookKind] = {
    ExtensionPoint.INGEST_FILTER: HookKind.TRANSFORM,
    ExtensionPoint.CUSTOM_AGENT: HookKind.PRODUCE,
    ExtensionPoint.RENDER_POSTPROCESS: HookKind.PRODUCE,
    ExtensionPoint.WEBHOOK_ACTION: HookKind.OBSERVE,
}

#: The minimum capabilities a hook at each point *typically* needs. This is
#: advisory metadata surfaced to reviewers/installers — it is NOT enforced as a
#: floor (a hook may legitimately need less), but a manifest that declares a
#: hook while requesting none of its suggested capabilities is flagged in review.
SUGGESTED_CAPABILITIES: dict[ExtensionPoint, tuple[str, ...]] = {
    ExtensionPoint.INGEST_FILTER: ("book.read",),
    ExtensionPoint.CUSTOM_AGENT: ("canon.query", "log.write"),
    ExtensionPoint.RENDER_POSTPROCESS: ("render.read", "render.annotate"),
    ExtensionPoint.WEBHOOK_ACTION: ("net.fetch",),
}


@dataclass(frozen=True, slots=True)
class HookSpec:
    """A single hook declaration inside a manifest.

    ``point`` is the seam; ``entrypoint`` names the attribute in the plugin
    module the runtime will call (a top-level callable); ``priority`` orders
    hooks at the same point (lower runs first); ``id`` is a manifest-local
    handle so a host can target/disable one hook of a multi-hook plugin.
    """

    id: str
    point: ExtensionPoint
    entrypoint: str
    priority: int = 100
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> HookKind:
        return EXTENSION_POINT_KIND[self.point]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "point": self.point.value,
            "entrypoint": self.entrypoint,
            "priority": self.priority,
            "description": self.description,
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HookSpec:
        from app.platform.plugins.errors import PluginValidationError

        if not isinstance(data, dict):
            raise PluginValidationError("hook must be an object")
        try:
            point = ExtensionPoint(data["point"])
        except (KeyError, ValueError) as exc:
            raise PluginValidationError(f"invalid extension point: {data.get('point')!r}") from exc
        hook_id = data.get("id")
        entry = data.get("entrypoint")
        if not isinstance(hook_id, str) or not hook_id:
            raise PluginValidationError("hook.id must be a non-empty string")
        if not isinstance(entry, str) or not entry.isidentifier():
            raise PluginValidationError(f"hook.entrypoint must be an identifier: {entry!r}")
        priority = data.get("priority", 100)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise PluginValidationError("hook.priority must be an int")
        config = data.get("config", {})
        if not isinstance(config, dict):
            raise PluginValidationError("hook.config must be an object")
        return cls(
            id=hook_id,
            point=point,
            entrypoint=entry,
            priority=priority,
            description=str(data.get("description", "")),
            config=config,
        )


__all__ = [
    "EXTENSION_POINT_KIND",
    "SUGGESTED_CAPABILITIES",
    "ExtensionPoint",
    "HookKind",
    "HookSpec",
]
