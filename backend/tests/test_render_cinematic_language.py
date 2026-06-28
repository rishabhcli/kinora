"""The cinematic-language model (Cinematographer / §9.3, §7.1, §10).

Pure deterministic film grammar: genre/mood inference, director-style emulation
profiles, lens/lighting/colour-grade derivation, scene coverage (master/medium/
CU), shot/reverse-shot eyeline matching, character blocking, and shot-length
cadence. No network, no model — every function is a pure function of beat text,
so the grammar is exhaustively testable.
"""

from __future__ import annotations

from app.agents.contracts import Beat
from app.render.cinematic_language import (
    Cadence,
    CoverageRole,
    FramePosition,
    Genre,
    LookJumpKind,
    Mood,
    ScenePlan,
    ShotPlan,
    Transition,
    block_subjects,
    camera_for_beat,
    color_grade_for,
    compile_scene_prompts,
    compile_shot_prompt,
    detect_look_jumps,
    expressive_move_for,
    infer_genre,
    infer_mood,
    infer_style_override,
    lens_for,
    lighting_for,
    move_phrase,
    negative_prompt_for,
    plan_coverage,
    plan_scene,
    plan_transitions,
    select_style_profile,
    shot_length_cadence,
    shot_reverse_shot,
    style_prompt_fragment,
    transition_between,
    transition_seconds,
)
from app.render.shot_grammar import ScreenDirection, violates_180


def _beat(summary: str, *, mood: str = "", visuals: str = "") -> Beat:
    return Beat(
        beat_id="b",
        scene_id="s",
        beat_index=0,
        summary=summary,
        mood=mood,
        described_visuals=visuals or None,
    )


# --------------------------------------------------------------------------- #
# Genre + mood inference
# --------------------------------------------------------------------------- #


def test_infer_genre_picks_dominant_cue() -> None:
    action = [_beat("a frantic chase as the gun fires and they sprint to escape")]
    assert infer_genre(action) is Genre.ACTION
    romance = [_beat("a tender kiss, the lovers in a long embrace, full of longing")]
    assert infer_genre(romance) is Genre.ROMANCE
    fantasy = [_beat("the dragon circles the kingdom while the wizard readies a spell")]
    assert infer_genre(fantasy) is Genre.FANTASY


def test_infer_genre_neutral_when_no_cue() -> None:
    assert infer_genre([_beat("a person stands by a table")]) is Genre.NEUTRAL
    assert infer_genre([]) is Genre.NEUTRAL


def test_infer_genre_noir_beats_drama_on_overlap() -> None:
    # "quiet" is a drama cue, "shadows"/"cigarette" are noir — noir is more specific.
    beats = [_beat("a quiet detective lights a cigarette amid the shadows")]
    assert infer_genre(beats) is Genre.NOIR


def test_infer_mood_reads_register() -> None:
    assert infer_mood(_beat("a tense, urgent moment full of danger")) is Mood.TENSE
    assert infer_mood(_beat("a tender, gentle, loving touch")) is Mood.TENDER
    assert infer_mood(_beat("a still empty room")) is Mood.CALM
    assert infer_mood(_beat("a person walks across")) is Mood.NEUTRAL


# --------------------------------------------------------------------------- #
# Director-style emulation profiles
# --------------------------------------------------------------------------- #


def test_select_profile_from_genre() -> None:
    noir = select_style_profile([_beat("the detective in rain-slick shadows, smoke curling")])
    assert noir.name == "noir_chiaroscuro"
    action = select_style_profile([_beat("a chase, a fight, an explosion, sprint to escape")])
    assert action.name == "kinetic_action"
    neutral = select_style_profile([_beat("a person at a table")])
    assert neutral.name == "classical_balanced"


def test_select_profile_override_wins() -> None:
    p = select_style_profile([_beat("a chase scene")], override="anamorphic_symmetry")
    assert p.name == "anamorphic_symmetry" and p.symmetric is True


def test_select_profile_from_style_token() -> None:
    p = select_style_profile(
        [_beat("a chase scene")], style_tokens={"director_style": "romantic_soft"}
    )
    assert p.name == "romantic_soft"


def test_select_profile_unknown_name_falls_back_to_genre() -> None:
    # An unknown override is ignored; the genre default is used instead.
    p = select_style_profile(
        [_beat("the dragon and the wizard's spell")], override="does_not_exist"
    )
    assert p.name == "epic_vista"


def test_style_prompt_fragment_is_one_clause() -> None:
    p = select_style_profile([], override="anamorphic_symmetry")
    frag = style_prompt_fragment(p)
    assert "anamorphic" in frag
    assert "symmetrical centred composition" in frag


