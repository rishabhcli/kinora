"""Unit tests for the formal continuity-reasoning engine (§7.2, §8.5, §10).

Everything here is pure and network-free: Allen interval algebra, contradiction
detection with proof traces, the §8.5 retirement/forgetting case, epistemic
spoiler tracking, propagation, spatial continuity, and multi-hop inference.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.render.continuity_reasoning import (
    ALL_RELATIONS,
    Allen,
    AllenNetwork,
    BeatInterval,
    CanonTimeline,
    ContinuityEngine,
    Fact,
    FactQuery,
    Visibility,
    check_proposed_fact,
    compose,
    converse,
    detect_canon_contradictions,
    detect_spatial_conflicts,
    fact_from_state_slice,
    fact_slot,
    inverse,
    multi_hop_closure,
    prop_persistence_gaps,
    propagate_retirement,
    reader_knowledge_at,
    spoiler_risks,
    transitive_location,
)
from app.render.continuity_reasoning.belief import (
    BeliefState,
    ReaderBelief,
)
from app.render.continuity_reasoning.composition import compose_singletons
from app.render.continuity_reasoning.epistemic import (
    check_spoiler,
    dramatic_irony_beats,
)
from app.render.continuity_reasoning.facts import StateLike
from app.render.continuity_reasoning.propagation import (
    EffectKind,
    propagate_supersede,
)
from app.render.continuity_reasoning.spatial import colocated_at

# --------------------------------------------------------------------------- #
# A duck-typed StateSlice double (matches StateLike without importing memory)
# --------------------------------------------------------------------------- #


@dataclass
class FakeState:
    state_id: str
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    valid_to_beat: int | None = None


def _fact(
    subject: str,
    predicate: str,
    obj: str,
    start: int,
    end: int | None = None,
    *,
    fact_id: str = "",
    slot: str | None = None,
    visibility: Visibility = Visibility.KNOWN,
    revealed_at_beat: int | None = None,
) -> Fact:
    # Mirror the real StateSlice adapter: derive the slot from the object so a
    # hand-built fact shares a channel with a FactQuery for the same prop.
    resolved_slot = fact_slot(predicate, obj) if slot is None else slot
    return Fact(
        subject=subject,
        predicate=predicate,
        object=obj,
        interval=BeatInterval(start, end),
        fact_id=fact_id or f"{subject}_{predicate}_{obj}_{start}",
        slot=resolved_slot,
        visibility=visibility,
        revealed_at_beat=revealed_at_beat,
    )


# --------------------------------------------------------------------------- #
# Allen interval algebra
# --------------------------------------------------------------------------- #


def test_allen_relations_are_exhaustive_and_inverse_consistent() -> None:
    a = BeatInterval(10, 20)
    cases: list[tuple[BeatInterval, Allen]] = [
        (BeatInterval(30, 40), Allen.BEFORE),
        (BeatInterval(20, 30), Allen.MEETS),
        (BeatInterval(15, 30), Allen.OVERLAPS),
        (BeatInterval(10, 15), Allen.STARTED_BY),
        (BeatInterval(12, 18), Allen.CONTAINS),
        (BeatInterval(15, 20), Allen.FINISHED_BY),
        (BeatInterval(10, 20), Allen.EQUALS),
        (BeatInterval(0, 5), Allen.AFTER),
        (BeatInterval(0, 10), Allen.MET_BY),
        (BeatInterval(5, 15), Allen.OVERLAPPED_BY),
        (BeatInterval(10, 25), Allen.STARTS),
        (BeatInterval(5, 25), Allen.DURING),
        (BeatInterval(5, 20), Allen.FINISHES),
    ]
    for b, expected in cases:
        assert a.relate(b) is expected, f"{a} vs {b} expected {expected}"
        # Inverse identity: b relate a == inverse(a relate b).
        assert b.relate(a) is inverse(expected)


def test_open_ended_intervals_compare_at_infinity() -> None:
    open_a = BeatInterval(12, None)
    open_b = BeatInterval(12, None)
    assert open_a.relate(open_b) is Allen.EQUALS
    later_open = BeatInterval(20, None)
    # Both run to +∞; the later-starting one FINISHES the earlier.
    assert later_open.relate(open_a) is Allen.FINISHES
    closed = BeatInterval(0, 12)
    assert closed.relate(open_a) is Allen.MEETS


def test_half_open_membership_and_overlap() -> None:
    iv = BeatInterval(12, 34)
    assert iv.contains_beat(12)
    assert iv.contains_beat(33)
    assert not iv.contains_beat(34)  # half-open: retired at 34 ⇒ not active at 34
    assert not BeatInterval(12, 34).overlaps(BeatInterval(34, 40))  # meets, no shared beat
    assert BeatInterval(12, 34).overlaps(BeatInterval(33, 40))


def test_interval_rejects_inverted_bounds() -> None:
    import pytest

    with pytest.raises(ValueError):
        BeatInterval(40, 30)


# --------------------------------------------------------------------------- #
# Fact model + StateSlice adapter
# --------------------------------------------------------------------------- #


def test_fact_from_state_slice_round_trips_interval_and_slot() -> None:
    state = FakeState(
        state_id="s1",
        subject_entity_key="char_hero",
        predicate="possesses",
        object_value="prop_sword_001",
        valid_from_beat=12,
        valid_to_beat=34,
    )
    assert isinstance(state, StateLike)
    fact = fact_from_state_slice(state)
    assert fact.subject == "char_hero"
    assert fact.interval == BeatInterval(12, 34)
    assert fact.slot == "weapon"  # 'sword' → weapon slot
    assert fact.fact_id == "s1"


def test_fact_slot_categorises_props() -> None:
    assert fact_slot("possesses", "the iron sword") == "weapon"
    assert fact_slot("holding", "a lit torch") == "light"
    assert fact_slot("wearing", "the red cloak") == "outerwear"
    assert fact_slot("possesses", "a mysterious orb") == ""  # unknown → default slot
    assert fact_slot("located_in", "the cellar") == ""  # not a slotted predicate


# --------------------------------------------------------------------------- #
# Contradiction detection (live shot check) + proof traces
# --------------------------------------------------------------------------- #


def _unarmed_timeline() -> CanonTimeline:
    # Hero possessed a sword over [12, 34); it was retired at 34, then 'is unarmed'.
    return CanonTimeline.build(
        [
            _fact("char_hero", "possesses", "prop_sword_001", 12, 34, fact_id="state_sword"),
            _fact("char_hero", "is", "unarmed", 34, None, fact_id="state_unarmed"),
        ]
    )


def test_proposed_sword_after_retirement_contradicts_with_proof() -> None:
    timeline = _unarmed_timeline()
    query = FactQuery(
        subject="char_hero",
        predicate="possesses",
        object="prop_sword_001",
        at_beat=39,
        slot="weapon",
    )
    contradiction = check_proposed_fact(timeline, query)
    assert contradiction is not None
    assert contradiction.beat == 39
    assert contradiction.cited_fact_id == "state_sword"
    trace = contradiction.trace
    assert trace.contradiction is True
    # The proof names the retirement beat and the forgetting rule (§8.5).
    rendered = trace.render()
    assert "34" in rendered
    assert "retired" in rendered.lower()
    assert "state_sword" in trace.cited_fact_ids


def test_proposed_functional_clash_with_active_value() -> None:
    # Hero is 'unarmed' from 34; a shot depicting 'armed' at 36 clashes head-on.
    timeline = _unarmed_timeline()
    query = FactQuery(subject="char_hero", predicate="is", object="armed", at_beat=36)
    contradiction = check_proposed_fact(timeline, query)
    assert contradiction is not None
    assert contradiction.cited_fact_id == "state_unarmed"
    assert "unarmed" in contradiction.trace.render()


def test_clean_shot_returns_none() -> None:
    timeline = _unarmed_timeline()
    # Depicting the sword *while it was still possessed* (beat 20) is fine.
    query = FactQuery(
        subject="char_hero",
        predicate="possesses",
        object="prop_sword_001",
        at_beat=20,
        slot="weapon",
    )
    assert check_proposed_fact(timeline, query) is None


def test_detect_canon_self_contradiction() -> None:
    # A mis-asserted canon: hero is in two places over overlapping intervals.
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_forest", 10, 25, fact_id="a"),
            _fact("char_hero", "located_in", "loc_castle", 20, 30, fact_id="b"),
        ]
    )
    contradictions = detect_canon_contradictions(timeline)
    assert len(contradictions) == 1
    c = contradictions[0]
    assert c.beat == 20  # first shared beat
    assert {"a", "b"} == set(c.trace.cited_fact_ids)


def test_no_contradiction_when_intervals_meet_but_dont_overlap() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_forest", 10, 20, fact_id="a"),
            _fact("char_hero", "located_in", "loc_castle", 20, 30, fact_id="b"),
        ]
    )
    assert detect_canon_contradictions(timeline) == []


# --------------------------------------------------------------------------- #
# Epistemic tracking (reader knows vs. canon-true) — §10
# --------------------------------------------------------------------------- #


def _irony_timeline() -> CanonTimeline:
    # Canon-true from beat 5 that the butler is the culprit, but the reader does
    # not learn it until the reveal at beat 80 (dramatic irony in between).
    return CanonTimeline.build(
        [
            _fact(
                "char_butler",
                "is",
                "the_culprit",
                5,
                None,
                fact_id="twist",
                visibility=Visibility.HIDDEN,
                revealed_at_beat=80,
            ),
            _fact("char_hero", "located_in", "loc_manor", 5, None, fact_id="loc"),
        ]
    )


def test_reader_knowledge_splits_canon_truth_from_known() -> None:
    timeline = _irony_timeline()
    report = reader_knowledge_at(timeline, beat=40)
    canon_ids = {f.fact_id for f in report.canon_true}
    known_ids = {f.fact_id for f in report.reader_known}
    assert "twist" in canon_ids  # canon-true at 40
    assert "twist" not in known_ids  # but reader does not know it yet
    assert "loc" in known_ids
    assert report.has_dramatic_irony is True


def test_spoiler_risk_before_reveal_and_clear_after() -> None:
    timeline = _irony_timeline()
    risks_before = spoiler_risks(timeline, beat=40)
    assert len(risks_before) == 1
    assert "80" in risks_before[0].trace.render()  # cites the reveal beat
    # After the reveal, the reader knows the fact → no spoiler.
    assert spoiler_risks(timeline, beat=85) == []


def test_check_spoiler_for_a_proposed_depiction() -> None:
    timeline = _irony_timeline()
    query = FactQuery(
        subject="char_butler", predicate="is", object="the_culprit", at_beat=40
    )
    risk = check_spoiler(timeline, query)
    assert risk is not None
    assert risk.fact.fact_id == "twist"
    # The same depiction after the reveal is safe.
    later = FactQuery(
        subject="char_butler", predicate="is", object="the_culprit", at_beat=90
    )
    assert check_spoiler(timeline, later) is None


def test_dramatic_irony_beats_are_bounded_by_the_reveal() -> None:
    timeline = _irony_timeline()
    irony = dramatic_irony_beats(timeline)
    assert 5 in irony  # reader unaware from the twist's start
    assert 80 not in irony  # at the reveal the reader knows it


# --------------------------------------------------------------------------- #
# Propagation across dependent facts — §8.5
# --------------------------------------------------------------------------- #


def test_propagate_retirement_recommends_closing_dependents() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "possesses", "prop_lantern", 10, None, fact_id="have"),
            _fact("char_hero", "holding", "prop_lantern", 12, None, fact_id="hold"),
            _fact("char_villain", "fears", "prop_lantern", 14, None, fact_id="fear"),
        ]
    )
    effects = propagate_retirement(timeline, retired_object="prop_lantern", at_beat=20)
    kinds = {e.affected.fact_id: e.kind for e in effects}
    # Possession/holding (entity-valued) → RETIRE; 'fears' (not entity-valued) → REVIEW.
    assert kinds["have"] is EffectKind.RETIRE
    assert kinds["hold"] is EffectKind.RETIRE
    assert kinds["fear"] is EffectKind.REVIEW
    assert all(e.at_beat == 20 for e in effects)


def test_propagate_supersede_closes_prior_open_value() -> None:
    timeline = CanonTimeline.build(
        [_fact("char_hero", "located_in", "loc_forest", 10, None, fact_id="old")]
    )
    new_fact = _fact("char_hero", "located_in", "loc_castle", 25, None, fact_id="new")
    effects = propagate_supersede(timeline, new_fact=new_fact)
    assert len(effects) == 1
    assert effects[0].affected.fact_id == "old"
    assert effects[0].at_beat == 25
    assert effects[0].kind is EffectKind.RETIRE


def test_propagate_supersede_ignores_same_value() -> None:
    timeline = CanonTimeline.build(
        [_fact("char_hero", "located_in", "loc_forest", 10, None, fact_id="old")]
    )
    same = _fact("char_hero", "located_in", "loc_forest", 25, None, fact_id="same")
    assert propagate_supersede(timeline, new_fact=same) == []


# --------------------------------------------------------------------------- #
# Spatial continuity + prop persistence
# --------------------------------------------------------------------------- #


def test_detect_spatial_teleport() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_forest", 10, 25, fact_id="f"),
            _fact("char_hero", "located_in", "loc_castle", 20, 30, fact_id="c"),
        ]
    )
    conflicts = detect_spatial_conflicts(timeline)
    assert len(conflicts) == 1
    assert conflicts[0].subject == "char_hero"
    assert {conflicts[0].place_a, conflicts[0].place_b} == {"loc_forest", "loc_castle"}
    assert "teleport" in conflicts[0].trace.render().lower()


def test_colocated_at_groups_subjects_in_a_place() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_hall", 10, None),
            _fact("char_villain", "located_in", "loc_hall", 12, None),
            _fact("char_child", "located_in", "loc_garden", 10, None),
        ]
    )
    assert colocated_at(timeline, "loc_hall", 15) == ("char_hero", "char_villain")


def test_prop_persistence_flags_depiction_after_retirement() -> None:
    timeline = CanonTimeline.build(
        [_fact("char_hero", "possesses", "the sword", 12, 34, fact_id="sword")]
    )
    # A shot of the hero at beat 40 still showing the sword (retired at 34).
    gaps = prop_persistence_gaps(timeline, {("char_hero", "the sword"): 40})
    assert len(gaps) == 1
    assert gaps[0].object == "the sword"
    assert gaps[0].next_beat == 40
    assert "must NOT appear" in gaps[0].trace.render()


# --------------------------------------------------------------------------- #
# Multi-hop inference
# --------------------------------------------------------------------------- #


def test_transitive_location_places_carried_prop() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_cellar", 40, None, fact_id="loc"),
            _fact("char_hero", "possesses", "prop_lantern", 38, None, fact_id="have"),
        ]
    )
    inferred = transitive_location(timeline, beat=40)
    assert len(inferred) == 1
    derived = inferred[0].fact
    assert derived.subject == "prop_lantern"
    assert derived.object == "loc_cellar"
    assert inferred[0].hops == 1
    assert "carried" in inferred[0].trace.render().lower()


def test_multi_hop_closure_places_accompanying_party_and_their_props() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_hero", "located_in", "loc_ship", 50, None, fact_id="loc"),
            _fact("char_hero", "accompanied_by", "char_squire", 50, None, fact_id="party"),
            _fact("char_squire", "possesses", "prop_map", 48, None, fact_id="map"),
        ]
    )
    inferred = multi_hop_closure(timeline, beat=50)
    by_subject = {i.fact.subject: i.fact for i in inferred}
    # The squire is placed on the ship (hop 1)...
    assert by_subject["char_squire"].object == "loc_ship"
    # ...and the squire's map is then placed there too (carried, composed).
    assert by_subject["prop_map"].object == "loc_ship"


def test_multi_hop_closure_terminates_on_cyclic_accompaniment() -> None:
    timeline = CanonTimeline.build(
        [
            _fact("char_a", "located_in", "loc_camp", 5, None, fact_id="loc"),
            _fact("char_a", "accompanied_by", "char_b", 5, None, fact_id="ab"),
            _fact("char_b", "accompanied_by", "char_a", 5, None, fact_id="ba"),
        ]
    )
    inferred = multi_hop_closure(timeline, beat=5, max_hops=4)
    # Both end up at the camp; the cycle does not loop forever.
    placed = {i.fact.subject: i.fact.object for i in inferred}
    assert placed["char_b"] == "loc_camp"


# --------------------------------------------------------------------------- #
# The ContinuityEngine façade (the agent's entry point)
# --------------------------------------------------------------------------- #


def test_engine_check_shot_claims_flags_contradiction() -> None:
    engine = ContinuityEngine.from_state_slices(
        [
            FakeState("state_sword", "char_hero", "possesses", "prop_sword_001", 12, 34),
            FakeState("state_unarmed", "char_hero", "is", "unarmed", 34, None),
        ]
    )
    verdict = engine.check_shot_claims(
        [
            FactQuery(
                subject="char_hero",
                predicate="possesses",
                object="prop_sword_001",
                at_beat=39,
                slot="weapon",
            )
        ]
    )
    assert verdict.ok is False
    assert verdict.primary is not None
    assert verdict.primary.cited_fact_id == "state_sword"
    assert "CONTRADICTION" in verdict.proof_text()


def test_engine_audit_canon_finds_teleport() -> None:
    engine = ContinuityEngine.from_state_slices(
        [
            FakeState("f", "char_hero", "located_in", "loc_forest", 10, 25),
            FakeState("c", "char_hero", "located_in", "loc_castle", 20, 30),
        ]
    )
    verdict = engine.audit_canon()
    assert verdict.ok is False
    assert any(f.kind.value == "spatial" for f in verdict.findings)


def test_engine_spoiler_path_via_hidden_state() -> None:
    engine = ContinuityEngine.from_state_slices(
        [FakeState("twist", "char_butler", "is", "the_culprit", 5, None)],
        hidden_state_ids=["twist"],
        revealed_at={"twist": 80},
    )
    verdict = engine.check_shot_claims(
        [FactQuery(subject="char_butler", predicate="is", object="the_culprit", at_beat=40)]
    )
    assert verdict.ok is False
    assert verdict.primary is not None
    assert verdict.primary.kind.value == "spoiler"


def test_engine_with_inference_catches_composed_contradiction() -> None:
    # The lantern is canonically destroyed (retired) at beat 30; the hero is in
    # the cellar at 40 and carries... nothing. But a shot claims the carried
    # lantern is in the cellar — caught only once the carried-location is closed.
    engine = ContinuityEngine.from_state_slices(
        [
            FakeState("loc", "char_hero", "located_in", "loc_cellar", 40, None),
            FakeState("have", "char_hero", "possesses", "prop_lantern", 38, None),
        ]
    )
    enriched = engine.with_inference_at(40)
    inferred = {f.subject: f for f in enriched.timeline.facts if f.source.startswith("inferred")}
    assert "prop_lantern" in inferred
    assert inferred["prop_lantern"].object == "loc_cellar"


def test_engine_clean_when_no_claims_problematic() -> None:
    engine = ContinuityEngine.from_state_slices(
        [FakeState("state_unarmed", "char_hero", "is", "unarmed", 34, None)]
    )
    verdict = engine.check_shot_claims(
        [FactQuery(subject="char_hero", predicate="located_in", object="loc_forest", at_beat=40)]
    )
    assert verdict.ok is True
    assert verdict.findings == ()


# --------------------------------------------------------------------------- #
# Allen composition table + path-consistency constraint network (§8.5)
# --------------------------------------------------------------------------- #


def test_composition_table_identity_and_transitivity() -> None:
    # EQUALS is the two-sided identity.
    for r in Allen:
        assert compose_singletons(Allen.EQUALS, r) == frozenset({r})
        assert compose_singletons(r, Allen.EQUALS) == frozenset({r})
    # BEFORE and DURING are transitive (idempotent singletons).
    assert compose_singletons(Allen.BEFORE, Allen.BEFORE) == frozenset({Allen.BEFORE})
    assert compose_singletons(Allen.DURING, Allen.DURING) == frozenset({Allen.DURING})
    # meets ∘ meets = before; before ∘ after carries no information.
    assert compose_singletons(Allen.MEETS, Allen.MEETS) == frozenset({Allen.BEFORE})
    assert compose_singletons(Allen.BEFORE, Allen.AFTER) == ALL_RELATIONS


def test_composition_converse_law_holds_for_all_pairs() -> None:
    # (r1 ∘ r2)⁻¹ == r2⁻¹ ∘ r1⁻¹ for every singleton pair.
    for r1 in Allen:
        for r2 in Allen:
            lhs = converse(compose_singletons(r1, r2))
            rhs = compose(frozenset({inverse(r2)}), frozenset({inverse(r1)}))
            assert lhs == rhs


def test_compose_relation_sets_unions_members() -> None:
    result = compose(frozenset({Allen.BEFORE, Allen.MEETS}), frozenset({Allen.BEFORE}))
    # both before∘before and meets∘before are {before}.
    assert result == frozenset({Allen.BEFORE})


def test_network_detects_unsatisfiable_temporal_cycle() -> None:
    net = AllenNetwork()
    net.constrain("duel", "funeral", frozenset({Allen.BEFORE}))
    net.constrain("funeral", "wedding", frozenset({Allen.BEFORE}))
    net.constrain("wedding", "duel", frozenset({Allen.BEFORE}))
    result = net.path_consistency()
    assert result.consistent is False
    assert result.empty is True
    assert result.trace is not None
    assert "INCONSISTENT" in result.trace.render()


def test_network_infers_transitive_ordering() -> None:
    net = AllenNetwork()
    net.constrain("a", "b", frozenset({Allen.BEFORE}))
    net.constrain("b", "c", frozenset({Allen.BEFORE}))
    result = net.path_consistency()
    assert result.consistent is True
    # a→c was not asserted, but composition forces 'before'.
    assert net.relation("a", "c") == frozenset({Allen.BEFORE})


def test_network_from_concrete_intervals_is_consistent() -> None:
    net = AllenNetwork.from_intervals(
        {
            "x": BeatInterval(10, 20),
            "y": BeatInterval(20, 30),
            "z": BeatInterval(15, 25),
        }
    )
    result = net.path_consistency()
    assert result.consistent is True
    assert net.relation("x", "y") == frozenset({Allen.MEETS})


def test_engine_audit_temporal_consistency_flags_imposed_cycle() -> None:
    # Three facts with concrete intervals are consistent on their own; an
    # author-imposed ordering constraint creates an impossible cycle.
    engine = ContinuityEngine.from_state_slices(
        [
            FakeState("e1", "story", "event", "duel", 10, 20),
            FakeState("e2", "story", "event", "funeral", 30, 40),
            FakeState("e3", "story", "event", "wedding", 50, 60),
        ]
    )
    # e1 < e2 < e3 by their beats, but assert wedding BEFORE duel → cycle.
    verdict = engine.audit_temporal_consistency(
        ordering_constraints=[("e3", "e1", frozenset({Allen.BEFORE}))]
    )
    assert verdict.ok is False
    assert verdict.primary is not None
    assert verdict.primary.kind.value == "temporal"


def test_engine_audit_temporal_consistency_clean_without_constraints() -> None:
    engine = ContinuityEngine.from_state_slices(
        [
            FakeState("e1", "story", "event", "duel", 10, 20),
            FakeState("e2", "story", "event", "funeral", 30, 40),
        ]
    )
    assert engine.audit_temporal_consistency().ok is True


# --------------------------------------------------------------------------- #
# Reader belief revision — unreliable narrator / misdirection (§10)
# --------------------------------------------------------------------------- #


def _misdirection_state() -> BeliefState:
    # Canon: the stranger is the long-lost prince from beat 5. The reader, however,
    # believes he is a common thief until the reveal at beat 90.
    timeline = CanonTimeline.build(
        [_fact("char_stranger", "is", "the_prince", 5, None, fact_id="truth")]
    )
    belief = ReaderBelief(
        subject="char_stranger",
        predicate="is",
        object="a_common_thief",
        interval=BeatInterval(5, None),
        mistaken=True,
        corrected_at_beat=90,
    )
    return BeliefState.build(timeline, [belief])


def test_reader_belief_is_the_render_target_before_reveal() -> None:
    state = _misdirection_state()
    # At beat 40 the shot should depict what the reader believes, not the canon.
    assert state.believed_value("char_stranger", "is", 40) == "a_common_thief"
    thief_shot = FactQuery(
        subject="char_stranger", predicate="is", object="a_common_thief", at_beat=40
    )
    prince_shot = FactQuery(
        subject="char_stranger", predicate="is", object="the_prince", at_beat=40
    )
    assert state.matches_reader_belief(thief_shot) is True
    # Depicting the canonical truth early would spoil the reveal.
    assert state.matches_reader_belief(prince_shot) is False


def test_reader_belief_flips_to_canon_after_reveal() -> None:
    state = _misdirection_state()
    # After the reveal the belief is dropped; the reader believes the canon.
    assert state.believed_value("char_stranger", "is", 95) == "the_prince"


def test_dramatic_irony_detected_while_belief_is_false() -> None:
    state = _misdirection_state()
    irony = state.dramatic_irony_at(40)
    assert len(irony) == 1
    belief, canon = irony[0]
    assert belief.object == "a_common_thief"
    assert canon.object == "the_prince"
    # Once corrected, no irony remains.
    assert state.dramatic_irony_at(95) == []


def test_belief_revision_fires_at_the_reveal_with_proof() -> None:
    state = _misdirection_state()
    revisions = state.revisions()
    assert len(revisions) == 1
    rev = revisions[0]
    assert rev.beat == 90
    assert rev.dropped.object == "a_common_thief"
    assert rev.adopted is not None and rev.adopted.object == "the_prince"
    assert "REVISION" in rev.trace.render()
