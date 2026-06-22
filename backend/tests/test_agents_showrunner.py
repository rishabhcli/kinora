"""Unit tests for the Showrunner: the §7.2 arbitration policy (pure, all three
branches) and the arbitrate entry point with an injected textual-support
judgment (no network)."""

from __future__ import annotations

from app.agents.contracts import (
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    ConflictType,
    TextualSupport,
)
from app.agents.showrunner import Showrunner, decide_arbitration
from app.providers import Providers
from tests.test_agents_support import providers  # noqa: F401  (pytest fixture)


def _conflict(*, with_evolve: bool = True, user_facing: bool = True) -> ConflictObject:
    options = [
        ConflictOptionSpec(id=ConflictOption.HONOR_CANON, action="regenerate", cost_video_s=5.0),
        ConflictOptionSpec(id=ConflictOption.SURFACE_TO_USER, action="ask", cost_video_s=0.0),
    ]
    if with_evolve:
        options.append(
            ConflictOptionSpec(
                id=ConflictOption.EVOLVE_CANON, action="assert", requires="textual support"
            )
        )
    return ConflictObject(
        conflict_id="cf_001",
        raised_by="continuity_supervisor",
        type=ConflictType.CANON_VIOLATION,
        shot_id="shot_00051",
        claim="shot depicts the heroine drawing a sword",
        canon_fact="state_hero_sword_001 retired at beat_0034",
        current_beat="beat_0039",
        user_facing=user_facing,
        options=options,
    )


def test_policy_evolve_canon_with_textual_support() -> None:
    chosen, evolved = decide_arbitration(
        _conflict(), textual_support=True, director_present=False
    )
    assert chosen is ConflictOption.EVOLVE_CANON
    assert evolved is True


def test_policy_surface_when_user_facing_and_director_present() -> None:
    chosen, evolved = decide_arbitration(
        _conflict(), textual_support=False, director_present=True
    )
    assert chosen is ConflictOption.SURFACE_TO_USER
    assert evolved is False


def test_policy_honor_canon_is_the_safe_default() -> None:
    chosen, evolved = decide_arbitration(
        _conflict(), textual_support=False, director_present=False
    )
    assert chosen is ConflictOption.HONOR_CANON
    assert evolved is False


def test_policy_cannot_evolve_without_the_option() -> None:
    # Textual support is present, but the conflict offers no evolve option.
    chosen, _ = decide_arbitration(
        _conflict(with_evolve=False), textual_support=True, director_present=True
    )
    assert chosen is ConflictOption.SURFACE_TO_USER


async def test_arbitrate_surface_branch_with_injected_support(providers: Providers) -> None:  # noqa: F811
    decision = await Showrunner(providers).arbitrate(
        _conflict(),
        source_span_text="she reached the far bank empty-handed",
        director_present=True,
        textual_support=TextualSupport(supported=False, reasoning="no reacquisition in text"),
    )
    assert decision.conflict_id == "cf_001"
    assert decision.chosen_option is ConflictOption.SURFACE_TO_USER
    assert decision.evolved_canon is False
    assert decision.reasoning  # always returns reasoning


async def test_arbitrate_evolve_branch_with_injected_support(providers: Providers) -> None:  # noqa: F811
    decision = await Showrunner(providers).arbitrate(
        _conflict(),
        source_span_text="she pulled a new blade from the fallen guard",
        director_present=False,
        textual_support=TextualSupport(supported=True, reasoning="acquires a new sword"),
    )
    assert decision.chosen_option is ConflictOption.EVOLVE_CANON
    assert decision.evolved_canon is True
