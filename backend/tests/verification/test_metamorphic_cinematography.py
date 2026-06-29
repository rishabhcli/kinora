"""Metamorphic + property tests for the Cinematographer's pure cinematic-language layer.

The §9.3/§10 Cinematographer pins the film's *look* deterministically before the
LLM fills prose, so these pure helpers must be stable and safe:

* ``infer_genre`` / ``negative_prompt_for`` — deterministic; the genre negative
  floor is always a superset of the universal base floor (a noir clip can never
  *drop* the universal artifact bans);
* ``Cinematographer._merge_negative`` — the deterministic floor is **always
  present** in the merged negative prompt no matter what the model adds or drops
  (the §9.3 "model may add, never drop" guarantee), and it de-dups;
* ``style_override_from_notes`` — the *last* look-naming note wins (latest ask),
  and a note that names no look leaves the prefs path alone (returns None).

These bind the agent layer's deterministic floor to the metamorphic relations the
look-consistency story depends on.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.agents.cinematographer import Cinematographer, style_override_from_notes
from app.agents.contracts import Beat, DirectorNote
from app.render.cinematic_language import (
    Genre,
    infer_genre,
    infer_style_override,
    negative_prompt_for,
)

# A vocabulary mixing genre-cue and neutral beat summaries.
GENRE_WORDS = [
    "shadow", "blood", "spell", "laser", "kiss", "explosion", "ghost", "castle",
    "the quiet room", "she walked", "a table", "",
]
beat_summaries = st.lists(st.sampled_from(GENRE_WORDS), max_size=8)


def _beats(words: list[str]) -> list[Beat]:
    return [Beat(beat_id=f"b{i}", summary=w) for i, w in enumerate(words)]


# --------------------------------------------------------------------------- #
# infer_genre + negative_prompt_for
# --------------------------------------------------------------------------- #


@given(beat_summaries)
def test_infer_genre_is_deterministic(words: list[str]) -> None:
    beats = _beats(words)
    assert infer_genre(beats) == infer_genre(beats)
    assert infer_genre(beats) in set(Genre)


@given(beat_summaries)
def test_genre_floor_is_a_superset_of_the_base_floor(words: list[str]) -> None:
    """Any genre's negative floor contains every universal (NEUTRAL) artifact ban.

    A genre may *add* look-breakers but can never drop a universal one — so the
    base floor's terms are a subset of any genre floor's terms.
    """
    base_terms = set(_terms(negative_prompt_for(_beats([]), genre=Genre.NEUTRAL)))
    for genre in Genre:
        genre_terms = set(_terms(negative_prompt_for(_beats(words), genre=genre)))
        assert base_terms <= genre_terms


@given(beat_summaries)
def test_negative_prompt_has_no_duplicate_terms(words: list[str]) -> None:
    """The floor de-dups: a genre rule echoing a base one appears once."""
    for genre in Genre:
        terms = _terms(negative_prompt_for(_beats(words), genre=genre))
        assert len(terms) == len(set(terms))


def _terms(prompt: str) -> list[str]:
    return [t.strip() for t in prompt.split(",") if t.strip()]


# --------------------------------------------------------------------------- #
# Cinematographer._merge_negative — floor is never dropped
# --------------------------------------------------------------------------- #

model_negatives = st.lists(
    st.sampled_from(["blurry", "extra fingers", "noir-look", "watermark", "", "low-res"]),
    max_size=6,
).map(lambda parts: ", ".join(parts))

floors = st.sampled_from(
    [
        "lowres, artifacts",
        "deformed, extra limbs, flicker",
        "modern objects, cars, neon",
        "",
    ]
)


@given(model_negatives, floors)
def test_merge_always_keeps_every_floor_term(model_negative: str, floor: str) -> None:
    """Metamorphic (§9.3): whatever the model says, every floor term survives the merge."""
    merged = Cinematographer._merge_negative(model_negative, floor)
    merged_terms = set(_terms(merged))
    for term in _terms(floor):
        assert term in merged_terms


@given(model_negatives, floors)
def test_merge_is_floor_first_and_dedups(model_negative: str, floor: str) -> None:
    """The floor terms lead (floor-first order) and the union has no duplicates."""
    merged_terms = _terms(Cinematographer._merge_negative(model_negative, floor))
    # No duplicates (case-insensitive de-dup in the impl).
    lowered = [t.lower() for t in merged_terms]
    assert len(lowered) == len(set(lowered))
    # Floor terms appear, in order, before any model-only term.
    floor_terms = _terms(floor)
    if floor_terms:
        positions = [lowered.index(t.lower()) for t in floor_terms]
        assert positions == sorted(positions)


@given(model_negatives, floors)
def test_merge_is_idempotent_on_the_floor(model_negative: str, floor: str) -> None:
    """Re-merging an already-merged prompt with the same floor adds nothing new."""
    once = Cinematographer._merge_negative(model_negative, floor)
    twice = Cinematographer._merge_negative(once, floor)
    assert set(_terms(twice)) == set(_terms(once))


# --------------------------------------------------------------------------- #
# style_override_from_notes — last look-naming note wins
# --------------------------------------------------------------------------- #

# Phrases that ``infer_style_override`` genuinely recognises as a *look* (verified
# against the lexicon — see test_metamorphic_cinematography setup).
LOOK_NOTES = ["shoot it like noir", "noir", "make it symmetrical", "wes anderson"]
# Axis-only asks that name no look (they stay on the §8.6 prefs path → None).
AXIS_NOTES = ["slower", "warmer", "wider", "make the coat red"]


@st.composite
def director_note_runs(draw: st.DrawFn) -> list[DirectorNote]:
    texts = draw(st.lists(st.sampled_from(LOOK_NOTES + AXIS_NOTES), max_size=6))
    return [DirectorNote(note=t) for t in texts]


@given(director_note_runs())
def test_style_override_matches_last_look_naming_note(
    notes: list[DirectorNote],
) -> None:
    """The most recent note that names a *look* wins; axis-only notes return None."""
    override = style_override_from_notes(notes)
    # Recompute the expected: scan in reverse for the first look-naming note.
    expected = None
    for note in reversed(notes):
        ov = infer_style_override(note.note)
        if ov is not None:
            expected = ov
            break
    assert override == expected


@given(st.lists(st.sampled_from(AXIS_NOTES), max_size=5))
def test_axis_only_notes_yield_no_override(texts: list[str]) -> None:
    """A run of pure-axis notes ('slower', 'warmer') never forces a look override."""
    notes = [DirectorNote(note=t) for t in texts]
    assert style_override_from_notes(notes) is None


@given(director_note_runs(), st.sampled_from(LOOK_NOTES))
def test_appending_a_look_note_overrides_prior_choice(
    notes: list[DirectorNote], last_look: str
) -> None:
    """Metamorphic: appending a look-note makes *that* note the winner (latest ask)."""
    extended = [*notes, DirectorNote(note=last_look)]
    assert style_override_from_notes(extended) == infer_style_override(last_look)
