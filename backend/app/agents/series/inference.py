"""Deterministic arc-beat inference from structured plan signals (§7, §8.4).

The arc/pacing layer is a **read model** (``docs/design/showrunner-series.md``):
it must be derivable from signals a scene plan / canon already expose, with *no
new ingest*. This module is that derivation — pure heuristics that turn a beat's
mood word, narrative position, and dramatic cues into an :class:`~app.agents.contracts.ArcBeat`
(its arc stage + 0..1 intensity).

The heuristics are intentionally simple, explainable, and pre-registered (like the
§9.5 Critic thresholds): a *mood lexicon* maps tone words to a base intensity, and
the *position* within the volume biases the stage along the canonical arc ordering
(early beats lean setup/rising, late beats lean climax/resolution). A real
deployment can refine these with the model, but the read model must stand alone —
so it does, deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agents.contracts import (
    ArcBeat,
    ArcStage,
    Beat,
    ScenePlanItem,
    SourceSpan,
)

#: Mood words → base intensity (0..1). Calm tones sit low, charged tones high.
#: Pre-registered; lowercase substring match so "very tense" still matches "tense".
_MOOD_INTENSITY: dict[str, float] = {
    "calm": 0.15,
    "peaceful": 0.15,
    "serene": 0.15,
    "quiet": 0.2,
    "gentle": 0.2,
    "wistful": 0.3,
    "melancholy": 0.35,
    "somber": 0.4,
    "tense": 0.7,
    "anxious": 0.7,
    "suspense": 0.75,
    "fearful": 0.75,
    "dread": 0.8,
    "violent": 0.85,
    "furious": 0.85,
    "desperate": 0.85,
    "climactic": 0.95,
    "shocking": 0.95,
    "triumphant": 0.8,
    "tragic": 0.85,
    "romantic": 0.55,
    "tender": 0.45,
    "joyful": 0.5,
    "hopeful": 0.45,
    "mysterious": 0.6,
    "ominous": 0.7,
}

#: Action/cue words in a summary that nudge intensity up regardless of mood.
_ACTION_CUES: tuple[str, ...] = (
    "battle",
    "fight",
    "death",
    "dies",
    "killed",
    "betray",
    "escape",
    "chase",
    "confront",
    "reveal",
    "discover",
    "explosion",
    "storm",
    "duel",
    "sacrifice",
)

_DEFAULT_INTENSITY = 0.4


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def mood_intensity(mood: str | None) -> float:
    """Base intensity for a mood word via the pre-registered lexicon (§7)."""
    if not mood:
        return _DEFAULT_INTENSITY
    low = mood.lower()
    for key, value in _MOOD_INTENSITY.items():
        if key in low:
            return value
    return _DEFAULT_INTENSITY


def cue_boost(summary: str | None) -> float:
    """An additive intensity bump from action cues in a beat summary (capped)."""
    if not summary:
        return 0.0
    low = summary.lower()
    hits = sum(1 for cue in _ACTION_CUES if cue in low)
    return min(0.25, 0.1 * hits)


def stage_for_position(position: float) -> ArcStage:
    """Bias the arc stage by where the beat sits in the volume, in ``[0, 1]`` (§7).

    A coarse, monotone mapping: the opening establishes (setup), the body builds
    (rising) and turns, the back third peaks (climax) and resolves. Position is the
    beat's fractional index within the volume.
    """
    p = _clamp01(position)
    if p < 0.15:
        return ArcStage.SETUP
    if p < 0.5:
        return ArcStage.RISING
    if p < 0.65:
        return ArcStage.TURN
    if p < 0.85:
        return ArcStage.CLIMAX
    if p < 0.95:
        return ArcStage.FALLING
    return ArcStage.RESOLUTION


def infer_arc_beat(
    *,
    volume_index: int,
    beat_index: int,
    position: float,
    mood: str | None = None,
    summary: str = "",
    source_span: SourceSpan | None = None,
) -> ArcBeat:
    """Infer one :class:`ArcBeat` from a beat's mood, summary and position (§7).

    ``intensity`` = mood base + action-cue boost; ``stage`` from the position. Pure.
    """
    intensity = _clamp01(mood_intensity(mood) + cue_boost(summary))
    return ArcBeat(
        volume_index=volume_index,
        beat_index=beat_index,
        stage=stage_for_position(position),
        intensity=intensity,
        summary=summary,
        source_span=source_span or SourceSpan(),
    )


def infer_arc_from_beats(
    beats: Sequence[Beat],
    *,
    volume_index: int = 0,
) -> list[ArcBeat]:
    """Infer a volume's arc beats from the Adapter's :class:`Beat` list (§7, §8.4).

    Positions are the beat's fractional index over the run; mood/summary feed the
    intensity. This is the bridge from the existing per-page ingest output to the
    series read model — no new ingest, exactly as §8.4 demands.
    """
    n = len(beats)
    if n == 0:
        return []
    out: list[ArcBeat] = []
    for i, beat in enumerate(beats):
        position = i / (n - 1) if n > 1 else 0.0
        out.append(
            infer_arc_beat(
                volume_index=volume_index,
                beat_index=beat.beat_index or i,
                position=position,
                mood=beat.mood,
                summary=beat.summary,
                source_span=beat.source_span,
            )
        )
    return out


def infer_character_arc_across_volumes(
    volume_beats: Mapping[int, Sequence[Beat]],
) -> list[ArcBeat]:
    """Infer a *cross-book* character arc that progresses over the whole series (§7).

    Unlike :func:`infer_arc_from_beats` (which scopes the arc stage to position
    *within one volume*), a character's series-long arc must advance across the
    volumes: Volume 1 establishes and rises, the final volume climaxes and
    resolves. This maps every beat onto a **series-global** position — the volume's
    span concatenated end to end — so the resolved stage climbs monotonically and
    :func:`app.agents.series.arcs.is_monotonic` holds for a well-formed series.

    Intensity is still local to the beat (its mood/cues), only the *stage* uses the
    global position. Returns the arc beats in series order.
    """
    if not volume_beats:
        return []
    ordered_volumes = sorted(volume_beats.items())
    total = sum(len(b) for _, b in ordered_volumes)
    if total == 0:
        return []
    out: list[ArcBeat] = []
    cursor = 0
    for volume_index, beats in ordered_volumes:
        for i, beat in enumerate(beats):
            position = cursor / (total - 1) if total > 1 else 0.0
            cursor += 1
            intensity = _clamp01(mood_intensity(beat.mood) + cue_boost(beat.summary))
            out.append(
                ArcBeat(
                    volume_index=volume_index,
                    beat_index=beat.beat_index or i,
                    stage=stage_for_position(position),
                    intensity=intensity,
                    summary=beat.summary,
                    source_span=beat.source_span or SourceSpan(),
                )
            )
    return out


def infer_scene_tensions(
    scenes: Sequence[ScenePlanItem],
    *,
    moods: dict[int, str] | None = None,
) -> dict[int, float]:
    """Infer a ``{scene_index: tension}`` map for the planner (§7).

    Uses an explicit ``tension`` on the scene if present, else a mood lookup, else
    a positional default that rises toward the back of the volume. Pure.
    """
    moods = moods or {}
    ordered = sorted(scenes, key=lambda s: s.scene_index)
    n = len(ordered)
    out: dict[int, float] = {}
    for i, scene in enumerate(ordered):
        if scene.tension is not None:
            out[scene.scene_index] = _clamp01(scene.tension)
            continue
        mood = moods.get(scene.scene_index)
        if mood is not None:
            out[scene.scene_index] = _clamp01(mood_intensity(mood) + cue_boost(scene.summary))
            continue
        # Positional default: a gentle ramp peaking in the back third.
        position = i / (n - 1) if n > 1 else 0.0
        out[scene.scene_index] = _clamp01(0.2 + 0.7 * position)
    return out


__all__ = [
    "cue_boost",
    "infer_arc_beat",
    "infer_arc_from_beats",
    "infer_character_arc_across_volumes",
    "infer_scene_tensions",
    "mood_intensity",
    "stage_for_position",
]
