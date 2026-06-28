"""Cinematographer — shot design + the §9.3 Wan-mode decision tree (§7.1, §9.3, §10).

The render mode is chosen by :func:`decide_render_mode`, a pure implementation of
the §9.3 tree, so it is unit-testable for every branch. The model then fills the
creative content (prompt / negative prompt / camera / seed) and *selects*
reference image ids — but only from the locked refs in the canon slice; invented
ids are dropped (the appearance is pinned to the canon, not re-imagined).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonEntitySlice, CanonSlice
from app.memory.prefs_service import PreferencePriors
from app.memory.prefs_signals import (
    camera_overrides,
    infer_signals,
    preferences_payload,
    prompt_hints,
)
from app.providers import Providers
from app.render.cinematic_language import (
    StyleProfile,
    color_grade_for,
    infer_genre,
    infer_style_override,
    lens_for,
    lighting_for,
    negative_prompt_for,
    select_style_profile,
    style_prompt_fragment,
)

from .base import BaseAgent
from .contracts import (
    Beat,
    Camera,
    CinematographerFill,
    DirectorNote,
    RenderMode,
    Segment,
    ShotSpec,
)
from .prompts import CINEMATOGRAPHER, SEGMENT

#: Director-note cues that demand the shot land on an exact composition (→ FLF).
_POSE_CUES = ("pose", "land on", "end on", "final frame", "freeze", "compose on")
#: Director-note cues that mark a minor edit of an already-accepted clip.
_EDIT_CUES = ("make ", "change ", "recolor", "recolour", "tweak", "adjust", "swap", "turn the")


@dataclass(frozen=True, slots=True)
class RenderModeInputs:
    """The boolean signals the §9.3 tree branches on (derived from beat+slice+notes)."""

    locked_character_present: bool = False
    needs_motion: bool = False
    must_land_exact_pose: bool = False
    prev_shot_accepted_continuous: bool = False
    is_establishing_no_character: bool = False
    minor_edit_on_accepted_clip: bool = False


def decide_render_mode(inputs: RenderModeInputs) -> RenderMode:
    """The §9.3 decision tree as deterministic code.

    locked character + motion + exact pose      -> first_last_frame
    locked character + motion + prev continuous  -> video_continuation
    locked character + motion                     -> reference_to_video
    establishing & no character                   -> text_to_video
    minor edit on an accepted clip                -> instruction_edit
    (fallback)                                    -> reference_to_video / text_to_video
    """
    if inputs.locked_character_present and inputs.needs_motion:
        if inputs.must_land_exact_pose:
            return RenderMode.FIRST_LAST_FRAME
        if inputs.prev_shot_accepted_continuous:
            return RenderMode.VIDEO_CONTINUATION
        return RenderMode.REFERENCE_TO_VIDEO
    if inputs.is_establishing_no_character:
        return RenderMode.TEXT_TO_VIDEO
    if inputs.minor_edit_on_accepted_clip:
        return RenderMode.INSTRUCTION_EDIT
    return (
        RenderMode.REFERENCE_TO_VIDEO
        if inputs.locked_character_present
        else RenderMode.TEXT_TO_VIDEO
    )


def locked_reference_ids(canon_slice: CanonSlice) -> list[str]:
    """The versioned ids of every entity with a locked reference in the slice.

    These are the *only* ids the Cinematographer may pin a shot to (§9.3 — lock
    appearance to the canon's locked references, never re-imagine per clip).
    """
    ids: list[str] = []
    ids.extend(_versioned_id(c) for c in canon_slice.characters if _has_locked_ref(c))
    if canon_slice.location is not None and _has_locked_ref(canon_slice.location):
        ids.append(_versioned_id(canon_slice.location))
    ids.extend(_versioned_id(p) for p in canon_slice.props if _has_locked_ref(p))
    return ids


def _has_locked_ref(entity: CanonEntitySlice) -> bool:
    return any(ref.locked for ref in entity.reference_images)


def _versioned_id(entity: CanonEntitySlice) -> str:
    return f"{entity.entity_key}@v{entity.version}"


def _stable_seed(beat_id: str) -> int:
    """A deterministic seed from the beat id (stable across runs → cache-friendly)."""
    digest = hashlib.sha1(beat_id.encode("utf-8")).digest()[:4]
    return int.from_bytes(digest, "big")


@dataclass(frozen=True, slots=True)
class CinematicBrief:
    """The cinematic-language brief the Cinematographer hands the model (§10).

    A deterministic, pre-computed bundle — the directorial eye (style profile),
    the genre, and the per-beat lens/lighting/grade — so the LLM fills prose
    *within* a fixed look instead of re-imagining the film each shot, and the
    deterministic camera floor stays consistent with the canon's style tokens.
    """

    profile: StyleProfile
    genre: str
    lens: str
    lighting: str
    grade: str
    negative_floor: str

    def payload(self) -> dict[str, str]:
        """The serializable block injected under ``cinematography`` in the prompt."""
        return {
            "director_style": self.profile.name,
            "style_note": self.profile.note,
            "genre": self.genre,
            "look": style_prompt_fragment(self.profile),
            "lens": self.lens,
            "lighting": self.lighting,
            "color_grade": self.grade,
            "negative_floor": self.negative_floor,
        }


def style_override_from_notes(notes: list[DirectorNote]) -> str | None:
    """The most recent director note naming a *look* → its profile id (§8.6/§10).

    A note like "shoot it like noir" or "more symmetrical" re-shoots the scene
    through that directorial eye; the last such note wins (the latest ask). A note
    that only names an *axis* ("slower", "warmer") returns ``None`` here — those
    stay on the §8.6 prefs path.
    """
    for note in reversed(notes):
        override = infer_style_override(note.note)
        if override is not None:
            return override
    return None


def build_brief(
    beat: Beat,
    canon_slice: CanonSlice,
    *,
    profile_override: str | None = None,
) -> CinematicBrief:
    """Compute the cinematic brief for ``beat`` against the canon's style (pure).

    The directorial eye is chosen from the scene's genre (or a canon
    ``director_style`` style token / explicit override), and the lens/lighting/
    grade are derived from that profile and nudged by the beat's own mood — one
    consistent look across the film, expressive per beat.
    """
    style_tokens = canon_slice.style.style_tokens if canon_slice.style else None
    profile = select_style_profile([beat], style_tokens=style_tokens, override=profile_override)
    genre = infer_genre([beat])
    return CinematicBrief(
        profile=profile,
        genre=genre.value,
        lens=lens_for(beat, profile),
        lighting=lighting_for(beat, profile),
        grade=color_grade_for(beat, profile),
        negative_floor=negative_prompt_for([beat], genre=genre),
    )


def build_segment_brief(
    beats: Sequence[Beat],
    canon_slice: CanonSlice,
    *,
    profile_override: str | None = None,
) -> CinematicBrief:
    """Compute one cinematic brief for a whole packed segment (pure).

    A continuous take holds *one* look across its beats, so the directorial eye
    and genre are inferred over the entire beat-run and the lens/lighting/grade
    are taken from the segment's opening beat (the take's establishing register).
    """
    style_tokens = canon_slice.style.style_tokens if canon_slice.style else None
    beat_list = list(beats)
    profile = select_style_profile(beat_list, style_tokens=style_tokens, override=profile_override)
    opener = beat_list[0] if beat_list else Beat(summary="")
    genre = infer_genre(beat_list)
    return CinematicBrief(
        profile=profile,
        genre=genre.value,
        lens=lens_for(opener, profile),
        lighting=lighting_for(opener, profile),
        grade=color_grade_for(opener, profile),
        negative_floor=negative_prompt_for(beat_list, genre=genre),
    )


class Cinematographer(BaseAgent):
    """Designs one shot: chooses the Wan mode, then fills prose/camera/seed/refs."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="cinematographer",
            model=settings.chat_model_adapter,
            prompt=CINEMATOGRAPHER,
            skills=skills,  # type: ignore[arg-type]
        )

    async def design_shot(
        self,
        beat: Beat,
        canon_slice: CanonSlice,
        director_notes: list[DirectorNote] | None = None,
        *,
        shot_id: str | None = None,
        target_duration_s: float = 5.0,
        priors: PreferencePriors | None = None,
    ) -> ShotSpec:
        """Design a shot for ``beat``: pick the render mode, then the creative fill.

        ``priors`` are the reader's accumulated directing preferences (§8.6). They
        are handed to the model as a default style *and* applied deterministically
        after the fill, so a learned "slower / wider" taste shifts the shot even
        when nothing in this beat asked for it — but only on axes the current
        director notes do not already speak to (an explicit ask always wins).
        """
        notes = director_notes or []
        inputs = self.derive_inputs(beat, canon_slice, notes)
        mode = decide_render_mode(inputs)
        candidates = locked_reference_ids(canon_slice)
        brief = build_brief(beat, canon_slice, profile_override=style_override_from_notes(notes))

        payload = {
            "beat": beat.model_dump(mode="json"),
            "render_mode": mode.value,
            "style_tokens": canon_slice.style.style_tokens if canon_slice.style else None,
            "cinematography": brief.payload(),
            "locked_reference_ids": candidates,
            "has_previous_endpoint": canon_slice.previous_endpoint is not None,
            "director_notes": [n.model_dump(mode="json") for n in notes],
            "preferences": preferences_payload(priors) or None,
        }
        fill = await self.run_json(payload, CinematographerFill, temperature=0.4)
        camera, prompt = self._apply_priors(fill.camera, fill.prompt, priors, notes)

        return ShotSpec(
            shot_id=shot_id or f"{beat.beat_id or 'beat'}_shot_00",
            beat_id=beat.beat_id or None,
            scene_id=beat.scene_id,
            render_mode=mode,
            prompt=prompt,
            negative_prompt=self._merge_negative(fill.negative_prompt, brief.negative_floor),
            reference_image_ids=self._select_refs(mode, fill.reference_image_ids, candidates),
            camera=camera,
            seed=fill.seed if fill.seed is not None else _stable_seed(beat.beat_id or "beat"),
            target_duration_s=target_duration_s,
            end_frame_ref=None,
        )

    async def design_segment(
        self,
        segment: Segment,
        beats: Sequence[Beat],
        canon_slice: CanonSlice,
        director_notes: list[DirectorNote] | None = None,
        *,
        continues_from_previous: bool = False,
        priors: PreferencePriors | None = None,
    ) -> ShotSpec:
        """Design ONE continuous ≤15s take for a packed ``segment`` (the single-clip overhaul).

        Unlike :meth:`design_shot`, a segment spans several beats rendered as a
        single seam-free i2v take: the render mode is reference/continuation (or
        text-to-video when no character is present), never a pose-landing
        first/last-frame, and the clip's duration is the segment's packed length
        (≤15s). The model is driven by the long-form ``segment@v1`` prompt and
        selects locked references verbatim. ``continues_from_previous`` marks a
        segment that opens on the prior segment's last frame (within-scene chain).
        """
        notes = director_notes or []
        has_character = bool(canon_slice.characters)
        anchored = continues_from_previous or canon_slice.previous_endpoint is not None
        inputs = RenderModeInputs(
            locked_character_present=any(_has_locked_ref(c) for c in canon_slice.characters),
            needs_motion=True,
            must_land_exact_pose=False,  # a segment is a continuous take, not a pose landing
            prev_shot_accepted_continuous=anchored,
            is_establishing_no_character=not has_character,
            minor_edit_on_accepted_clip=False,
        )
        mode = decide_render_mode(inputs)
        candidates = locked_reference_ids(canon_slice)
        brief = build_segment_brief(
            beats, canon_slice, profile_override=style_override_from_notes(notes)
        )

        payload = {
            "segment_id": segment.segment_id,
            "duration_s": segment.duration_s,
            "render_mode": mode.value,
            "continues_from_previous": anchored,
            "beats": [
                {"summary": b.summary, "described_visuals": b.described_visuals, "mood": b.mood}
                for b in beats
            ],
            "style_tokens": canon_slice.style.style_tokens if canon_slice.style else None,
            "cinematography": brief.payload(),
            "locked_reference_ids": candidates,
            "director_notes": [n.model_dump(mode="json") for n in notes],
            "preferences": preferences_payload(priors) or None,
        }
        fill = await self.run_json(
            payload, CinematographerFill, temperature=0.4, system=SEGMENT.system
        )
        camera, prompt = self._apply_priors(fill.camera, fill.prompt, priors, notes)

        return ShotSpec(
            shot_id=segment.segment_id,
            beat_id=beats[0].beat_id if beats else None,
            scene_id=beats[0].scene_id if beats else None,
            render_mode=mode,
            prompt=prompt,
            negative_prompt=self._merge_negative(fill.negative_prompt, brief.negative_floor),
            reference_image_ids=self._select_refs(mode, fill.reference_image_ids, candidates),
            camera=camera,
            seed=fill.seed if fill.seed is not None else _stable_seed(segment.segment_id),
            target_duration_s=segment.duration_s,
            end_frame_ref=None,
        )

    @staticmethod
    def _apply_priors(
        camera: Camera,
        prompt: str,
        priors: PreferencePriors | None,
        notes: list[DirectorNote],
    ) -> tuple[Camera, str]:
        """Fold learned priors into the camera/prompt, skipping axes the notes set."""
        if priors is None or not priors.priors:
            return camera, prompt
        # Axes this call's notes already address are left to the explicit ask.
        skip = frozenset(kind for kind, _ in infer_signals(" ".join(n.note for n in notes)))
        overrides = camera_overrides(priors, skip=skip)
        if overrides:
            camera = camera.model_copy(update=overrides)
        for hint in prompt_hints(priors, skip=skip):
            if hint.lower() not in prompt.lower():
                prompt = f"{prompt}; {hint}" if prompt else hint
        return camera, prompt

    # -- deriving the tree's booleans from real signals ---------------------- #

    def derive_inputs(
        self, beat: Beat, canon_slice: CanonSlice, notes: list[DirectorNote]
    ) -> RenderModeInputs:
        """Map a beat + canon slice + director notes onto the §9.3 tree's signals."""
        minor_edit = self._is_minor_edit(notes)
        has_character = bool(canon_slice.characters)
        return RenderModeInputs(
            locked_character_present=any(_has_locked_ref(c) for c in canon_slice.characters),
            # A beat is fresh motion unless it is a minor edit of existing footage.
            needs_motion=not minor_edit,
            must_land_exact_pose=self._wants_exact_pose(notes),
            prev_shot_accepted_continuous=(
                not minor_edit and canon_slice.previous_endpoint is not None
            ),
            is_establishing_no_character=not has_character,
            minor_edit_on_accepted_clip=minor_edit,
        )

    @staticmethod
    def _wants_exact_pose(notes: list[DirectorNote]) -> bool:
        return any(any(cue in n.note.lower() for cue in _POSE_CUES) for n in notes)

    @staticmethod
    def _is_minor_edit(notes: list[DirectorNote]) -> bool:
        return any(
            n.shot_id is not None and any(cue in n.note.lower() for cue in _EDIT_CUES)
            for n in notes
        )

    @staticmethod
    def _select_refs(mode: RenderMode, selected: list[str], candidates: list[str]) -> list[str]:
        if mode is RenderMode.TEXT_TO_VIDEO:
            return []
        verbatim = [ref for ref in selected if ref in candidates]
        return verbatim or list(candidates)

    @staticmethod
    def _merge_negative(model_negative: str | None, floor: str) -> str:
        """Union the model's negative prompt with the deterministic genre floor.

        The §9.3/§10 negative floor (universal artifacts + the genre's
        look-breakers) is always present; the model may *add* to it but can never
        drop it. De-dups by the comma-separated term, preserving floor-first order.
        """
        floor_terms = [t.strip() for t in floor.split(",") if t.strip()]
        extra = [t.strip() for t in (model_negative or "").split(",") if t.strip()]
        seen: set[str] = set()
        merged: list[str] = []
        for term in [*floor_terms, *extra]:
            key = term.lower()
            if key not in seen:
                seen.add(key)
                merged.append(term)
        return ", ".join(merged)


__all__ = [
    "Cinematographer",
    "CinematicBrief",
    "RenderModeInputs",
    "build_brief",
    "build_segment_brief",
    "decide_render_mode",
    "locked_reference_ids",
    "style_override_from_notes",
]
