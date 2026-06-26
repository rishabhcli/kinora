"""The Event Director — beat-cluster → N parallel shots → one chronological film.

An **event** is a beat-cluster (e.g. "the chase across the bridge"): a contiguous
run of beats inside a scene that read as one continuous dramatic moment. Where the
Adapter decomposes a *scene* into ~5s shots (§4.2) and the Scheduler renders them
one at a time, the Event Director plans and renders an event as a **single
continuous vertical film** — 3–6 shots fanned out concurrently, stitched with
explicit continuity from the end-state of each shot into the start of the next.

This module is the Script/Director Agent + Video-Generation-Agent role from the
product architecture, split into three pure-then-orchestrated layers so each is
testable in isolation (the house style — cf. :mod:`app.render.stitch`):

* **planning** (:func:`plan_event_script`, pure) — clusters beats into an ordered
  shot list; picks each shot's duration from beat density + pacing (the event
  director decides clip length, *not* a global constant, default 3–8s); chains the
  §9.3 render modes (a locked character establishes, then the film *continues*
  from each accepted endpoint); and emits an explicit **continuity hand-off** —
  the end-state of shot N anchors the start of N+1 via its ``lastframes/{book}/{
  shot}.png`` frame (§9.3 ``video_continuation`` / §9.6 last-frame → canon);
* **fan-out** (:class:`EventDirector.render_event`) — renders the shots
  *concurrently* (``asyncio.gather``); with ``KINORA_LIVE_VIDEO`` off every shot
  is a real Ken-Burns mp4 at the vertical :data:`~app.render.degrade.FILM_SIZE`
  (zero video-seconds), so the whole event film proves out end-to-end at no spend;
* **stitch** — the per-shot clips concatenate into ONE 720×1280 mp4 and the
  per-shot sync segments merge (cumulative timecodes) into one event sync map
  (delegated to :mod:`app.render.stitch` so the geometry + timeline math is shared
  with the scene stitcher).

The renderer is a :class:`EventShotRenderer` Protocol so the live Wan path can be
slotted in later without touching planning or stitching, and so tests can inject a
double that records start timestamps to prove the fan-out is concurrent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio
from pydantic import BaseModel, ConfigDict, Field

from app.agents.cinematographer import RenderModeInputs, decide_render_mode
from app.agents.contracts import Beat, Camera, RenderMode, SourceSpan
from app.core.logging import get_logger
from app.memory.interfaces import BlobStore, CanonSlice
from app.render.degrade import (
    DEFAULT_FPS,
    FILM_SIZE,
    audio_text_card,
    ken_burns_over_image,
    zoom_for_camera,
)
from app.render.stitch import SceneSyncMap, concat_clips, merge_sync_segments
from app.render.sync_map import SyncSegment, build_sync_segment
from app.storage.object_store import keys

logger = get_logger("app.render.event_director")

#: An event bundles this many shots into one continuous film (the spec's 3–6).
MIN_EVENT_SHOTS = 3
MAX_EVENT_SHOTS = 6
#: The per-shot duration band the event director chooses within (seconds).
MIN_SHOT_S = 3.0
MAX_SHOT_S = 8.0
BASE_SHOT_S = 5.0

#: Beat cues that mark a shot which must *land* on an exact pose/composition — the
#: §9.3 ``first_last_frame`` branch (storyboard → storyboard, no drift-to-generic).
_POSE_CUES = (
    "final stand",
    "turns to face",
    "freeze",
    "lands on",
    "comes to rest",
    "final frame",
    "stops dead",
)
#: Mood/summary cues that tighten or stretch a shot's dwell time.
_FAST_CUES = ("chase", "sprint", "frantic", "urgent", "tense", "fast", "panic", "run")
_SLOW_CUES = ("calm", "slow", "quiet", "still", "somber", "languid", "gentle", "wide")
#: active-state predicates that read as wardrobe / time-of-day continuity.
_WARDROBE_PREDICATES = ("wears", "wearing", "dressed", "outfit", "clothes", "coat", "cloak")
_TIME_PREDICATES = ("time_of_day", "time", "hour", "daylight")


# --------------------------------------------------------------------------- #
# The event-script contract (serializable; Agent 3 exposes it over HTTP)
# --------------------------------------------------------------------------- #


class ContinuityDirective(BaseModel):
    """The explicit continuity a shot must honor + the state it hands off (§9.6).

    The ``hand_off`` is the **end-state** of this shot — the composition the next
    shot opens on — and ``continues_from_shot_id`` / ``last_frame_key`` are the
    concrete anchor (§9.3 ``video_continuation`` / ``first_last_frame``): the prior
    shot's accepted last frame at ``lastframes/{book}/{shot}.png``.
    """

    model_config = ConfigDict(extra="forbid")

    wardrobe: str | None = None
    setting: str | None = None
    lighting: str | None = None
    time_of_day: str | None = None
    camera_logic: str = ""
    hand_off: str = ""
    continues_from_shot_id: str | None = None
    last_frame_key: str | None = None


class EventShot(BaseModel):
    """One shot in an event script: its beat, render mode, duration + continuity."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    beat_id: str | None = None
    ordinal: int
    render_mode: RenderMode
    summary: str = ""
    camera: Camera = Field(default_factory=Camera)
    duration_s: float
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    directive: ContinuityDirective = Field(default_factory=ContinuityDirective)


