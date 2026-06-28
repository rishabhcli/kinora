"""Pure-unit tests for the role lattice + the policy engine (no infra).

These cover the *decision* half of the authz model exhaustively: the lattice
ordering, the capability-superset invariant, the most-permissive-grant collapse,
and the allow/deny rules for every (role, action) pair.
"""

from __future__ import annotations

import pytest

from app.workspaces.policy import Decision, GrantSet, allowed_actions, decide
from app.workspaces.roles import (
    ROLE_CAPABILITIES,
    Action,
    Role,
    capabilities_for,
    max_role,
    minimum_role_for,
    role_allows,
    role_at_least,
    role_rank,
)

ALL_ROLES = [Role.OWNER, Role.EDITOR, Role.COMMENTER, Role.VIEWER]
LOWER_TO_HIGHER = [Role.VIEWER, Role.COMMENTER, Role.EDITOR, Role.OWNER]


# --------------------------------------------------------------------------- #
# Role lattice
# --------------------------------------------------------------------------- #


def test_rank_is_total_and_ordered() -> None:
    ranks = [role_rank(r) for r in LOWER_TO_HIGHER]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # totally ordered, no ties


def test_capability_superset_invariant() -> None:
    # Each higher role's capabilities are a strict superset of the next lower.
    for lower, higher in zip(LOWER_TO_HIGHER, LOWER_TO_HIGHER[1:], strict=False):
        assert capabilities_for(lower) < capabilities_for(higher)


def test_owner_has_every_action() -> None:
    assert capabilities_for(Role.OWNER) == frozenset(Action)


def test_viewer_can_only_view() -> None:
    assert capabilities_for(Role.VIEWER) == frozenset({Action.VIEW})


def test_commenter_can_comment_not_edit() -> None:
    caps = capabilities_for(Role.COMMENTER)
    assert Action.COMMENT in caps
    assert Action.VIEW in caps
    assert Action.EDIT not in caps
    assert Action.SHARE not in caps


def test_editor_can_edit_and_render_not_share() -> None:
    caps = capabilities_for(Role.EDITOR)
    assert {Action.EDIT, Action.RENDER, Action.DOWNLOAD} <= caps
    assert Action.SHARE not in caps
    assert Action.MANAGE_MEMBERS not in caps


@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_allows_matches_capability_table(role: Role) -> None:
    for action in Action:
        assert role_allows(role, action) == (action in ROLE_CAPABILITIES[role])


def test_role_allows_none_is_false() -> None:
    assert role_allows(None, Action.VIEW) is False


def test_role_at_least() -> None:
    assert role_at_least(Role.OWNER, Role.EDITOR) is True
    assert role_at_least(Role.VIEWER, Role.EDITOR) is False
    assert role_at_least(None, Role.VIEWER) is False
    assert role_at_least(Role.VIEWER, Role.VIEWER) is True


def test_max_role_picks_strongest() -> None:
    assert max_role(Role.VIEWER, Role.EDITOR, Role.COMMENTER) == Role.EDITOR
    assert max_role(None, Role.VIEWER, None) == Role.VIEWER
    assert max_role() is None
    assert max_role(None, None) is None


def test_minimum_role_for() -> None:
    assert minimum_role_for(Action.VIEW) == Role.VIEWER
    assert minimum_role_for(Action.COMMENT) == Role.COMMENTER
    assert minimum_role_for(Action.EDIT) == Role.EDITOR
    assert minimum_role_for(Action.SHARE) == Role.OWNER
    assert minimum_role_for(Action.DELETE) == Role.OWNER


# --------------------------------------------------------------------------- #
# GrantSet collapse
# --------------------------------------------------------------------------- #


def test_grantset_empty() -> None:
    assert GrantSet().is_empty() is True
    assert GrantSet().effective_role() is None


def test_grantset_most_permissive_wins() -> None:
    grants = GrantSet(direct_share=Role.VIEWER, workspace_role=Role.EDITOR)
    assert grants.effective_role() == Role.EDITOR


def test_grantset_personal_owner_beats_lower_share() -> None:
    grants = GrantSet(personal_owner=Role.OWNER, direct_share=Role.VIEWER)
    assert grants.effective_role() == Role.OWNER


def test_grantset_extra_folded_in() -> None:
    grants = GrantSet(direct_share=Role.VIEWER, extra=(Role.OWNER,))
    assert grants.effective_role() == Role.OWNER


# --------------------------------------------------------------------------- #
# decide()
# --------------------------------------------------------------------------- #


def test_decide_denies_with_no_grant() -> None:
    decision = decide(Action.VIEW, GrantSet())
    assert decision.allowed is False
    assert decision.effective_role is None
    assert "no grant" in decision.reason
    assert bool(decision) is False


def test_decide_allows_within_capability() -> None:
    decision = decide(Action.COMMENT, GrantSet(workspace_role=Role.COMMENTER))
    assert decision.allowed is True
    assert decision.effective_role == Role.COMMENTER
    assert bool(decision) is True


def test_decide_denies_beyond_capability_with_helpful_reason() -> None:
    decision = decide(Action.EDIT, GrantSet(direct_share=Role.VIEWER))
    assert decision.allowed is False
    assert decision.effective_role == Role.VIEWER
    assert "editor" in decision.reason


@pytest.mark.parametrize("role", ALL_ROLES)
@pytest.mark.parametrize("action", list(Action))
def test_decide_exhaustive(role: Role, action: Action) -> None:
    decision = decide(action, GrantSet(workspace_role=role))
    assert decision.allowed == (action in ROLE_CAPABILITIES[role])
    assert isinstance(decision, Decision)


def test_allowed_actions_matches_role() -> None:
    assert allowed_actions(GrantSet(workspace_role=Role.OWNER)) == frozenset(Action)
    assert allowed_actions(GrantSet()) == frozenset()
    assert allowed_actions(GrantSet(direct_share=Role.VIEWER)) == frozenset({Action.VIEW})
