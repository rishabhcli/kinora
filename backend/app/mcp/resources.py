"""MCP resources over the canon — read-only views + subscriptions (§8 / §8.3).

The §8.3 *tools* are verbs (query / assert / render). MCP *resources* are nouns:
addressable, subscribable documents a client can list, read, and watch. The
canon is exactly the kind of state worth exposing as resources — the frontend
canon editor (§3, "the memory graph rendered inspectable") wants to *read* the
current canon and be *told when it changes* without polling. This module maps a
slice of the canon onto resource URIs, reads them through ``MemoryTools`` (the
single execution path — never a second data path), and tracks which client is
watching which resource so a write can fan out a ``resources/updated``
notification.

URI scheme — ``kinora://`` — with a small, deliberate set of templates:

    kinora://canon/{book_id}                 canon.view  (active facts + branches + audit tail)
    kinora://canon/{book_id}/vault           canon.vault (the inspectable markdown document)
    kinora://canon/{book_id}/audit           canon.audit (the hash-chained audit log)
    kinora://canon/{book_id}/branch/{name}   canon.facts_as_of on a branch
    kinora://prefs/{user_id}                 prefs.get   (a director's learned style)

Reads delegate to the corresponding tool, so a resource read is *the same*
authorization-and-validation path as a tool call. Subscriptions are tracked
per client; :meth:`ResourceProvider.resources_touched_by` maps a completed write
tool call to the resource URIs it may have changed, which the server turns into
notifications. Nothing here renders or spends — resources are read views.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.mcp.errors import InvalidParamsError, NotFoundError

#: The Kinora resource URI scheme.
SCHEME = "kinora"


class _Dispatcher(Protocol):
    """The single method a resource read needs (``MemoryTools.dispatch``)."""

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ResourceTemplate:
    """A parameterised resource URI + the tool that materialises it.

    ``pattern`` is a regex over the path with named groups; ``tool`` is the §8.3
    tool whose result *is* the resource body; ``arg_map`` maps a path group onto
    the tool's argument name (plus any constant args).
    """

    template: str
    pattern: re.Pattern[str]
    tool: str
    arg_map: dict[str, str]
    const_args: dict[str, Any]
    title: str
    description: str
    mime_type: str = "application/json"


def _tpl(
    template: str,
    regex: str,
    tool: str,
    arg_map: dict[str, str],
    title: str,
    description: str,
    *,
    const_args: dict[str, Any] | None = None,
    mime_type: str = "application/json",
) -> ResourceTemplate:
    return ResourceTemplate(
        template=template,
        pattern=re.compile(regex),
        tool=tool,
        arg_map=arg_map,
        const_args=const_args or {},
        title=title,
        description=description,
        mime_type=mime_type,
    )


#: The canon resource templates, in list order.
RESOURCE_TEMPLATES: tuple[ResourceTemplate, ...] = (
    _tpl(
        "kinora://canon/{book_id}",
        r"^kinora://canon/(?P<book_id>[^/]+)$",
        "canon.view",
        {"book_id": "book_id"},
        "Canon (current)",
        "Active continuity facts at the latest beat + branch registry + audit tail.",
    ),
    _tpl(
        "kinora://canon/{book_id}/vault",
        r"^kinora://canon/(?P<book_id>[^/]+)/vault$",
        "canon.vault",
        {"book_id": "book_id"},
        "Canon vault (markdown)",
        "The full bitemporal canon rendered to inspectable markdown.",
        mime_type="text/markdown",
    ),
    _tpl(
        "kinora://canon/{book_id}/audit",
        r"^kinora://canon/(?P<book_id>[^/]+)/audit$",
        "canon.audit",
        {"book_id": "book_id"},
        "Canon audit log",
        "The append-only, hash-chained canon audit log (tamper-evident).",
    ),
    _tpl(
        "kinora://canon/{book_id}/branch/{branch}",
        r"^kinora://canon/(?P<book_id>[^/]+)/branch/(?P<branch>[^/]+)$",
        "canon.facts_as_of",
        {"book_id": "book_id", "branch": "branch"},
        "Canon branch (current facts)",
        "Active facts on a named branch, at the latest beat, current belief.",
        const_args={"beat": 2_147_483_647},
    ),
    _tpl(
        "kinora://prefs/{user_id}",
        r"^kinora://prefs/(?P<user_id>[^/]+)$",
        "prefs.get",
        {"user_id": "user_id"},
        "Director preferences",
        "A reader's learned directing-style priors (pacing / palette / composition).",
    ),
)


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    """A concrete resource URI resolved to its backing tool call."""

    uri: str
    template: ResourceTemplate
    arguments: dict[str, Any]

    @property
    def tool(self) -> str:
        return self.template.tool

    @property
    def book_id(self) -> str | None:
        return self.arguments.get("book_id")


def resolve_uri(uri: str) -> ResolvedResource:
    """Resolve a ``kinora://`` URI to its template + tool arguments.

    Raises:
        InvalidParamsError: the URI does not match any known template.
    """
    for tpl in RESOURCE_TEMPLATES:
        match = tpl.pattern.match(uri)
        if match is None:
            continue
        args: dict[str, Any] = dict(tpl.const_args)
        for group, value in match.groupdict().items():
            arg_name = tpl.arg_map.get(group, group)
            args[arg_name] = value
        return ResolvedResource(uri=uri, template=tpl, arguments=args)
    raise InvalidParamsError(f"unknown resource URI: {uri!r}", data={"uri": uri})


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    """A listable resource entry (the SDK's ``Resource`` in Kinora terms)."""

    uri: str
    name: str
    title: str
    description: str
    mime_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True, slots=True)