class EventScript(BaseModel):
    """The ordered shot list for one event + its cumulative continuity plan."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    book_id: str
    scene_id: str | None = None
    shots: list[EventShot] = Field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        return round(sum(s.duration_s for s in self.shots), 3)


# --------------------------------------------------------------------------- #
# Pure planning (§4.2 cluster, §9.3 mode chain, per-beat duration)
# --------------------------------------------------------------------------- #


def _lower(text: str | None) -> str:
    return (text or "").lower()


def shot_duration_for_beat(
    beat: Beat,
    *,
    base_s: float = BASE_SHOT_S,
    min_s: float = MIN_SHOT_S,
    max_s: float = MAX_SHOT_S,
) -> float:
    """Choose a shot's dwell time from its beat's density + pacing (3–8s band).

    Denser beats (more narrative words) run a touch longer; a frantic/chase mood
    tightens the cut, a calm/wide mood lets it linger — so clip length is a
    *decision*, not a fixed constant (the event-director mandate).
    """
    words = len((beat.summary or "").split())
    # ~12 words ≈ the base duration; each extra word nudges +0.1s.
    dur = base_s + (words - 12) * 0.1
    mood = f"{_lower(beat.mood)} {_lower(beat.summary)}"
    if any(cue in mood for cue in _FAST_CUES):
        dur *= 0.8
    elif any(cue in mood for cue in _SLOW_CUES):
        dur *= 1.25
    return round(max(min_s, min(max_s, dur)), 2)


def wants_exact_pose(beat: Beat) -> bool:
    """Whether the beat must land on an exact composition (→ first_last_frame)."""
    text = f"{_lower(beat.summary)} {_lower(beat.mood)}"
    return any(cue in text for cue in _POSE_CUES)


def camera_for_ordinal(ordinal: int, total: int, beat: Beat) -> Camera:
    """A first-pass shot-grammar camera: establish wide, then push in (§9.3/§10).

    Shot 0 of an event establishes (wide, slow); interior shots play medium; a
    shot that lands a pose tightens to a close. Pacing follows the beat mood. This
    is the deterministic seed the richer shot-grammar (screen-direction, the 180°
    rule) refines.
    """
    mood = f"{_lower(beat.mood)} {_lower(beat.summary)}"
    speed = "fast" if any(c in mood for c in _FAST_CUES) else (
        "slow" if any(c in mood for c in _SLOW_CUES) else "medium"
    )
    if ordinal == 0:
        return Camera(move="push_in", speed=speed, shot_size="wide")
    if wants_exact_pose(beat):
        return Camera(move="static", speed=speed, shot_size="close")
    return Camera(move="pan", speed=speed, shot_size="medium")


def _has_locked_character(canon: CanonSlice | None) -> bool:
    if canon is None:
        return False
    return any(
        any(ref.locked for ref in c.reference_images) for c in canon.characters
    )


def _wardrobe_from_canon(canon: CanonSlice | None) -> str | None:
    if canon is None:
        return None
    for state in canon.active_states:
        if any(p in _lower(state.predicate) for p in _WARDROBE_PREDICATES):
            return state.object_value
    for character in canon.characters:
        appearance = character.appearance or {}
        for key in ("wardrobe", "outfit", "clothing", "attire"):
            if appearance.get(key):
                return str(appearance[key])
    return None


def _time_of_day_from(canon: CanonSlice | None, beat: Beat) -> str | None:
    if canon is not None:
        for state in canon.active_states:
            if any(p in _lower(state.predicate) for p in _TIME_PREDICATES):
                return state.object_value
        if canon.style and canon.style.style_tokens:
            tod = canon.style.style_tokens.get("time_of_day")
            if tod:
                return str(tod)
    for cue in ("dawn", "dusk", "night", "midnight", "noon", "morning", "evening", "twilight"):
        if cue in _lower(beat.summary):
            return cue
    return None


def _lighting_from(canon: CanonSlice | None, beat: Beat) -> str | None:
    if canon is not None and canon.style and canon.style.style_tokens:
        for key in ("lighting", "palette", "lens"):
            value = canon.style.style_tokens.get(key)
            if value:
                return str(value)
    return beat.mood or None


def _setting_from(canon: CanonSlice | None, beat: Beat) -> str | None:
    if canon is not None and canon.location is not None:
        return canon.location.description or canon.location.name
    return beat.described_visuals or None


def _hand_off_for(beat: Beat) -> str:
    """A short end-state description the next shot opens on (the §9.6 hand-off)."""
    summary = (beat.summary or "").strip()
    if not summary:
        return "hold on the final composition"
    # The last clause of the beat is the moment the shot lands on.
    clause = summary.replace(";", ".").split(".")
    tail = next((c.strip() for c in reversed(clause) if c.strip()), summary)
    return f"end on: {tail}"


def plan_event_script(
    *,
    event_id: str,
    book_id: str,
    scene_id: str | None,
    beats: Sequence[Beat],
    canon: CanonSlice | None = None,
    max_shots: int = MAX_EVENT_SHOTS,
    base_duration_s: float = BASE_SHOT_S,
) -> EventScript:
    """Cluster ``beats`` into an ordered event shot list (pure, deterministic).

    One beat → one shot (the §4.2 default), capped at ``max_shots``. Each shot gets
    a per-beat duration, a §9.3 render mode chained off the previous shot, a
    shot-grammar camera, and a :class:`ContinuityDirective` carrying the canon's
    wardrobe/setting/lighting/time-of-day plus the explicit last-frame hand-off.
    """
    chosen = list(beats[:max_shots])
    has_locked = _has_locked_character(canon)
    has_characters = bool(canon and canon.characters)
    prev_endpoint = canon.previous_endpoint if canon is not None else None

    shots: list[EventShot] = []
    for ordinal, beat in enumerate(chosen):
        shot_id = f"{event_id}_shot_{ordinal:02d}"
        mode = decide_render_mode(
            RenderModeInputs(
                locked_character_present=has_locked,
                needs_motion=True,
                must_land_exact_pose=wants_exact_pose(beat),
                # Every interior shot continues from the previous accepted endpoint.
                prev_shot_accepted_continuous=(ordinal > 0),
                is_establishing_no_character=not has_characters,
                minor_edit_on_accepted_clip=False,
            )
        )
        if ordinal > 0:
            continues_from = shots[ordinal - 1].shot_id
            last_frame_key = keys.lastframe(book_id, continues_from)
        elif prev_endpoint is not None:
            # Cross-event continuity: open on the prior event's accepted endpoint.
            continues_from = prev_endpoint.shot_id
            last_frame_key = prev_endpoint.last_frame_key or keys.lastframe(
                book_id, prev_endpoint.shot_id
            )
        else:
            continues_from = None
            last_frame_key = None

        directive = ContinuityDirective(
            wardrobe=_wardrobe_from_canon(canon),
            setting=_setting_from(canon, beat),
            lighting=_lighting_from(canon, beat),
            time_of_day=_time_of_day_from(canon, beat),
            camera_logic=("establishing" if ordinal == 0 else "continuation"),
            hand_off=_hand_off_for(beat),
            continues_from_shot_id=continues_from,
            last_frame_key=last_frame_key,
        )
        shots.append(
            EventShot(
                shot_id=shot_id,
                beat_id=beat.beat_id or None,
                ordinal=ordinal,
                render_mode=mode,
                summary=beat.summary,
                camera=camera_for_ordinal(ordinal, len(chosen), beat),
                duration_s=shot_duration_for_beat(beat, base_s=base_duration_s),
                source_span=beat.source_span,
                directive=directive,
            )
        )

    logger.info(
        "event.planned",
        event_id=event_id,
        scene_id=scene_id,
        shots=len(shots),
        modes=[s.render_mode.value for s in shots],
    )
    return EventScript(event_id=event_id, book_id=book_id, scene_id=scene_id, shots=shots)


# --------------------------------------------------------------------------- #
# Rendering — concurrent fan-out + stitch
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RenderedShot:
    """One rendered shot's artifacts + the wall-clock window it occupied.

    ``started_at`` / ``finished_at`` are ``time.monotonic`` stamps used to *prove*
    the fan-out ran concurrently (the renders overlap in time).
    """

    shot_id: str
    ordinal: int
    clip_bytes: bytes
    last_frame_bytes: bytes | None
    duration_s: float
    render_mode: RenderMode
    word_timestamps: list[Any] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class EventRenderResult:
    """The rendered + stitched event: ONE vertical mp4 + the merged sync map."""

    event_id: str
    scene_id: str | None
    book_id: str
    clip_bytes: bytes
    sync_map: SceneSyncMap
    duration_s: float
    shot_count: int
    rendered: list[RenderedShot]
    clip_key: str | None = None
    clip_url: str | None = None
    last_frame_keys: dict[str, str] = field(default_factory=dict)


class EventShotRenderer(Protocol):
    """Render one shot to a clip + last frame (Ken-Burns off-gate, Wan live)."""

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot: ...


class KenBurnsEventRenderer:
    """The default off-gate renderer: a real Ken-Burns mp4 per shot at FILM_SIZE.

    Zero video-seconds — proves the whole event pipeline end-to-end with
    ``KINORA_LIVE_VIDEO`` off (§4.4/§12.4). The learned camera prior drives the
    push (``zoom_for_camera``); a shot with no still drops to the audio/text card.
    """

    def __init__(
        self, *, film_size: tuple[int, int] = FILM_SIZE, fps: int = DEFAULT_FPS
    ) -> None:
        self._film_size = film_size
        self._fps = fps

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        started = time.monotonic()
        if still is not None:
            zoom = zoom_for_camera(shot.camera)
            clip = await anyio.to_thread.run_sync(
                lambda: ken_burns_over_image(
                    still,
                    shot.duration_s,
                    audio_bytes=audio,
                    size=self._film_size,
                    fps=self._fps,
                    zoom_max=zoom,
                )
            )
            last_frame = still
        else:
            clip = await anyio.to_thread.run_sync(
                lambda: audio_text_card(
                    shot.duration_s, audio_bytes=audio, size=self._film_size, fps=self._fps
                )
            )
            last_frame = None
        finished = time.monotonic()
        return RenderedShot(
            shot_id=shot.shot_id,
            ordinal=shot.ordinal,
            clip_bytes=clip,
            last_frame_bytes=last_frame,
            duration_s=shot.duration_s,
            render_mode=shot.render_mode,
            started_at=started,
            finished_at=finished,
        )


class EventDirector:
    """Render an event script's shots concurrently, then stitch ONE event film.

    ``renderer`` defaults to the off-gate :class:`KenBurnsEventRenderer`; inject a
    Wan-backed renderer for the live path. When a ``store`` (a :class:`BlobStore`)
    is given the event film is persisted at ``clips/{book}/{event}`` and every
    shot's last frame at ``lastframes/{book}/{shot}.png`` (the §9.6 continuation
    anchors the next event opens on).
    """

    def __init__(
        self,
        renderer: EventShotRenderer | None = None,
        *,
        store: BlobStore | None = None,
        film_size: tuple[int, int] = FILM_SIZE,
        fps: int = DEFAULT_FPS,
        url_ttl: int = 3600,
    ) -> None:
        self._renderer = renderer or KenBurnsEventRenderer(film_size=film_size, fps=fps)
        self._store = store
        self._film_size = film_size
        self._fps = fps
        self._ttl = url_ttl

    async def render_event(
        self,
        script: EventScript,
        *,
        stills: Mapping[str, bytes] | None = None,
        audio: Mapping[str, bytes] | None = None,
        page_boxes: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        word_timestamps: Mapping[str, Sequence[Any]] | None = None,
    ) -> EventRenderResult:
        """Fan the shots out concurrently, stitch into one vertical film + sync map.

        Raises:
            ValueError: when the script has no shots.
        """
        if not script.shots:
            raise ValueError("cannot render an event with no shots")
        stills = stills or {}
        audio = audio or {}

        # Fan-out: every shot render starts before any finishes (asyncio.gather).
        rendered: list[RenderedShot] = list(
            await asyncio.gather(
                *(
                    self._renderer.render_shot(
                        shot, still=stills.get(shot.shot_id), audio=audio.get(shot.shot_id)
                    )
                    for shot in script.shots
                )
            )
        )

        segments: list[SyncSegment] = []
        durations: list[float] = []
        for shot, shot_result in zip(script.shots, rendered, strict=True):
            segments.append(
                build_sync_segment(
                    shot_id=shot_result.shot_id,
                    word_timestamps=(word_timestamps or {}).get(shot_result.shot_id)
                    or shot_result.word_timestamps,
                    source_span=shot.source_span.model_dump(mode="json"),
                    page_word_boxes=(page_boxes or {}).get(shot_result.shot_id),
                    duration_s=shot_result.duration_s,
                )
            )
            durations.append(shot_result.duration_s)

        clips = [r.clip_bytes for r in rendered]
        clip_bytes = await anyio.to_thread.run_sync(
            lambda: concat_clips(clips, size=self._film_size, fps=self._fps)
        )
        sync_map = merge_sync_segments(segments, scene_id=script.event_id, durations=durations)

        clip_key, clip_url, last_frame_keys = await self._persist(script, clip_bytes, rendered)

        logger.info(
            "event.rendered",
            event_id=script.event_id,
            scene_id=script.scene_id,
            shots=len(rendered),
            duration_s=sync_map.duration_s,
            size=f"{self._film_size[0]}x{self._film_size[1]}",
            clip_key=clip_key,
        )
        return EventRenderResult(
            event_id=script.event_id,
            scene_id=script.scene_id,
            book_id=script.book_id,
            clip_bytes=clip_bytes,
            sync_map=sync_map,
            duration_s=sync_map.duration_s,
            shot_count=len(rendered),
            rendered=rendered,
            clip_key=clip_key,
            clip_url=clip_url,
            last_frame_keys=last_frame_keys,
        )

    async def _persist(
        self, script: EventScript, clip_bytes: bytes, rendered: Sequence[RenderedShot]
    ) -> tuple[str | None, str | None, dict[str, str]]:
        """Persist the event film + every shot's last frame, if a store is wired."""
        if self._store is None:
            return None, None, {}
        store = self._store
        clip_key = keys.clip(script.book_id, script.event_id)
        await anyio.to_thread.run_sync(store.put_bytes, clip_key, clip_bytes, "video/mp4")
        clip_url = await anyio.to_thread.run_sync(
            lambda: store.presigned_get_url(clip_key, ttl=self._ttl)
        )
        last_frame_keys: dict[str, str] = {}
        for shot_result in rendered:
            if shot_result.last_frame_bytes is None:
                continue
            lf_key = keys.lastframe(script.book_id, shot_result.shot_id)
            await anyio.to_thread.run_sync(
                store.put_bytes, lf_key, shot_result.last_frame_bytes, "image/png"
            )
            last_frame_keys[shot_result.shot_id] = lf_key
        return clip_key, clip_url, last_frame_keys


__all__ = [
    "BASE_SHOT_S",
    "MAX_EVENT_SHOTS",
    "MAX_SHOT_S",
    "MIN_EVENT_SHOTS",
    "MIN_SHOT_S",
    "ContinuityDirective",
    "EventDirector",
    "EventRenderResult",
    "EventScript",
    "EventShot",
    "EventShotRenderer",
    "KenBurnsEventRenderer",
    "RenderedShot",
    "camera_for_ordinal",
    "plan_event_script",
    "shot_duration_for_beat",
    "wants_exact_pose",
]
