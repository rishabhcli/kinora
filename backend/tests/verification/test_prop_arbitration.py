"""Property tests for the §7.2 conflict-arbitration policy (``decide_arbitration``).

The policy is a fixed three-branch gate over a structured conflict plus two
booleans (``textual_support``, ``director_present``). These properties pin the
documented branch order and the safety invariants the canon depends on:

* the canon **evolves** only when the conflict offers it *and* the text supports
  the change (the no-silent-rewrite guarantee);
* a conflict **surfaces** only to a present director on a user-facing conflict;
* otherwise the canon is **honored** (the safe default);
* with no series context, the gate is exactly the documented three branches; the
  optional ``context`` hook only ever upgrades honor→surface, never relaxes
  evolve/surface eligibility.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.agents.contracts import ConflictObject, ConflictOption
from app.agents.showrunner import decide_arbitration
from app.verification.properties.strategies import conflict_objects


def _offers_evolve(conflict: ConflictObject) -> bool:
    return any(opt.id is ConflictOption.EVOLVE_CANON for opt in conflict.options)


@given(conflict_objects(), st.booleans(), st.booleans())
def test_returns_a_valid_option_and_consistent_evolved_flag(
    conflict: ConflictObject, support: bool, director: bool
) -> None:
    """The result is always a valid option, and ``evolved`` iff EVOLVE was chosen."""
    option, evolved = decide_arbitration(
        conflict, textual_support=support, director_present=director
    )
    assert option in set(ConflictOption)
    assert evolved == (option is ConflictOption.EVOLVE_CANON)


@given(conflict_objects(), st.booleans(), st.booleans())
def test_deterministic(conflict: ConflictObject, support: bool, director: bool) -> None:
    first = decide_arbitration(conflict, textual_support=support, director_present=director)
    again = decide_arbitration(conflict, textual_support=support, director_present=director)
    assert first == again


@given(conflict_objects(), st.booleans(), st.booleans())
def test_evolve_requires_offer_and_textual_support(
    conflict: ConflictObject, support: bool, director: bool
) -> None:
    """The no-silent-rewrite invariant: evolve ⇒ (offered ∧ text-supported) (§7.2)."""
    option, evolved = decide_arbitration(
        conflict, textual_support=support, director_present=director
    )
    if option is ConflictOption.EVOLVE_CANON:
        assert _offers_evolve(conflict)
        assert support
        assert evolved is True


@given(conflict_objects(), st.booleans())
def test_evolve_fires_whenever_offered_and_supported(
    conflict: ConflictObject, director: bool
) -> None:
    """Conversely: when evolve is offered AND text-supported, evolve always wins.

    Evolve is the first branch, so a present/absent director is irrelevant here.
    """
    if _offers_evolve(conflict):
        option, evolved = decide_arbitration(
            conflict, textual_support=True, director_present=director
        )
        assert option is ConflictOption.EVOLVE_CANON
        assert evolved is True


@given(conflict_objects(), st.booleans())
def test_no_textual_support_never_evolves(
    conflict: ConflictObject, director: bool
) -> None:
    """Without textual support the canon is never silently rewritten."""
    option, evolved = decide_arbitration(
        conflict, textual_support=False, director_present=director
    )
    assert option is not ConflictOption.EVOLVE_CANON
    assert evolved is False


@given(conflict_objects())
def test_no_director_and_no_support_honors_canon(conflict: ConflictObject) -> None:
    """The safe default: nobody to surface to + no text support ⇒ honor."""
    option, evolved = decide_arbitration(
        conflict, textual_support=False, director_present=False
    )
    assert option is ConflictOption.HONOR_CANON
    assert evolved is False


@given(conflict_objects())
def test_surface_requires_present_director_on_user_facing(
    conflict: ConflictObject,
) -> None:
    """Surface ⇒ a present director on a user-facing conflict (§7.2)."""
    option, _ = decide_arbitration(
        conflict, textual_support=False, director_present=True
    )
    if option is ConflictOption.SURFACE_TO_USER:
        assert conflict.user_facing
    else:
        # The only other no-support option is honor.
        assert option is ConflictOption.HONOR_CANON


@given(conflict_objects())
def test_non_user_facing_never_surfaces(conflict: ConflictObject) -> None:
    """A non-user-facing conflict is resolved internally — never surfaced."""
    if not conflict.user_facing:
        for support in (True, False):
            option, _ = decide_arbitration(
                conflict, textual_support=support, director_present=True
            )
            assert option is not ConflictOption.SURFACE_TO_USER


def test_full_branch_table_is_exact() -> None:
    """An explicit truth table over the policy's three structural axes (§7.2).

    Axes: evolve offered? · text supported? · director present? · user-facing?
    """
    from app.agents.contracts import ConflictOptionSpec

    def make(offer_evolve: bool, user_facing: bool) -> ConflictObject:
        options = (
            [ConflictOptionSpec(id=ConflictOption.EVOLVE_CANON, action="evolve")]
            if offer_evolve
            else []
        )
        return ConflictObject(
            conflict_id="c",
            raised_by="critic",
            claim="x",
            user_facing=user_facing,
            options=options,
        )

    HONOR = ConflictOption.HONOR_CANON
    SURFACE = ConflictOption.SURFACE_TO_USER
    EVOLVE = ConflictOption.EVOLVE_CANON

    cases = [
        # (offer_evolve, support, director, user_facing) -> expected option
        ((True, True, False, True), EVOLVE),
        ((True, True, True, False), EVOLVE),  # evolve precedes the surface gate
        ((True, False, True, True), SURFACE),  # offered but unsupported → surface gate
        ((False, True, True, True), SURFACE),  # not offered → can't evolve
        ((False, True, False, True), HONOR),  # supported but nothing to evolve, no director
        ((True, False, False, True), HONOR),
        ((False, False, True, False), HONOR),  # non-user-facing never surfaces
        ((False, False, False, False), HONOR),
    ]
    for (offer, support, director, uf), expected in cases:
        conflict = make(offer, uf)
        option, evolved = decide_arbitration(
            conflict, textual_support=support, director_present=director
        )
        assert option is expected, (offer, support, director, uf, option)
        assert evolved == (expected is EVOLVE)
