"""Music scoring — map a scene's mood/palette to a deterministic score cue (§9.6).

The §9.6 stitch ships a scene's audio; today that is only narration. A *scored*
scene also carries a music bed under the narration. This module owns the **cue
selection** half of that — a pure, deterministic mapping from a beat's free-text
``mood`` (and the canon ``palette``) onto a :class:`ScoreCue`: a musical key, mode,
tempo, intensity, and instrument colour. The actual mix (looping the bed to length,
ducking it under speech, mastering) is :mod:`app.render.audio_post`'s job.

Why a *generated* cue rather than a library of mp3s? Two reasons that matter for
public release (§17): a procedurally-described cue is **copyright-clean** (no
third-party recording), and it is **deterministic** — the same mood always scores
the same way, so a re-render of a scene produces an identical bed (cache-friendly,
diffable in tests). The cue is rendered to real audio by
:func:`app.render.audio_post.render_score_bed` using ffmpeg's synth sources; this
module never touches ffmpeg, so the taxonomy stays exhaustively unit-testable.

The mood vocabulary is intentionally small and fuzzy-matched: a beat's ``mood`` is
prose ("a tense, hushed confrontation"), so :func:`classify_mood` scores it against
keyword sets and picks the best — never raises, always lands on a real cue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Tokeniser shared with the rest of the render layer's text handling.
_WORD_RE = re.compile(r"[a-z]+")


class Mood(StrEnum):
    """The score moods a scene can land on (each maps to one :class:`ScoreCue`)."""

    CALM = "calm"
    TENSE = "tense"
    TENDER = "tender"
    TRIUMPHANT = "triumphant"
    SOMBRE = "sombre"
    WONDROUS = "wondrous"
    PLAYFUL = "playful"
    NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class ScoreCue:
    """A fully-resolved, copyright-clean musical cue for a scene bed.

    Attributes:
        mood: the classified :class:`Mood`.
        root_hz: tonic frequency of the bed's drone/pad (a real pitch).
        mode: ``"major"`` / ``"minor"`` (selects the third/colour of the chord).
        tempo_bpm: pulse the bed breathes at (slow pad swell rate).
        intensity: 0..1 — how present the bed is in the final mix (drives gain).
        timbre: a short instrument-colour label (telemetry / future hosted gen).
        chord_hz: the (root, third, fifth) frequencies the bed stacks.
    """

    mood: Mood
    root_hz: float
    mode: str
    tempo_bpm: int
    intensity: float
    timbre: str
    chord_hz: tuple[float, float, float]


#: Keyword sets per mood. Fuzzy, additive — overlapping words just raise scores.
_MOOD_KEYWORDS: dict[Mood, frozenset[str]] = {
    Mood.CALM: frozenset(
        {"calm", "quiet", "still", "peaceful", "serene", "gentle", "hushed", "soft", "restful"}
    ),
    Mood.TENSE: frozenset(
        {"tense", "fear", "afraid", "danger", "threat", "dread", "anxious", "menacing", "urgent",
         "panic", "confrontation", "chase"}
    ),
    Mood.TENDER: frozenset(
        {"tender", "love", "warm", "intimate", "longing", "embrace", "fond", "caring", "kind"}
    ),
    Mood.TRIUMPHANT: frozenset(
        {"triumphant", "victory", "heroic", "soar", "rise", "glorious", "win", "celebration",
         "bold"}
    ),
    Mood.SOMBRE: frozenset(
        {"sombre", "somber", "grief", "sorrow", "mourning", "loss", "sad", "lonely", "bleak",
         "funeral", "despair"}
    ),
    Mood.WONDROUS: frozenset(
        {"wonder", "wondrous", "awe", "magic", "magical", "mystery", "dream", "enchanted",
         "ethereal", "vast"}
    ),
    Mood.PLAYFUL: frozenset(
        {"playful", "joy", "happy", "merry", "light", "fun", "whimsical", "lively", "bright"}
    ),
}

#: One canonical cue per mood. Frequencies are real equal-temperament pitches.
#: Minor modes flatten the third (~A3/C4 stacks); intensity rises with arousal.
_MOOD_CUES: dict[Mood, ScoreCue] = {
    Mood.CALM: ScoreCue(
        Mood.CALM, root_hz=220.0, mode="major", tempo_bpm=60, intensity=0.28,
        timbre="warm pad", chord_hz=(220.0, 277.18, 329.63),
    ),
    Mood.TENSE: ScoreCue(
        Mood.TENSE, root_hz=146.83, mode="minor", tempo_bpm=92, intensity=0.5,
        timbre="low drone", chord_hz=(146.83, 174.61, 220.0),
    ),
    Mood.TENDER: ScoreCue(
        Mood.TENDER, root_hz=261.63, mode="major", tempo_bpm=66, intensity=0.34,
        timbre="soft strings", chord_hz=(261.63, 329.63, 392.0),
    ),
    Mood.TRIUMPHANT: ScoreCue(
        Mood.TRIUMPHANT, root_hz=196.0, mode="major", tempo_bpm=104, intensity=0.6,
        timbre="brass swell", chord_hz=(196.0, 246.94, 293.66),
    ),
    Mood.SOMBRE: ScoreCue(
        Mood.SOMBRE, root_hz=174.61, mode="minor", tempo_bpm=54, intensity=0.32,
        timbre="cello drone", chord_hz=(174.61, 207.65, 261.63),
    ),
    Mood.WONDROUS: ScoreCue(
        Mood.WONDROUS, root_hz=293.66, mode="major", tempo_bpm=72, intensity=0.42,
        timbre="glass bells", chord_hz=(293.66, 369.99, 440.0),
    ),
    Mood.PLAYFUL: ScoreCue(
        Mood.PLAYFUL, root_hz=329.63, mode="major", tempo_bpm=120, intensity=0.46,
        timbre="plucked", chord_hz=(329.63, 415.30, 493.88),
    ),
    Mood.NEUTRAL: ScoreCue(
        Mood.NEUTRAL, root_hz=220.0, mode="major", tempo_bpm=72, intensity=0.3,
        timbre="neutral pad", chord_hz=(220.0, 277.18, 329.63),
    ),
}

#: When the palette is known it nudges intensity/timbre without overriding the
#: mood — a "warm" palette lifts the bed a touch, a "cool" one pulls it back.
_PALETTE_INTENSITY_DELTA: dict[str, float] = {"warm": 0.04, "cool": -0.04}


@dataclass(frozen=True, slots=True)
class MoodScores:
    """Per-mood keyword scores for a piece of mood text (debug / explainability)."""

    scores: dict[Mood, int] = field(default_factory=dict)

    @property
    def best(self) -> Mood:
        if not self.scores or max(self.scores.values(), default=0) == 0:
            return Mood.NEUTRAL
        # Deterministic tie-break: highest score, then the StrEnum declaration order.
        order = list(Mood)
        return max(self.scores, key=lambda m: (self.scores[m], -order.index(m)))


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def score_mood_keywords(mood_text: str | None) -> MoodScores:
    """Count keyword hits per mood for ``mood_text`` (pure; never raises)."""
    tokens = set(_tokens(mood_text or ""))
    scores: dict[Mood, int] = {}
    for mood, keywords in _MOOD_KEYWORDS.items():
        hits = len(tokens & keywords)
        if hits:
            scores[mood] = hits
    return MoodScores(scores=scores)


def classify_mood(mood_text: str | None) -> Mood:
    """Classify free-text scene mood into a :class:`Mood` (defaults to NEUTRAL)."""
    return score_mood_keywords(mood_text).best


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_scene(
    *,
    mood_text: str | None = None,
    palette: str | None = None,
    intensity_override: float | None = None,
) -> ScoreCue:
    """Resolve a scene's :class:`ScoreCue` from its mood + palette (pure).

    The mood selects the base cue; the palette nudges intensity; an explicit
    ``intensity_override`` (e.g. a learned per-reader "quieter music" preference,
    Phase 11) wins outright. Always returns a real cue — unknown mood → NEUTRAL.
    """
    mood = classify_mood(mood_text)
    base = _MOOD_CUES[mood]
    if intensity_override is not None:
        intensity = _clamp01(intensity_override)
    else:
        delta = _PALETTE_INTENSITY_DELTA.get((palette or "").strip().lower(), 0.0)
        intensity = _clamp01(base.intensity + delta)
    if intensity == base.intensity:
        return base
    return ScoreCue(
        mood=base.mood,
        root_hz=base.root_hz,
        mode=base.mode,
        tempo_bpm=base.tempo_bpm,
        intensity=intensity,
        timbre=base.timbre,
        chord_hz=base.chord_hz,
    )


def cue_for_mood(mood: Mood) -> ScoreCue:
    """The canonical cue for an already-classified :class:`Mood`."""
    return _MOOD_CUES[mood]


__all__ = [
    "Mood",
    "MoodScores",
    "ScoreCue",
    "classify_mood",
    "cue_for_mood",
    "score_mood_keywords",
    "score_scene",
]