# --------------------------------------------------------------------------- #
# Lens / lighting / colour-grade
# --------------------------------------------------------------------------- #


def test_lens_intimate_and_vista_override_profile() -> None:
    profile = select_style_profile([], override="kinetic_action")
    assert "85mm" in lens_for(_beat("a close look at her trembling hands"), profile)
    assert "24mm" in lens_for(_beat("a vast vista across the horizon"), profile)
    # No intimate/vista cue → the profile's own lens.
    assert lens_for(_beat("she walks"), profile) == profile.lens


def test_lighting_and_grade_nudged_by_mood() -> None:
    profile = select_style_profile([], override="classical_balanced")
    tense = lighting_for(_beat("a tense, dangerous standoff"), profile)
    assert tense.startswith(profile.lighting) and "harder key" in tense
    tender = color_grade_for(_beat("a tender, loving embrace"), profile)
    assert tender.startswith(profile.grade) and "warmer" in tender
    # A neutral beat leaves the profile defaults untouched.
    assert lighting_for(_beat("a person stands"), profile) == profile.lighting


# --------------------------------------------------------------------------- #
# Scene coverage — master / medium / CU
# --------------------------------------------------------------------------- #


def test_plan_coverage_opens_on_master_then_mediums_and_closes() -> None:
    beats = [
        _beat("the hall, wide and empty"),  # ordinal 0 → master
        _beat("she crosses the floor"),  # medium
        _beat("a close look at her eyes, trembling"),  # close insert
    ]
    plan = plan_coverage(beats)
    assert plan[0].role is CoverageRole.MASTER
    roles = [c.role for c in plan]
    assert CoverageRole.MEDIUM in roles
    assert CoverageRole.CLOSE in roles


def test_plan_coverage_adds_reverse_for_two_hander() -> None:
    beats = [_beat("two figures face each other across the table"), _beat("they argue")]
    plan = plan_coverage(beats, subjects=["alice", "bob"])
    roles = [c.role for c in plan]
    assert CoverageRole.REVERSE in roles
    reverse = next(c for c in plan if c.role is CoverageRole.REVERSE)
    assert reverse.subject == "bob"


def test_plan_coverage_empty() -> None:
    assert plan_coverage([]) == []


# --------------------------------------------------------------------------- #
# Shot / reverse-shot + eyeline match
# --------------------------------------------------------------------------- #


def test_shot_reverse_shot_matches_eyelines_and_holds_axis() -> None:
    pair = shot_reverse_shot("alice", "bob", axis=ScreenDirection.LEFT_TO_RIGHT)
    assert pair.a_looks is ScreenDirection.LEFT_TO_RIGHT
    assert pair.b_looks is ScreenDirection.RIGHT_TO_LEFT
    # The pair stays on one side of the line: the eyeline flip across the cut is
    # the *intended* reverse, so it must not read as a 180° violation.
    assert violates_180(pair.a_looks, pair.b_looks, reversal=True) is False


def test_shot_reverse_shot_defaults_invalid_axis() -> None:
    pair = shot_reverse_shot("a", "b", axis=ScreenDirection.NEUTRAL)
    assert pair.axis is ScreenDirection.LEFT_TO_RIGHT


# --------------------------------------------------------------------------- #
# Character blocking
# --------------------------------------------------------------------------- #


def test_blocking_follows_screen_direction_with_lead_room() -> None:
    beats = [
        _beat("she runs to the right across the bridge"),  # L2R → left third, lead right
        _beat("the planks blur beneath her"),  # holds L2R
    ]
    blocks = block_subjects(beats)
    assert blocks[0].position is FramePosition.LEFT
    assert blocks[0].lead_room is FramePosition.RIGHT
    assert blocks[1].position is FramePosition.LEFT  # direction held


def test_blocking_centres_for_symmetric_profile() -> None:
    profile = select_style_profile([], override="anamorphic_symmetry")
    beats = [_beat("she runs to the right")]
    blocks = block_subjects(beats, profile=profile)
    assert blocks[0].position is FramePosition.CENTER
    assert blocks[0].lead_room is None


def test_blocking_centres_neutral_beat() -> None:
    blocks = block_subjects([_beat("a still empty room")])
    assert blocks[0].position is FramePosition.CENTER


# --------------------------------------------------------------------------- #
# Visual rhythm / cadence
# --------------------------------------------------------------------------- #