class ResourceContents:
    """The body of a read resource (text + structured content)."""

    uri: str
    mime_type: str
    text: str
    structured: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"uri": self.uri, "mimeType": self.mime_type, "text": self.text}


class ResourceProvider:
    """Lists and reads canon resources by delegating to ``MemoryTools``.

    A resource read is a tool call under the hood, so it inherits the tool's
    validation and (when wired) authorization. ``list_for_book`` enumerates the
    concrete per-book resources the editor can subscribe to.
    """

    def __init__(self, tools: _Dispatcher) -> None:
        self._tools = tools

    def templates(self) -> list[dict[str, Any]]:
        """The advertised resource *templates* (the URI grammar)."""
        return [
            {
                "uriTemplate": t.template,
                "name": t.tool,
                "title": t.title,
                "description": t.description,
                "mimeType": t.mime_type,
            }
            for t in RESOURCE_TEMPLATES
        ]

    def list_for_book(self, book_id: str, *, branches: Iterable[str] = ("main",)) -> list[
        ResourceDescriptor
    ]:
        """Concrete resources for one book (what a client lists then subscribes to)."""
        out: list[ResourceDescriptor] = [
            ResourceDescriptor(
                uri=f"kinora://canon/{book_id}",
                name="canon.view",
                title="Canon (current)",
                description=RESOURCE_TEMPLATES[0].description,
                mime_type="application/json",
            ),
            ResourceDescriptor(
                uri=f"kinora://canon/{book_id}/vault",
                name="canon.vault",
                title="Canon vault (markdown)",
                description=RESOURCE_TEMPLATES[1].description,
                mime_type="text/markdown",
            ),
            ResourceDescriptor(
                uri=f"kinora://canon/{book_id}/audit",
                name="canon.audit",
                title="Canon audit log",
                description=RESOURCE_TEMPLATES[2].description,
                mime_type="application/json",
            ),
        ]
        for branch in branches:
            out.append(
                ResourceDescriptor(
                    uri=f"kinora://canon/{book_id}/branch/{branch}",
                    name="canon.facts_as_of",
                    title=f"Canon branch: {branch}",
                    description=RESOURCE_TEMPLATES[3].description,
                    mime_type="application/json",
                )
            )
        return out

    async def read(self, uri: str) -> ResourceContents:
        """Read a resource: resolve the URI, run its tool, serialize the body.

        Raises:
            InvalidParamsError: malformed URI.
            NotFoundError: the backing tool reported the object is absent.
        """
        resolved = resolve_uri(uri)
        result = await self._tools.dispatch(resolved.tool, resolved.arguments)
        structured = result.model_dump(mode="json")
        if _looks_absent(structured):
            raise NotFoundError(
                f"resource not found: {uri!r}", data={"uri": uri, "tool": resolved.tool}
            )
        import json

        is_markdown = resolved.template.mime_type == "text/markdown"
        markdown = structured.get("markdown")
        if is_markdown and isinstance(markdown, str):
            text = markdown
        else:
            text = json.dumps(structured, default=str)
        return ResourceContents(
            uri=uri,
            mime_type=resolved.template.mime_type,
            text=text,
            structured=structured,
        )

    @staticmethod
    def resources_touched_by(tool: str, arguments: dict[str, Any]) -> list[str]:
        """Map a completed write call to the resource URIs it may have changed.

        Used by the server to fan out change notifications: e.g. any canon write
        for ``book_id`` invalidates that book's ``canon`` / ``vault`` / ``audit``
        views (and the touched branch), and a ``prefs.upsert`` invalidates the
        user's prefs resource. Reads return ``[]``.
        """
        touched: list[str] = []
        book_id = arguments.get("book_id")
        if tool.startswith("canon.") and _is_canon_write(tool) and book_id:
            touched.extend(
                [
                    f"kinora://canon/{book_id}",
                    f"kinora://canon/{book_id}/vault",
                    f"kinora://canon/{book_id}/audit",
                ]
            )
            branch = arguments.get("branch")
            if branch:
                touched.append(f"kinora://canon/{book_id}/branch/{branch}")
        elif tool == "prefs.upsert":
            user_id = arguments.get("user_id")
            if user_id:
                touched.append(f"kinora://prefs/{user_id}")
        return touched


