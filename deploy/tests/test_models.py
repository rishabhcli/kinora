"""Tests for the deploy state machine + value types (no cloud, no clock)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.models import (
    ABORTABLE_STATES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    Artifact,
    DeployState,
    Environment,
    ServiceRole,
    SLOTarget,
    can_transition,
)


def test_artifact_requires_digest_and_name() -> None:
    with pytest.raises(ValueError):
        Artifact(name="x", tag="t", digest="")
    with pytest.raises(ValueError):
        Artifact(name="", tag="t", digest="sha256:abc")


def test_artifact_short_truncates_digest_body() -> None:
    art = Artifact(name="kinora-backend", tag="v1", digest="sha256:" + "f" * 64)
    assert art.short() == "f" * 12
    assert art.ref == "kinora-backend:v1@sha256:" + "f" * 64


def test_artifact_default_roles_nonempty() -> None:
    art = Artifact(name="x", tag="t", digest="sha256:abc")
    assert ServiceRole.RENDER_WORKER in art.roles
    with pytest.raises(ValueError):
        Artifact(name="x", tag="t", digest="sha256:abc", roles=())


def test_only_render_worker_drains_queue() -> None:
    assert ServiceRole.RENDER_WORKER.drains_queue is True
    for role in ServiceRole:
        if role is not ServiceRole.RENDER_WORKER:
            assert role.drains_queue is False


def test_environment_rank_is_ordered() -> None:
    assert Environment.DEV.rank < Environment.STAGING.rank < Environment.PROD.rank


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_legal_transition_examples() -> None:
    assert can_transition(DeployState.PENDING, DeployState.HYDRATING)
    assert can_transition(DeployState.ROLLING_OUT, DeployState.VERIFYING)
    assert can_transition(DeployState.VERIFYING, DeployState.ROLLING_OUT)  # next canary step
    assert can_transition(DeployState.VERIFYING, DeployState.ROLLING_BACK)
    assert can_transition(DeployState.PROMOTING, DeployState.SUCCEEDED)
    assert can_transition(DeployState.ROLLING_BACK, DeployState.ROLLED_BACK)


def test_illegal_transition_examples() -> None:
    assert not can_transition(DeployState.HYDRATING, DeployState.ROLLING_BACK)
    assert not can_transition(DeployState.SUCCEEDED, DeployState.ROLLING_OUT)
    assert not can_transition(DeployState.ROLLED_BACK, DeployState.PENDING)
    assert not can_transition(DeployState.PENDING, DeployState.SUCCEEDED)


def test_every_nonterminal_state_can_reach_a_terminal_state() -> None:
    # Reachability: BFS from each state must hit a terminal state.
    for start in DeployState:
        seen = {start}
        frontier = [start]
        reached_terminal = start in TERMINAL_STATES
        while frontier and not reached_terminal:
            cur = frontier.pop()
            for nxt in LEGAL_TRANSITIONS[cur]:
                if nxt in TERMINAL_STATES:
                    reached_terminal = True
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert reached_terminal, f"{start} cannot reach a terminal state"


def test_abortable_states_are_active_and_nonterminal() -> None:
    assert ABORTABLE_STATES.isdisjoint(TERMINAL_STATES)
    assert DeployState.ROLLING_BACK not in ABORTABLE_STATES


def test_slo_target_direction_semantics() -> None:
    higher = SLOTarget(name="success", threshold=0.95, higher_is_better=True)
    assert higher.is_breaching(0.90) is True
    assert higher.is_breaching(0.99) is False

    lower = SLOTarget(name="errors", threshold=0.05, higher_is_better=False)
    assert lower.is_breaching(0.10) is True
    assert lower.is_breaching(0.01) is False


def test_slo_target_breach_tolerance_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SLOTarget(name="x", threshold=1.0, breach_tolerance=0)