def test_cadence_building_scene_tightens() -> None:
    beats = [_beat("the chase begins"), _beat("they sprint"), _beat("the escape")]
    cadence = shot_length_cadence(beats)
    assert isinstance(cadence, Cadence)
    assert cadence.building is True
    # Each successive shot is no longer than the previous (accelerating cut).
    assert cadence.lengths_s[0] >= cadence.lengths_s[1] >= cadence.lengths_s[2]


def test_cadence_holding_scene_plays_long_and_flat() -> None:
    beats = [_beat("a calm, quiet morning"), _beat("she lingers, still")]
    cadence = shot_length_cadence(beats)
    assert cadence.building is False
    assert all(length >= 5.0 for length in cadence.lengths_s)


def test_cadence_empty() -> None:
    cadence = shot_length_cadence([])
    assert cadence.lengths_s == [] and cadence.building is False


# --------------------------------------------------------------------------- #
# Camera derivation
# --------------------------------------------------------------------------- #


def test_camera_for_beat_opens_with_push_in_on_wide() -> None:
    profile = select_style_profile([], override="classical_balanced")
    cam = camera_for_beat(0, _beat("the wide hall"), profile)
    assert cam.shot_size == "wide"
    assert cam.move == "push_in"


def test_camera_for_beat_static_on_pose() -> None:
    profile = select_style_profile([], override="kinetic_action")
    cam = camera_for_beat(2, _beat("she comes to rest, frozen"), profile)
    assert cam.move == "static"


def test_camera_speed_fast_when_building() -> None:
    profile = select_style_profile([], override="kinetic_action")
    cadence = shot_length_cadence([_beat("a chase"), _beat("a sprint")])
    cam = camera_for_beat(1, _beat("she sprints"), profile, cadence=cadence)
    assert cam.speed == "fast"


# --------------------------------------------------------------------------- #
# Scene sequencer — the whole cinematic language tied together
# --------------------------------------------------------------------------- #


def _scene_beats() -> list[Beat]:
    return [
        Beat(beat_id="b0", scene_id="s", summary="the wide hall, the detective enters the shadows"),
        Beat(beat_id="b1", scene_id="s", summary="he crosses the floor toward the desk"),
        Beat(beat_id="b2", scene_id="s", summary="a close look at his eyes, full of dread"),
    ]


def test_plan_scene_produces_a_shot_per_beat_with_one_eye() -> None:
    plan = plan_scene(_scene_beats())
    assert isinstance(plan, ScenePlan)
    assert plan.genre is Genre.NOIR
    assert plan.profile.name == "noir_chiaroscuro"
    assert len(plan.shots) == 3
    assert all(isinstance(s, ShotPlan) for s in plan.shots)
    # The look (grade) is the one profile's grade across the scene (one eye).
    assert all(s.grade.startswith(plan.profile.grade) for s in plan.shots)


def test_plan_scene_first_shot_establishes_wide() -> None:
    plan = plan_scene(_scene_beats())
    assert plan.shots[0].shot_size == "wide"
    assert plan.shots[0].camera.move == "push_in"
    # The intimate final beat lands close on a portrait lens.
    assert plan.shots[-1].shot_size == "close"
    assert "85mm" in plan.shots[-1].lens


def test_plan_scene_reports_axis_violation() -> None:
    beats = [
        Beat(beat_id="b0", scene_id="s", summary="she sprints to the right"),
        Beat(beat_id="b1", scene_id="s", summary="she edges leftward without turning"),
    ]
    plan = plan_scene(beats)
    assert len(plan.axis_violations) == 1
    assert plan.axis_violations[0].ordinal == 1


def test_plan_scene_includes_coverage_and_reverses_for_two_hander() -> None:
    beats = [
        Beat(beat_id="b0", scene_id="s", summary="two figures face each other"),
        Beat(beat_id="b1", scene_id="s", summary="they argue bitterly"),
    ]
    plan = plan_scene(beats, subjects=["alice", "bob"])
    roles = [c.role for c in plan.coverage]
    assert CoverageRole.MASTER in roles
    assert CoverageRole.REVERSE in roles


def test_plan_scene_empty() -> None:
    plan = plan_scene([])
    assert plan.shots == [] and plan.coverage == [] and plan.axis_violations == []


def test_plan_scene_override_forces_eye() -> None:
    plan = plan_scene(_scene_beats(), profile_override="anamorphic_symmetry")
    assert plan.profile.symmetric is True
    # A symmetric eye centres every subject (no lead-room blocking).
    assert all(s.blocking.position is FramePosition.CENTER for s in plan.shots)


