"""The deep literary-comprehension engine — composes every pass (§4.2, §9.1, §10).

This is the single entry point the Adapter uses to turn a raw, single-pass beat
(``summary`` / ``described_visuals`` / ``mood`` / ``entities`` from the LLM) into
a *deeply comprehended* beat: POV + unreliable-narrator flag, discourse mode +
interiority, attributed dialogue, literary-device → visual intent, and pacing
tempo. It also runs the book-level **non-linear timeline reconstruction** across
a whole beat sequence so flashbacks/flash-forwards carry a story-time rank.

Everything here is PURE and network-free: it composes the deterministic passes
in :mod:`app.agents.comprehension`. An LLM enrichment pass can later override any
field, but the engine alone produces a fully-populated, testable comprehension
without a single model call — the §10 testability discipline applied end-to-end.

Design: per-beat passes (``analyze_beat``) are independent and order-free;
cross-beat structure (timeline, POV continuity) is a second ``enrich_sequence``
pass so the engine stays a clean two-phase pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agents.contracts import Beat, SceneTempo, ShotIntent, StoryTime

from . import devices as devices_mod
from . import dialogue as dialogue_mod
from . import discourse as discourse_mod
from . import pacing as pacing_mod
from . import pov as pov_mod
from . import timeline as timeline_mod

#: Human-readable pacing hint per tempo (surfaced to the Cinematographer).
_PACING_HINT: dict[SceneTempo, str] = {
    SceneTempo.SCENE: "real-time, brisk cutting",
    SceneTempo.PAUSE: "held, lingering — let the frame breathe",
    SceneTempo.SUMMARY: "compressed — one shot spans a long passage of time",
    SceneTempo.ELLIPSIS: "a single transition shot across a time-jump",
}


def _beat_text(beat: Beat) -> str:
    """The text a beat exposes for analysis (summary + described visuals).

    Beats carry no raw source text post-LLM, so comprehension reads the summary
    and described-visuals the Adapter produced. The ingest reconciler still keys
    spans off the real extracted words; this is the *meaning* surface.
    """
    parts = [beat.summary or ""]
    if beat.described_visuals:
        parts.append(beat.described_visuals)
    return " ".join(p for p in parts if p).strip()


def analyze_beat(
    beat: Beat,
    *,
    canon_names: Mapping[str, str] | set[str] | None = None,
) -> Beat:
    """Enrich ONE beat with all per-beat literary comprehension (pure).

    Returns a copy of ``beat`` with ``pov``/``pov_character``/``unreliable``/
    ``discourse``/``interiority``/``dialogue``/``devices``/``tempo`` filled.
    ``story_time`` is left to :func:`enrich_sequence` (it needs neighbours).
    Never invents entities: dialogue speakers and focal characters are filtered
    against ``canon_names`` when supplied (§10).
    """
    text = _beat_text(beat)
    if not text:
        return beat

    pov_a = pov_mod.classify_pov(text, canon_names=canon_names)
    disc_a = discourse_mod.classify_discourse(text)
    attrs = dialogue_mod.attribute_dialogue(text, canon_names=canon_names)
    devs = devices_mod.detect_devices(text)
    pace = pacing_mod.classify_tempo(text)

    return beat.model_copy(
        update={
            "pov": pov_a.person,
            "pov_character": pov_a.focal_character,
            "unreliable": pov_a.unreliable,
            "discourse": disc_a.mode,
            "interiority": disc_a.interiority,
            "dialogue": dialogue_mod.to_dialogue_lines(attrs),
            "devices": devs,
            "tempo": pace.tempo,
        }
    )


def enrich_sequence(
    beats: Sequence[Beat],
    *,
    canon_names: Mapping[str, str] | set[str] | None = None,
) -> list[Beat]:
    """Deeply comprehend a whole ordered beat sequence (per-beat + cross-beat).

    Two phases:

    1. per-beat :func:`analyze_beat` over every beat;
    2. book-level :mod:`.timeline` reconstruction so each beat gets a
       :class:`StoryTime` (narrative-order vs story-order, flashback/forward
       position, the matched temporal marker).

    The returned beats preserve narrative order (the scroll-sync key); only their
    ``story_time.order`` differs when the prose is non-linear.
    """
    analyzed = [analyze_beat(b, canon_names=canon_names) for b in beats]

    timed = [
        timeline_mod.TimedBeat(
            beat_id=b.beat_id or f"beat_{i:04d}",
            narrative_order=b.beat_index if b.beat_index else i,
            text=_beat_text(b),
        )
        for i, b in enumerate(analyzed)
    ]
    reconstructed = timeline_mod.reconstruct_timeline(timed)

    out: list[Beat] = []
    for beat, recon in zip(analyzed, reconstructed, strict=True):
        story = StoryTime(
            position=recon.position,
            order=recon.story_order,
            narrative_order=recon.narrative_order,
            marker=recon.marker,
        )
        out.append(beat.model_copy(update={"story_time": story}))
    return out


def build_shot_intent(beat: Beat) -> ShotIntent:
    """Distil a comprehended beat into a structured :class:`ShotIntent` (pure).

    The intent is the Adapter's hand-off to the Cinematographer: the staging
    decisions that fall out of *comprehension* (literal vs subjective framing,
    POV vantage, unreliability, device motifs, speakers, pacing) — separate from
    the cinematographer's own creative fill (prompt/camera/seed). Carries no
    invented entities; every name was resolved from the text (§10).
    """
    subjective = discourse_mod.is_subjective(beat.discourse)
    motifs = [d.visual_intent for d in beat.devices if d.visual_intent]
    speakers = list(dict.fromkeys(d.speaker for d in beat.dialogue if d.speaker))
    pacing = _PACING_HINT.get(beat.tempo, "")
    intent = ShotIntent(
        subjective=subjective,
        pov_character=beat.pov_character,
        unreliable=beat.unreliable,
        visual_motifs=motifs,
        speakers=speakers,
        pacing=pacing,
    )
    return intent.model_copy(update={"brief": _brief_for(intent)})


def _brief_for(intent: ShotIntent) -> str:
    """Assemble the one-line natural-language brief from a structured intent."""
    parts: list[str] = []
    if intent.subjective:
        parts.append(
            "render as a SUBJECTIVE image (the character's inner view), not a "
            "literal exterior action"
        )
    if intent.pov_character:
        parts.append(f"told from {intent.pov_character}'s point of view")
    if intent.unreliable:
        parts.append("the narration is unreliable — stage it as a coloured/biased view")
    if intent.visual_motifs:
        parts.append("; ".join(intent.visual_motifs))
    if intent.speakers:
        parts.append(f"dialogue between {', '.join(intent.speakers)}")
    if intent.pacing:
        parts.append(f"pacing: {intent.pacing}")
    return "; ".join(parts)


def shot_intent(beat: Beat) -> str:
    """A one-line creative brief distilled from a beat's comprehension.

    Thin wrapper over :func:`build_shot_intent` returning just the assembled
    natural-language brief (kept for callers that only want the string).
    """
    return build_shot_intent(beat).brief


__all__ = ["analyze_beat", "build_shot_intent", "enrich_sequence", "shot_intent"]
