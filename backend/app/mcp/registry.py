"""The tool catalog — versioned, scoped metadata over the §8.3 tool surface.

``tools.py`` owns *what each tool does* (the handlers + ``TOOL_DEFS`` + the
input models) and is the single execution path. This module owns *what the
protocol layer needs to know about each tool* and never re-implements a tool:

* **Output model** — every handler returns a typed pydantic result, but
  ``ToolDef`` only records the *input* model. The catalog resolves each tool's
  declared output model (by reading the handler's return annotation), so the
  server can advertise an ``outputSchema`` and validate the response.
* **Version** — a ``ToolVersion`` (major.minor) per tool so a client can pin a
  version and the server can reject an unknown one (forward-compatible API
  evolution; kinora.md §8.3 calls the surface "small and deliberate", which only
  stays true if changes are versioned rather than silent).
* **Scope tags** — ``read`` vs ``write``, and whether the tool is ``book_scoped``
  (carries a ``book_id``) — so per-client scoping (``identity.py``) can allow a
  read-only client without enumerating 28 tool names, and the resource layer
  knows which tools mutate canon (and therefore fire change notifications).

The catalog is built once from ``TOOL_DEFS`` and is the single source of this
metadata for ``server.py``, ``validation.py``, ``capabilities.py``,
``resources.py`` and ``client.py``.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from app.mcp.tools import TOOL_DEFS, MemoryTools, ToolDef


@dataclass(frozen=True, slots=True, order=True)
class ToolVersion:
    """A semantic-ish ``major.minor`` version for a single tool.

    Tools evolve independently, so each carries its own version rather than a
    server-wide number. ``major`` bumps on a breaking input/output change;
    ``minor`` bumps on a backward-compatible addition.
    """

    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, text: str) -> ToolVersion:
        """Parse ``"1.2"`` / ``"1"`` into a :class:`ToolVersion`."""
        parts = text.strip().split(".")
        if not parts or not parts[0]:
            raise ValueError(f"invalid tool version: {text!r}")
        try:
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid tool version: {text!r}") from exc
        return cls(major=major, minor=minor)

    def is_compatible_with(self, requested: ToolVersion) -> bool:
        """True when *this* (the served version) satisfies a ``requested`` pin.

        Same major, and the served minor is at least the requested minor — the
        usual "the server may add minor features the client doesn't know about"
        compatibility rule.
        """
        return self.major == requested.major and self.minor >= requested.minor


class Scope(StrEnum):
    """A capability a client may be granted; tools are tagged with what they need."""

    #: Reads canon / episodic / budget / prefs — no mutation.
    READ = "read"
    #: Writes canon / episodic / prefs (mutation).
    WRITE = "write"
    #: Enqueues a render / reserves budget — a control-plane spend action (§12).
    RENDER = "render"


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """Everything the protocol layer needs about one tool, derived once.

    Wraps the underlying :class:`~app.mcp.tools.ToolDef` (never replacing it) and
    adds version + scope + the resolved output model.
    """

    defn: ToolDef
    version: ToolVersion
    scopes: frozenset[Scope]
    book_scoped: bool
    output_model: type[BaseModel] | None

    @property
    def name(self) -> str:
        return self.defn.name

    @property
    def description(self) -> str:
        return self.defn.description

    @property
    def input_model(self) -> type[BaseModel]:
        return self.defn.input_model

    @property
    def is_write(self) -> bool:
        """True when the tool mutates canon / episodic / prefs state."""
        return Scope.WRITE in self.scopes

    @property
    def is_render(self) -> bool:
        """True when the tool can enqueue a render / reserve budget (§12)."""
        return Scope.RENDER in self.scopes

    def input_schema(self) -> dict[str, Any]:
        """The advertised JSON Schema for the tool's arguments."""
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any] | None:
        """The advertised JSON Schema for the tool's result (``None`` if unknown)."""
        if self.output_model is None:
            return None
        return self.output_model.model_json_schema()


# --- scope + version policy --------------------------------------------------
#
# Default scope = READ. The catalog overrides the few tools that mutate state or
# spend the control plane. Keeping the override list explicit (rather than
# guessing from the name) means a new tool is treated as a write until it is
# deliberately classified — fail safe for the per-client scoping.

_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "canon.upsert_entity",
        "canon.assert_state",
        "canon.retire_state",
        "canon.assert_fact",
        "canon.correct_fact",
        "canon.retire_fact",
        "canon.fork",
        "canon.merge",
        "canon.compact",
        "episodic.log",
        "prefs.upsert",
    }
)

