"""Deep literary-comprehension engine for the Adapter (§4.2, §9.1, §10).

A package of PURE, network-free passes that turn a single-pass LLM beat into a
deeply comprehended one — multi-POV / unreliable-narrator handling, non-linear
timeline reconstruction (narrative-time vs story-time), free-indirect-discourse
and interiority detection, dialogue attribution + speaker diarization, and
literary-device → visual-intent translation, plus pacing-aware tempo that varies
shot density by scene rhythm.

The Adapter (:mod:`app.agents.adapter`) composes these via :func:`enrich_sequence`
to enrich the beats it persists; :func:`analyze_beat` enriches one beat in
isolation. Every pass is deterministic so the whole engine is unit-testable
without a model call (the §10 discipline), while an LLM enrichment pass can layer
on top and override individual fields.
"""

from __future__ import annotations

from .devices import detect_devices, visual_intent_summary
from .dialogue import (
    Attribution,
    attribute_dialogue,
    dialogue_density,
    to_dialogue_lines,
)
from .discourse import DiscourseAnalysis, classify_discourse, is_subjective
from .engine import analyze_beat, build_shot_intent, enrich_sequence, shot_intent
from .llm import BeatComprehension, merge_comprehension
from .pacing import (
    PacingAnalysis,
    classify_tempo,
    density_multiplier,
    duration_bias,
    words_per_shot_for,
)
from .pov import PovAnalysis, classify_pov, pov_changed
from .report import (
    ComprehensionReport,
    dominant_discourse,
    dominant_tempo,
    summarize_comprehension,
)
from .text_utils import (
    QuoteSpan,
    Sentence,
    extract_quotes,
    split_sentences,
    strip_quotes,
    titlecase_names,
    words,
)
from .timeline import (
    ReconstructedBeat,
    TimeCue,
    TimedBeat,
    classify_time_position,
    in_story_order,
    is_linear,
    reconstruct_timeline,
)

__all__ = [
    # engine
    "analyze_beat",
    "build_shot_intent",
    "enrich_sequence",
    "shot_intent",
    # llm refinement
    "BeatComprehension",
    "merge_comprehension",
    # report / telemetry
    "ComprehensionReport",
    "dominant_discourse",
    "dominant_tempo",
    "summarize_comprehension",
    # dialogue
    "Attribution",
    "attribute_dialogue",
    "dialogue_density",
    "to_dialogue_lines",
    # pov
    "PovAnalysis",
    "classify_pov",
    "pov_changed",
    # discourse
    "DiscourseAnalysis",
    "classify_discourse",
    "is_subjective",
    # devices
    "detect_devices",
    "visual_intent_summary",
    # pacing
    "PacingAnalysis",
    "classify_tempo",
    "density_multiplier",
    "duration_bias",
    "words_per_shot_for",
    # timeline
    "ReconstructedBeat",
    "TimeCue",
    "TimedBeat",
    "classify_time_position",
    "in_story_order",
    "is_linear",
    "reconstruct_timeline",
    # text utils
    "QuoteSpan",
    "Sentence",
    "extract_quotes",
    "split_sentences",
    "strip_quotes",
    "titlecase_names",
    "words",
]