# --------------------------------------------------------------------------- #
# Prompt-fragment compiler
# --------------------------------------------------------------------------- #


def test_compile_shot_prompt_names_subject_and_look() -> None:
    plan = plan_scene(_scene_beats())
    prompt = compile_shot_prompt(plan.shots[-1], subject="the detective")
    # The intimate final beat compiles a close-up on the named subject, 85mm,
    # with the noir grade.
    assert "close-up" in prompt
    assert "the detective" in prompt
    assert "85mm" in prompt
    assert plan.shots[-1].grade.split(";")[0] in prompt


def test_compile_shot_prompt_lead_room_for_moving_subject() -> None:
    beats = [
        Beat(beat_id="b0", scene_id="s", summary="the wide street"),
        Beat(beat_id="b1", scene_id="s", summary="she runs to the right past the shops"),
    ]
    plan = plan_scene(beats)
    prompt = compile_shot_prompt(plan.shots[1])
    assert "lead room to frame-right" in prompt


def test_compile_scene_prompts_one_per_shot() -> None:
    plan = plan_scene(_scene_beats())
    prompts = compile_scene_prompts(plan, subject="the detective")
    assert len(prompts) == len(plan.shots)
    assert all(isinstance(p, str) and p for p in prompts)


# --------------------------------------------------------------------------- #
# Lens / grade continuity guard
# --------------------------------------------------------------------------- #


def _shot(
    ordinal: int, *, lens: str, grade: str, size: str = "medium", mood: str = "neutral"
) -> ShotPlan:
    """A bare ShotPlan for exercising the look-jump guard directly."""
    profile = select_style_profile([], override="classical_balanced")
    return ShotPlan(
        ordinal=ordinal,
        beat_id=f"b{ordinal}",
        shot_size=size,
        camera=camera_for_beat(ordinal, _beat("x"), profile),
        lens=lens,
        lighting="soft",
        grade=grade,
        screen_direction=ScreenDirection.NEUTRAL,
        blocking=block_subjects([_beat("x")])[0],
        mood=mood,
        length_s=5.0,
    )


def _bare_scene(shots: list[ShotPlan]) -> ScenePlan:
    return ScenePlan(
        profile=select_style_profile([], override="classical_balanced"),
        genre=Genre.NEUTRAL,
        shots=shots,
    )


def test_detect_look_jumps_clean_on_consistent_scene() -> None:
    # Same size, same mood, same lens + grade → no jumps.
    shots = [
        _shot(0, lens="35mm", grade="muted"),
        _shot(1, lens="35mm", grade="muted"),
    ]
    assert detect_look_jumps(_bare_scene(shots)) == []


def test_detect_look_jumps_flags_unmotivated_lens_pop() -> None:
    # Same size + mood, but the lens changed → an unmotivated focal-length pop.
    shots = [
        _shot(0, lens="35mm", grade="muted"),
        _shot(1, lens="85mm", grade="muted"),
    ]
    jumps = detect_look_jumps(_bare_scene(shots))
    assert any(j.kind is LookJumpKind.LENS for j in jumps)


def test_detect_look_jumps_lens_change_motivated_by_size() -> None:
    # A medium → close insert is *expected* to change lens (motivated, no flag).
    shots = [
        _shot(0, lens="35mm", grade="muted", size="medium"),
        _shot(1, lens="85mm", grade="muted", size="close"),
    ]
    lens_jumps = [j for j in detect_look_jumps(_bare_scene(shots)) if j.kind is LookJumpKind.LENS]
    assert lens_jumps == []


def test_detect_look_jumps_grade_change_motivated_by_mood() -> None:
    # A grade change is fine when the mood flips; flagged when the mood holds.
    motivated = [
        _shot(0, lens="35mm", grade="muted", mood="calm"),
        _shot(1, lens="35mm", grade="warm golden", mood="triumphant"),
    ]
    grade_jumps = [
        j for j in detect_look_jumps(_bare_scene(motivated)) if j.kind is LookJumpKind.GRADE
    ]
    assert grade_jumps == []
    unmotivated = [
        _shot(0, lens="35mm", grade="muted", mood="calm"),
        _shot(1, lens="35mm", grade="warm golden", mood="calm"),
    ]
    assert any(
        j.kind is LookJumpKind.GRADE for j in detect_look_jumps(_bare_scene(unmotivated))
    )


# --------------------------------------------------------------------------- #
# Negative-prompt grammar
# --------------------------------------------------------------------------- #


