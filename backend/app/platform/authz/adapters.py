"""Adapters — fold the scattered legacy checks into the plane unchanged.

The whole point of the plane is *unification without behaviour change*. These
adapters wrap each existing subsystem as a plane :class:`AuthorizationEngine`
that **delegates to the original check**, so the legacy decision is preserved
exactly while becoming expressible, cacheable, and auditable through the unified
``check``. Nothing in the original modules is removed; the adapters are the only
new coupling, and they only *read* through the existing public surfaces.

Four adapters, one per scattered check:

* :class:`AuthRbacAdapter` — wraps :mod:`app.auth.rbac`'s ``Principal``
  capability check (``has_capability`` / wildcard matching). A subject carrying
  ``permissions`` in its attributes is checked against the action exactly as the
  legacy ``Principal.has_permission`` would. Pure / synchronous.
* :class:`WorkspaceAuthzAdapter` — wraps :class:`app.workspaces.authz.AuthorizationService`'s
  DB-backed ``can(user, action, resource)``. Async (it reads the workspace
  membership / share tables). The plane action is mapped to the workspaces
  :class:`~app.workspaces.roles.Action`; an unmappable action abstains.
* :class:`McpBookScopeAdapter` — wraps :class:`app.mcp.authz.BookScopedAuthorizer`'s
  "reject unknown book_id" check. Async (it calls the book-existence lookup).
  Emits DENY for a forged/unknown book, ABSTAIN otherwise — preserving the exact
  pass-through-unless-unknown behaviour.
* :class:`ModerationPolicyAdapter` — wraps :func:`app.moderation.policy.evaluate`'s
  content disposition. A BLOCK verdict becomes a plane DENY (with the driving
  labels as the reason); FLAG/ALLOW abstain (the plane gates *access*, not the
  flag-for-review queue, which the moderation gate still owns). Pure.

Each adapter is independently testable with a fake/stub of the wrapped surface,
so the "behaviour-preserving" claim is verified, not asserted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.platform.authz.engine import AuthorizationEngine, SyncEngine
from app.platform.authz.model import AuthorizationRequest, EngineResult
from app.platform.authz.rbac import permission_matches

# --------------------------------------------------------------------------- #
# 1. auth RBAC adapter (pure)
# --------------------------------------------------------------------------- #


class AuthRbacAdapter(SyncEngine):
    """Fold :mod:`app.auth.rbac`'s capability check into the plane (pure).

    The legacy check is ``has_capability(held, required)`` with ``*`` / ``ns:*``
    wildcards. The subject carries its held capabilities in
    ``attributes['permissions']`` (an API key's scopes, or a role-expanded set).
    This adapter reproduces that check verbatim — same matching function — so a
    capability means the same thing in the plane as in the auth dependency.

    Emits ALLOW when a held capability grants the action; abstains otherwise
    (never a hard DENY — absence of a capability is "no opinion", letting a
    relationship/ABAC grant still allow under deny-overrides).
    """

    name = "auth-rbac"

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        held = request.subject.attr("permissions")
        if not held:
            return EngineResult.abstain(self.name, "subject carries no capabilities")
        held_set = {str(p) for p in held}
        if permission_matches(held_set, request.action):
            return EngineResult.allow(
                self.name,
                f"capability grants '{request.action}'",
                rule="auth-rbac:capability",
            )
        return EngineResult.abstain(
            self.name, f"no capability grants '{request.action}'"
        )


# --------------------------------------------------------------------------- #
# 2. workspaces can() adapter (async, DB-backed)
# --------------------------------------------------------------------------- #

#: The action-string → workspaces ``Action`` value map. Plane actions are
#: ``ns:verb``; the workspaces model uses the bare verb. We map by the verb part
#: so ``book:edit`` and the bare ``edit`` both resolve to ``Action.EDIT``.
_WORKSPACE_VERBS = {
    "view": "view",
    "read": "view",
    "comment": "comment",
    "edit": "edit",
    "render": "render",
    "download": "download",
    "share": "share",
    "manage_members": "manage_members",
    "manage_settings": "manage_settings",
    "manage_collections": "manage_collections",
    "transfer_ownership": "transfer_ownership",
    "delete": "delete",
    "view_activity": "view_activity",
}


class WorkspaceAuthzAdapter(AuthorizationEngine):
    """Fold :class:`app.workspaces.authz.AuthorizationService.can` into the plane.

    Constructed with a ``service_factory`` that yields an ``AuthorizationService``
    bound to a request session (so the adapter does not own a DB session). The
    plane action is mapped to the workspaces ``Action`` and resolved against the
    same ``ResourceRef`` the legacy code uses — so the workspace/share/org grant
    resolution is identical. Emits ALLOW on grant, ABSTAIN on no-grant (so the
    workspace's *absence* of a grant doesn't override an explicit grant elsewhere,
    matching the legacy "most-permissive path wins" semantics).
    """

    name = "workspaces"

    def __init__(
        self,
        service_factory: Callable[[], Any],
        *,
        resource_types: frozenset[str] = frozenset({"book", "workspace", "collection"}),
    ) -> None:
        self._service_factory = service_factory
        self._resource_types = resource_types

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:  # pragma: no cover
        raise TypeError("WorkspaceAuthzAdapter is async-only; use aevaluate()")

    async def aevaluate(self, request: AuthorizationRequest) -> EngineResult:
        if request.resource.type not in self._resource_types:
            return EngineResult.abstain(
                self.name, f"not a workspace resource: {request.resource.type}"
            )
        verb = self._verb(request.action)
        if verb is None:
            return EngineResult.abstain(self.name, f"unmapped action '{request.action}'")

        from app.workspaces.authz import ResourceRef
        from app.workspaces.roles import Action, ResourceType

        try:
            action = Action(verb)
            resource_type = ResourceType(request.resource.type)
        except ValueError:
            return EngineResult.abstain(self.name, f"unknown verb/type for '{request.action}'")

        ref = ResourceRef(type=resource_type, id=request.resource.id)
        service = self._service_factory()
        decision = await service.decide(request.subject.id, action, ref)
        if decision.allowed:
            return EngineResult.allow(
                self.name, decision.reason, rule="workspaces:grant"
            )
        return EngineResult.abstain(self.name, decision.reason)

    @staticmethod
    def _verb(action: str) -> str | None:
        _, _, verb = action.partition(":")
        verb = verb or action
        return _WORKSPACE_VERBS.get(verb)


# --------------------------------------------------------------------------- #
# 3. MCP book-scope adapter (async)
# --------------------------------------------------------------------------- #

BookLookup = Callable[[str], Awaitable[bool]]


class McpBookScopeAdapter(AuthorizationEngine):
    """Fold :class:`app.mcp.authz.BookScopedAuthorizer`'s check into the plane.

    The legacy check rejects an MCP tool call whose ``book_id`` does not exist;
    a call with no book_id passes through. This adapter reproduces that exactly:
    a ``book`` resource whose id does not exist → DENY; a non-book resource (or a
    type-level ``book:*``) → ABSTAIN (pass-through). The book-existence lookup is
    injected, so the adapter is unit-testable with a stub.
    """

    name = "mcp-book-scope"

    def __init__(self, *, book_exists: BookLookup) -> None:
        self._book_exists = book_exists

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:  # pragma: no cover
        raise TypeError("McpBookScopeAdapter is async-only; use aevaluate()")

    async def aevaluate(self, request: AuthorizationRequest) -> EngineResult:
        if request.resource.type != "book":
            return EngineResult.abstain(self.name, "not a book resource")
        book_id = request.resource.id
        if not book_id or book_id == "*":
            return EngineResult.abstain(self.name, "no concrete book_id")
        if await self._book_exists(book_id):
            return EngineResult.abstain(self.name, f"book {book_id} exists")
        return EngineResult.deny(
            self.name, f"unknown book_id: {book_id}", rule="mcp:book-existence"
        )


# --------------------------------------------------------------------------- #
# 4. moderation policy adapter (pure)
# --------------------------------------------------------------------------- #


class ModerationPolicyAdapter(SyncEngine):
    """Fold :func:`app.moderation.policy.evaluate`'s BLOCK verdict into the plane.

    Moderation is *content* authorization: a BLOCK verdict forbids surfacing the
    content. The adapter runs the deterministic policy over a
    :class:`~app.moderation.contracts.ClassificationResult` carried in
    ``context['classification']`` and, on a BLOCK, emits a plane DENY (with the
    driving labels as the reason). FLAG/ALLOW abstain — the plane gates *access*;
    the flag-for-review queue stays the moderation gate's job, unchanged.

    A request with no classification in context abstains (this engine only speaks
    for content-bearing actions like ``content:publish``).
    """

    name = "moderation"

    def __init__(self, *, action_prefixes: frozenset[str] = frozenset({"content"})) -> None:
        self._prefixes = action_prefixes

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        prefix = request.action.split(":", 1)[0]
        if prefix not in self._prefixes:
            return EngineResult.abstain(self.name, f"not a content action: {request.action}")
        classification = request.context.attr("classification")
        if classification is None:
            return EngineResult.abstain(self.name, "no classification in context")

        from app.moderation.policy import evaluate as moderation_evaluate
        from app.moderation.taxonomy import Disposition

        policy = request.context.attr("moderation_policy")
        verdict = (
            moderation_evaluate(classification, policy=policy)
            if policy is not None
            else moderation_evaluate(classification)
        )
        if verdict.decision is Disposition.BLOCK:
            return EngineResult.deny(
                self.name,
                f"content blocked: {verdict.reason}",
                rule="moderation:block",
            )
        return EngineResult.abstain(
            self.name, f"content disposition {verdict.decision.value}"
        )


__all__ = [
    "AuthRbacAdapter",
    "McpBookScopeAdapter",
    "ModerationPolicyAdapter",
    "WorkspaceAuthzAdapter",
]
