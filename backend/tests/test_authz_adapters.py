"""Tests that the adapters fold the legacy checks in *without changing behaviour*.

Each adapter is checked against a stub/fake of the wrapped surface, plus a
parity assertion against the original legacy function where it is pure.
"""

from __future__ import annotations

import pytest

from app.platform.authz.adapters import (
    AuthRbacAdapter,
    McpBookScopeAdapter,
    ModerationPolicyAdapter,
    WorkspaceAuthzAdapter,
)
from app.platform.authz.model import (
    AuthorizationRequest,
    Context,
    Effect,
    Resource,
    Subject,
)


def _req(action, *, subject_attrs=None, resource=None, ctx=None) -> AuthorizationRequest:
    return AuthorizationRequest(
        subject=Subject(type="user", id="alice", attributes=subject_attrs or {}),
        action=action,
        resource=resource or Resource.of("book", "1"),
        context=Context(attributes=ctx or {}),
    )


# -- 1. auth RBAC adapter (pure; parity with has_capability) ----------------- #


def test_auth_rbac_adapter_parity_with_legacy() -> None:
    from app.auth.rbac import Principal, has_capability

    adapter = AuthRbacAdapter()
    held = ["book:read", "render:*"]
    principal = Principal(user_id="alice", permissions=frozenset(held))
    for action in ("book:read", "render:enqueue", "book:write", "admin:rbac"):
        legacy = principal.has_permission(action)
        result = adapter.evaluate(_req(action, subject_attrs={"permissions": held}))
        plane_allows = result.effect is Effect.ALLOW
        assert plane_allows == legacy, action
        # sanity: the legacy helper agrees too
        assert has_capability(held, action) == legacy


def test_auth_rbac_adapter_abstains_without_capabilities() -> None:
    adapter = AuthRbacAdapter()
    assert adapter.evaluate(_req("book:read")).effect is Effect.ABSTAIN


def test_auth_rbac_adapter_never_denies() -> None:
    # absence of a capability is "no opinion", not a hard deny
    adapter = AuthRbacAdapter()
    res = adapter.evaluate(_req("admin:rbac", subject_attrs={"permissions": ["book:read"]}))
    assert res.effect is Effect.ABSTAIN


# -- 2. workspaces adapter (async; delegates to AuthorizationService) -------- #


class FakeWorkspaceService:
    """A stand-in for AuthorizationService.decide returning scripted decisions."""

    def __init__(self, allow: bool) -> None:
        self._allow = allow
        self.calls: list[tuple[str, str, str]] = []

    async def decide(self, user_id, action, ref):
        from app.workspaces.policy import Decision as WsDecision

        self.calls.append((user_id, str(action), ref.id))
        return WsDecision(
            allowed=self._allow,
            action=action,
            effective_role=None,
            reason="scripted",
        )


@pytest.mark.asyncio
async def test_workspace_adapter_allows_on_grant() -> None:
    svc = FakeWorkspaceService(allow=True)
    adapter = WorkspaceAuthzAdapter(lambda: svc)
    res = await adapter.aevaluate(_req("book:edit"))
    assert res.effect is Effect.ALLOW
    assert svc.calls == [("alice", "edit", "1")]  # mapped book:edit → Action.EDIT


@pytest.mark.asyncio
async def test_workspace_adapter_abstains_on_no_grant() -> None:
    svc = FakeWorkspaceService(allow=False)
    adapter = WorkspaceAuthzAdapter(lambda: svc)
    res = await adapter.aevaluate(_req("book:edit"))
    # a workspace *absence* of grant is abstain, not deny (so another path can allow)
    assert res.effect is Effect.ABSTAIN


@pytest.mark.asyncio
async def test_workspace_adapter_abstains_for_unknown_resource_type() -> None:
    svc = FakeWorkspaceService(allow=True)
    adapter = WorkspaceAuthzAdapter(lambda: svc)
    res = await adapter.aevaluate(_req("user:edit", resource=Resource.of("user", "x")))
    assert res.effect is Effect.ABSTAIN
    assert svc.calls == []  # never even consulted


@pytest.mark.asyncio
async def test_workspace_adapter_abstains_for_unmapped_action() -> None:
    svc = FakeWorkspaceService(allow=True)
    adapter = WorkspaceAuthzAdapter(lambda: svc)
    res = await adapter.aevaluate(_req("book:teleport"))
    assert res.effect is Effect.ABSTAIN


def test_workspace_adapter_sync_raises() -> None:
    adapter = WorkspaceAuthzAdapter(lambda: FakeWorkspaceService(True))
    with pytest.raises(TypeError):
        adapter.evaluate(_req("book:edit"))