def test_negative_prompt_carries_base_floor_and_genre_rules() -> None:
    noir = negative_prompt_for([], genre=Genre.NOIR)
    assert "extra fingers" in noir  # base floor
    assert "bright daylight" in noir  # noir-specific look-breaker
    fantasy = negative_prompt_for([], genre=Genre.FANTASY)
    assert "modern objects" in fantasy
    # No duplicate entries.
    parts = noir.split(", ")
    assert len(parts) == len(set(parts))


def test_negative_prompt_infers_genre_from_beats() -> None:
    neg = negative_prompt_for([_beat("a frantic chase, a fight, they sprint")])
    assert "motion smear" in neg  # the action genre's rule fired


# --------------------------------------------------------------------------- #
# Expressive camera-move vocabulary
# --------------------------------------------------------------------------- #


def test_expressive_move_for_genre_mood() -> None:
    horror_tense = _beat("a tense, dread-filled approach in the dark")
    assert expressive_move_for(horror_tense, Genre.HORROR) == "slow_dolly_zoom"
    romance_tender = _beat("a tender, loving moment")
    assert expressive_move_for(romance_tender, Genre.ROMANCE) == "gentle_orbit"
    # No mapped (genre, mood) → None (fall back to the profile default).
    assert expressive_move_for(_beat("a person walks"), Genre.NEUTRAL) is None


def test_move_phrase_renders_expressive_and_default() -> None:
    assert "dolly-zoom" in move_phrase("slow_dolly_zoom")
    assert move_phrase("push_in", "slow") == "slow push-in"  # speed prefix, not doubled
    assert move_phrase("push_in", "medium") == "push-in"  # medium speed → no prefix
    assert move_phrase("static", "fast") == "locked-off static frame"  # static ignores speed


def test_plan_scene_applies_expressive_move_for_horror() -> None:
    beats = [
        Beat(beat_id="b0", scene_id="s", summary="the wide haunted corridor"),
        Beat(beat_id="b1", scene_id="s", summary="a tense, dread-filled creep toward the door"),
    ]
    plan = plan_scene(beats)
    assert plan.genre is Genre.HORROR
    # The non-opening tense beat gets the dolly-zoom; the prompt reflects it.
    assert plan.shots[1].camera.move == "slow_dolly_zoom"
    prompt = compile_shot_prompt(plan.shots[1])
    assert "dolly-zoom" in prompt


# --------------------------------------------------------------------------- #
# Style-note → profile override (the §8.6 cinematographer-side bridge)
# --------------------------------------------------------------------------- #


def test_infer_style_override_maps_named_looks() -> None:
    assert infer_style_override("shoot it like noir, hard shadows") == "noir_chiaroscuro"
    assert infer_style_override("more symmetrical, Wes Anderson style") == "anamorphic_symmetry"
    assert infer_style_override("make it dreamy and romantic") == "romantic_soft"
    assert infer_style_override("slower please") is None  # an axis ask, not a look


def test_style_override_re_shoots_through_the_named_eye() -> None:
    # A chase scene defaults to kinetic_action, but a noir style note re-shoots it.
    beats = [_beat("a frantic chase, they sprint")]
    override = infer_style_override("shoot it like film noir")
    profile = select_style_profile(beats, override=override)
    assert profile.name == "noir_chiaroscuro"


# --------------------------------------------------------------------------- #
# Transition grammar
# --------------------------------------------------------------------------- #


def test_transition_between_reads_text_cues() -> None:
    calm = _beat("a calm, quiet morning by the window")
    later = _beat("hours pass; the room is dark")
    assert transition_between(calm, later) is Transition.DISSOLVE
    fade = _beat("everything went black; the chapter closes")
    assert transition_between(calm, fade) is Transition.FADE_TO_BLACK
    tense = _beat("a sudden tense scream of danger")
    # calm → tense with no time/fade cue is a smash cut.
    assert transition_between(calm, tense) is Transition.SMASH_CUT
    plain = _beat("she crosses the room")
    assert transition_between(calm, plain) is Transition.CUT


def test_transition_seconds_and_plan() -> None:
    assert transition_seconds(Transition.CUT) == 0.0
    assert transition_seconds(Transition.DISSOLVE) > 0.0
    beats = [_beat("a calm morning"), _beat("hours pass, dusk falls"), _beat("she walks on")]
    transitions = plan_transitions(beats)
    assert transitions[0] is Transition.FADE_FROM_BLACK  # the film opens
    assert transitions[1] is Transition.DISSOLVE  # the time jump
    assert len(transitions) == len(beats)


def test_plan_transitions_empty() -> None:
    assert plan_transitions([]) == []