#: Tools on the control plane: they enqueue a render or reserve metered seconds.
_RENDER_TOOLS: frozenset[str] = frozenset({"shot.render", "budget.reserve"})

#: Per-tool version pins. Absent => v1.0. Bump here when a tool's contract changes.
_VERSIONS: dict[str, ToolVersion] = {}


def _scopes_for(name: str) -> frozenset[Scope]:
    scopes: set[Scope] = set()
    if name in _RENDER_TOOLS:
        scopes.add(Scope.RENDER)
    if name in _WRITE_TOOLS:
        scopes.add(Scope.WRITE)
    if not scopes:
        scopes.add(Scope.READ)
    return frozenset(scopes)


def _resolve_output_model(defn: ToolDef) -> type[BaseModel] | None:
    """Read the handler's return annotation to find its typed output model.

    The handler is ``MemoryTools.<defn.handler>``; its return annotation is the
    output pydantic model (``schemas.*Output`` or a memory ``contracts.*`` type).
    Resolved via ``typing.get_type_hints`` so string ("from __future__")
    annotations are evaluated against the right module globals.
    """
    handler = getattr(MemoryTools, defn.handler, None)
    if handler is None:  # pragma: no cover - defensive
        return None
    try:
        hints = typing.get_type_hints(handler)
    except Exception:  # pragma: no cover - defensive
        # Fall back to the raw annotation if forward refs can't be resolved.
        sig = inspect.signature(handler)
        ann = sig.return_annotation
        return ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None
    ret = hints.get("return")
    if isinstance(ret, type) and issubclass(ret, BaseModel):
        return ret
    return None


def _book_scoped(defn: ToolDef) -> bool:
    """True when the tool's input model carries a ``book_id`` field."""
    return "book_id" in defn.input_model.model_fields


def _build_meta(defn: ToolDef) -> ToolMeta:
    return ToolMeta(
        defn=defn,
        version=_VERSIONS.get(defn.name, ToolVersion(1, 0)),
        scopes=_scopes_for(defn.name),
        book_scoped=_book_scoped(defn),
        output_model=_resolve_output_model(defn),
    )


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """The immutable, versioned, scoped view of the §8.3 tool surface.

    Built from ``TOOL_DEFS`` so it is always in lock-step with the single
    execution path — adding a tool to ``tools.py`` automatically registers it
    here (as a read tool at v1.0 until classified). Look-ups are O(1).
    """

    metas: tuple[ToolMeta, ...]
    _by_name: dict[str, ToolMeta] = field(repr=False)

    @classmethod
    def from_tool_defs(cls, defs: list[ToolDef] = TOOL_DEFS) -> ToolCatalog:
        metas = tuple(_build_meta(d) for d in defs)
        return cls(metas=metas, _by_name={m.name: m for m in metas})

    def names(self) -> list[str]:
        """All registered tool names, in catalog order."""
        return [m.name for m in self.metas]

    def get(self, name: str) -> ToolMeta | None:
        """The metadata for ``name`` (``None`` when unknown)."""
        return self._by_name.get(name)

    def require(self, name: str) -> ToolMeta:
        """The metadata for ``name`` or raise :class:`KeyError`."""
        meta = self._by_name.get(name)
        if meta is None:
            raise KeyError(name)
        return meta

    def with_scope(self, scope: Scope) -> list[ToolMeta]:
        """All tools requiring ``scope``."""
        return [m for m in self.metas if scope in m.scopes]

    def write_tools(self) -> list[str]:
        """Names of every tool that mutates state."""
        return [m.name for m in self.metas if m.is_write]

    def book_scoped_tools(self) -> list[str]:
        """Names of every tool carrying a ``book_id``."""
        return [m.name for m in self.metas if m.book_scoped]

    def resolve_version(self, name: str, requested: str | None) -> ToolMeta:
        """Resolve a tool, optionally validating a pinned version.

        Raises :class:`KeyError` when the tool is unknown and
        :class:`ValueError` when ``requested`` is set but incompatible with the
        served version. ``server.py`` maps these onto typed MCP errors.
        """
        meta = self.require(name)
        if requested is None:
            return meta
        want = ToolVersion.parse(requested)
        if not meta.version.is_compatible_with(want):
            raise ValueError(
                f"tool {name!r} is served at v{meta.version}; "
                f"incompatible with requested v{want}"
            )
        return meta


@lru_cache(maxsize=1)
def default_catalog() -> ToolCatalog:
    """The process-wide catalog built from the live ``TOOL_DEFS`` (cached)."""
    return ToolCatalog.from_tool_defs()


__all__ = [
    "Scope",
    "ToolCatalog",
    "ToolMeta",
    "ToolVersion",
    "default_catalog",
]