# -- 3. MCP book-scope adapter (async; parity with BookScopedAuthorizer) ----- #


@pytest.mark.asyncio
async def test_mcp_adapter_denies_unknown_book() -> None:
    async def exists(book_id: str) -> bool:
        return book_id == "real"

    adapter = McpBookScopeAdapter(book_exists=exists)
    deny = await adapter.aevaluate(_req("book:query", resource=Resource.of("book", "forged")))
    assert deny.effect is Effect.DENY
    allow = await adapter.aevaluate(_req("book:query", resource=Resource.of("book", "real")))
    assert allow.effect is Effect.ABSTAIN  # exists → pass-through (abstain)


@pytest.mark.asyncio
async def test_mcp_adapter_parity_with_legacy_authorizer() -> None:
    from app.mcp.authz import BookScopedAuthorizer, MCPAuthorizationError

    async def exists(book_id: str) -> bool:
        return book_id == "real"

    legacy = BookScopedAuthorizer(book_exists=exists)
    adapter = McpBookScopeAdapter(book_exists=exists)

    # legacy: rejects unknown book_id, passes a known one
    with pytest.raises(MCPAuthorizationError):
        await legacy.authorize("canon.query", {"book_id": "forged"})
    await legacy.authorize("canon.query", {"book_id": "real"})  # no raise
    await legacy.authorize("canon.query", {})  # no book_id → pass

    # adapter mirrors: deny for forged, abstain for real
    assert (await adapter.aevaluate(
        _req("book:x", resource=Resource.of("book", "forged"))
    )).effect is Effect.DENY
    assert (await adapter.aevaluate(
        _req("book:x", resource=Resource.of("book", "real"))
    )).effect is Effect.ABSTAIN


@pytest.mark.asyncio
async def test_mcp_adapter_abstains_for_non_book_and_typelevel() -> None:
    async def exists(book_id: str) -> bool:
        return False

    adapter = McpBookScopeAdapter(book_exists=exists)
    assert (await adapter.aevaluate(
        _req("ws:x", resource=Resource.of("workspace", "1"))
    )).effect is Effect.ABSTAIN
    assert (await adapter.aevaluate(
        _req("book:x", resource=Resource.type_level("book"))
    )).effect is Effect.ABSTAIN


# -- 4. moderation adapter (pure; parity with policy.evaluate) --------------- #


def _classification(block: bool):
    from app.moderation.contracts import ClassificationResult, ContentLabel, Surface
    from app.moderation.taxonomy import ModerationCategory

    if block:
        labels = [ContentLabel.of(ModerationCategory.EXTREMISM, 0.95)]  # zero-tolerance
    else:
        labels = [ContentLabel.of(ModerationCategory.SAFE, 0.99)]
    return ClassificationResult(surface=Surface.CLIP, labels=labels, classifier="kw")


def test_moderation_adapter_denies_block_verdict() -> None:
    adapter = ModerationPolicyAdapter()
    res = adapter.evaluate(
        _req("content:publish", ctx={"classification": _classification(block=True)})
    )
    assert res.effect is Effect.DENY
    assert "blocked" in res.reasons[0].detail


def test_moderation_adapter_abstains_on_allow_verdict() -> None:
    adapter = ModerationPolicyAdapter()
    res = adapter.evaluate(
        _req("content:publish", ctx={"classification": _classification(block=False)})
    )
    assert res.effect is Effect.ABSTAIN


def test_moderation_adapter_abstains_without_classification() -> None:
    adapter = ModerationPolicyAdapter()
    assert adapter.evaluate(_req("content:publish")).effect is Effect.ABSTAIN


def test_moderation_adapter_ignores_non_content_actions() -> None:
    adapter = ModerationPolicyAdapter()
    res = adapter.evaluate(
        _req("book:read", ctx={"classification": _classification(block=True)})
    )
    assert res.effect is Effect.ABSTAIN  # only speaks for content actions


def test_moderation_adapter_parity_with_policy() -> None:
    from app.moderation.policy import evaluate as policy_evaluate
    from app.moderation.taxonomy import Disposition

    adapter = ModerationPolicyAdapter()
    for block in (True, False):
        cls = _classification(block)
        verdict = policy_evaluate(cls)
        res = adapter.evaluate(_req("content:publish", ctx={"classification": cls}))
        if verdict.decision is Disposition.BLOCK:
            assert res.effect is Effect.DENY
        else:
            assert res.effect is Effect.ABSTAIN