_CANON_WRITE_TOOLS = frozenset(
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
    }
)


def _is_canon_write(tool: str) -> bool:
    return tool in _CANON_WRITE_TOOLS


def _looks_absent(structured: dict[str, Any]) -> bool:
    """A read tool with a ``found`` flag set False means the object is absent."""
    return structured.get("found") is False


@dataclass(slots=True)
class SubscriptionRegistry:
    """Tracks which client subscribes to which resource URIs.

    A subscription is ``(client_id, uri)``. Writes are translated to a set of
    touched URIs; :meth:`subscribers_for` returns the clients to notify. The
    registry is in-memory and per-process — the canon MCP runs in one process
    per the compose model — and is intentionally simple: add, remove, fan-out.
    """

    _by_uri: dict[str, set[str]] = field(default_factory=dict)
    _by_client: dict[str, set[str]] = field(default_factory=dict)

    def subscribe(self, client_id: str, uri: str) -> None:
        """Record that ``client_id`` watches ``uri`` (validates the URI shape)."""
        resolve_uri(uri)  # raises InvalidParamsError on a bad URI
        self._by_uri.setdefault(uri, set()).add(client_id)
        self._by_client.setdefault(client_id, set()).add(uri)

    def unsubscribe(self, client_id: str, uri: str) -> None:
        """Stop watching ``uri`` for ``client_id`` (idempotent)."""
        watchers = self._by_uri.get(uri)
        if watchers is not None:
            watchers.discard(client_id)
            if not watchers:
                del self._by_uri[uri]
        owned = self._by_client.get(client_id)
        if owned is not None:
            owned.discard(uri)
            if not owned:
                del self._by_client[client_id]

    def drop_client(self, client_id: str) -> None:
        """Remove every subscription for a disconnecting client."""
        for uri in list(self._by_client.get(client_id, ())):
            self.unsubscribe(client_id, uri)

    def subscriptions_for(self, client_id: str) -> set[str]:
        """The URIs ``client_id`` currently watches."""
        return set(self._by_client.get(client_id, ()))

    def subscribers_for(self, uri: str) -> set[str]:
        """The clients watching ``uri``."""
        return set(self._by_uri.get(uri, ()))

    def fan_out(self, touched_uris: Iterable[str]) -> dict[str, set[str]]:
        """Given changed URIs, return ``{client_id: {uris it watches}}`` to notify."""
        notifications: dict[str, set[str]] = {}
        for uri in touched_uris:
            for client_id in self.subscribers_for(uri):
                notifications.setdefault(client_id, set()).add(uri)
        return notifications

    @property
    def total_subscriptions(self) -> int:
        return sum(len(v) for v in self._by_uri.values())


__all__ = [
    "RESOURCE_TEMPLATES",
    "SCHEME",
    "ResolvedResource",
    "ResourceContents",
    "ResourceDescriptor",
    "ResourceProvider",
    "ResourceTemplate",
    "SubscriptionRegistry",
    "resolve_uri",
]
