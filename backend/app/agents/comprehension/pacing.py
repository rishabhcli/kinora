"""Pacing-aware tempo classification + shot-density (§4.2 beat→shot).

Narrative pace is not uniform: a page of dramatised dialogue is *scene* time
(events unfold at roughly real-time, every beat earns coverage), while "the war
dragged on for three years" is *summary* (a long span compressed into a clause —
one establishing shot, not three years of footage). A faithful edit varies shot
density with this tempo so the film breathes like the prose.

This module classifies a beat's :class:`SceneTempo` from deterministic signals
(dialogue density, time-span markers, sensory/static description, action verbs)
and exposes a **density multiplier** the deterministic beat→shot splitter scales
by — keeping the split itself a pure function (the §4.2 discipline).

The classifier is heuristic and network-free; an LLM pass can refine the call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.contracts import SceneTempo

from .dialogue import dialogue_density
from .text_utils import split_sentences, words

#: Markers that a long span of time is being compressed → SUMMARY.
_SPAN_RE = re.compile(
    r"\b(?:for\s+)?(?:many|several|three|four|five|six|seven|ten|twenty|"
    r"a\s+(?:few|number\s+of))\s+(?:years|months|weeks|days|hours|seasons|winters|summers)\b"
    r"|\b(?:over|throughout|during)\s+the\s+(?:years|months|weeks|war|season|winter|summer)\b"
    r"|\b(?:for\s+)?(?:days|weeks|months|years)\s+(?:on\s+end|passed|went\s+by|dragged)\b",
    re.IGNORECASE,
)
#: Markers of an abrupt jump/cut in time → ELLIPSIS (a single transition shot).
_ELLIPSIS_RE = re.compile(
    r"\b(?:later\s+that|the\s+next\s+(?:day|morning|week|year)|hours?\s+later|"
    r"meanwhile|suddenly|at\s+last|finally,|eventually,|by\s+(?:nightfall|morning|noon)|"
    r"when\s+(?:she|he|they|i)\s+(?:awoke|woke|returned))\b",
    re.IGNORECASE,
)
#: Stillness / pure description cues → PAUSE (held, sparse coverage).
_STATIC = frozenset({
    "stood", "lay", "sat", "remained", "motionless", "still", "silent", "quiet",
    "stretched", "hung", "rested", "waited", "gazed", "stared", "watched",
    "loomed", "spread", "lined", "dotted", "scattered", "bathed", "shrouded",
})
#: Kinetic action verbs → SCENE (real-time, dense coverage).
_ACTION = frozenset({
    "ran", "leapt", "jumped", "struck", "grabbed", "threw", "fell", "rushed",
    "lunged", "slammed", "shouted", "screamed", "drew", "fired", "swung",
    "kicked", "dashed", "burst", "charged", "spun", "ducked", "dove", "crashed",
    "snatched", "hurled", "fought", "fled", "chased", "smashed", "tore",
})

#: Multiplier on the words-per-shot density per tempo. >1 ⇒ denser (more shots).
TEMPO_DENSITY: dict[SceneTempo, float] = {
    SceneTempo.SCENE: 1.0,  # baseline: ~one shot per WORDS_PER_SHOT
    SceneTempo.PAUSE: 0.6,  # description: fewer, longer-held shots
    SceneTempo.SUMMARY: 0.4,  # compression: one shot covers a long span
    SceneTempo.ELLIPSIS: 0.3,  # a jump: a single transition shot
}
#: Per-tempo target screen-seconds bias (a held PAUSE lingers; a SCENE is brisk).
TEMPO_DURATION_BIAS: dict[SceneTempo, float] = {
    SceneTempo.SCENE: 1.0,
    SceneTempo.PAUSE: 1.25,
    SceneTempo.SUMMARY: 1.1,
    SceneTempo.ELLIPSIS: 0.9,
}


@dataclass(frozen=True)
class PacingAnalysis:
    """A beat's tempo classification plus the signals that produced it."""

    tempo: SceneTempo
    dialogue_density: float
    action_density: float
    static_density: float


def classify_tempo(text: str) -> PacingAnalysis:
    """Classify a beat's pacing tempo from deterministic prose signals.

    Precedence: an explicit long-span marker ⇒ SUMMARY; a time-jump marker ⇒
    ELLIPSIS; otherwise the balance of dialogue/action (⇒ SCENE) against static
    description (⇒ PAUSE) decides. Empty text defaults to SCENE (neutral).
    """
    if not text.strip():
        return PacingAnalysis(SceneTempo.SCENE, 0.0, 0.0, 0.0)

    toks = words(text)
    n = max(1, len(toks))
    dlg = dialogue_density(text)
    action = sum(1 for t in toks if t in _ACTION) / n
    static = sum(1 for t in toks if t in _STATIC) / n

    if _SPAN_RE.search(text):
        tempo = SceneTempo.SUMMARY
    elif _ELLIPSIS_RE.search(text) and dlg < 0.3 and action < 0.05:
        tempo = SceneTempo.ELLIPSIS
    elif dlg >= 0.3 or action >= 0.04:
        tempo = SceneTempo.SCENE
    elif static >= 0.04 or _is_descriptive(text):
        tempo = SceneTempo.PAUSE
    else:
        tempo = SceneTempo.SCENE

    return PacingAnalysis(tempo, round(dlg, 3), round(action, 3), round(static, 3))


def _is_descriptive(text: str) -> bool:
    """Long sentences with few verbs of motion read as held description."""
    sents = split_sentences(text)
    if not sents:
        return False
    toks = words(text)
    avg_len = len(toks) / len(sents)
    motion = sum(1 for t in toks if t in _ACTION)
    return avg_len >= 18 and motion == 0


def density_multiplier(tempo: SceneTempo) -> float:
    """Words-per-shot density multiplier for a tempo (>1 ⇒ more shots)."""
    return TEMPO_DENSITY.get(tempo, 1.0)


def duration_bias(tempo: SceneTempo) -> float:
    """Per-shot screen-seconds bias for a tempo (a PAUSE lingers longer)."""
    return TEMPO_DURATION_BIAS.get(tempo, 1.0)


def words_per_shot_for(tempo: SceneTempo, base_words_per_shot: int) -> int:
    """How many narration words one shot covers at this tempo (≥ 1).

    Denser tempo ⇒ *fewer* words per shot ⇒ more shots over the same span. The
    base is divided by the density multiplier so SCENE keeps the baseline while
    SUMMARY/ELLIPSIS pack a long span into a single clip.
    """
    mult = density_multiplier(tempo)
    return max(1, round(base_words_per_shot / mult))


__all__ = [
    "TEMPO_DENSITY",
    "TEMPO_DURATION_BIAS",
    "PacingAnalysis",
    "classify_tempo",
    "density_multiplier",
    "duration_bias",
    "words_per_shot_for",
]
