"""Cinematographer — shot design + the §9.3 Wan-mode decision tree (§7.1, §9.3, §10).

The render mode is chosen by :func:`decide_render_mode`, a pure implementation of
the §9.3 tree, so it is unit-testable for every branch. The model then fills the
creative content (prompt / negative prompt / camera / seed) and *selects*
reference image ids — but only from the locked refs in the canon slice; invented
ids are dropped (the appearance is pinned to the canon, not re-imagined).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonEntitySlice, CanonSlice
from app.providers import Providers

from .base import BaseAgent
from .contracts import (
    Beat,
    CinematographerFill,
    DirectorNote,
    RenderMode,
    ShotSpec,
)
from .prompts import CINEMATOGRAPHER

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
    ) -> ShotSpec:
        """Design a shot for ``beat``: pick the render mode, then the creative fill."""
        notes = director_notes or []
        inputs = self.derive_inputs(beat, canon_slice, notes)
        mode = decide_render_mode(inputs)
        candidates = locked_reference_ids(canon_slice)

        payload = {
            "beat": beat.model_dump(mode="json"),
            "render_mode": mode.value,
            "style_tokens": canon_slice.style.style_tokens if canon_slice.style else None,
            "locked_reference_ids": candidates,
            "has_previous_endpoint": canon_slice.previous_endpoint is not None,
            "director_notes": [n.model_dump(mode="json") for n in notes],
        }
        fill = await self.run_json(payload, CinematographerFill, temperature=0.4)

        return ShotSpec(
            shot_id=shot_id or f"{beat.beat_id or 'beat'}_shot_00",
            beat_id=beat.beat_id or None,
            scene_id=beat.scene_id,
            render_mode=mode,
            prompt=fill.prompt,
            negative_prompt=fill.negative_prompt,
            reference_image_ids=self._select_refs(mode, fill.reference_image_ids, candidates),
            camera=fill.camera,
            seed=fill.seed if fill.seed is not None else _stable_seed(beat.beat_id or "beat"),
            target_duration_s=target_duration_s,
            end_frame_ref=None,
        )

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
    def _select_refs(
        mode: RenderMode, selected: list[str], candidates: list[str]
    ) -> list[str]:
        if mode is RenderMode.TEXT_TO_VIDEO:
            return []
        verbatim = [ref for ref in selected if ref in candidates]
        return verbatim or list(candidates)


__all__ = [
    "Cinematographer",
    "RenderModeInputs",
    "decide_render_mode",
    "locked_reference_ids",
]
