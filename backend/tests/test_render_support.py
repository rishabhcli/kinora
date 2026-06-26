"""Shared, network-free test doubles + real-asset builders for the render phase.

The render pipeline's heavy collaborators (the Cinematographer / Generator /
Critic model calls, TTS, image-gen) are replaced with canned async doubles, and
the memory services / repositories with in-memory fakes, so the §9.7 orchestrator
is exercised end-to-end without a database or DashScope. The ffmpeg/Ken-Burns
artifacts are *real* (built with the bundled/system ffmpeg via degrade.py).

This module holds no tests of its own — it mirrors ``tests.test_agents_support``.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import Image

from app.agents.continuity import ContinuityJudgment, build_conflict
from app.agents.contracts import (
    Beat as AgentBeat,
)
from app.agents.contracts import (
    ConflictObject,
    ContinuityResult,
    DecisionRecord,
    QARecord,
    RenderMode,
    TextualSupport,
)
from app.agents.contracts import (
    ShotSpec as AgentShotSpec,
)
from app.agents.critic import decide_qa
from app.agents.generator import GeneratorOutput
from app.agents.showrunner import decide_arbitration
from app.db.models.enums import ShotStatus
from app.memory.budget_service import Reservation
from app.memory.interfaces import (
    CanonEntitySlice,
    CanonSlice,
    EndpointFrame,
    RefImage,
    StateSlice,
)
from app.providers.types import TtsResult, TtsWord
from app.render import degrade

# --------------------------------------------------------------------------- #
# Real asset builders (lazy, so importing this module never needs ffmpeg)
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=8)
def png_bytes(width: int = 1280, height: int = 720) -> bytes:
    """A real gradient PNG (cached by size)."""
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 255) // width, (y * 255) // height, 96)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@lru_cache(maxsize=4)
def wav_bytes(duration_s: float = 2.0, sample_rate: int = 24000, freq: float = 220.0) -> bytes:
    """A real mono 16-bit WAV tone (cached)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        for n in range(int(sample_rate * duration_s)):
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * n / sample_rate)))
        writer.writeframes(bytes(frames))
    return buf.getvalue()


@lru_cache(maxsize=4)
def real_mp4(duration_s: float = 1.0, *, with_audio: bool = True) -> bytes:
    """A real Ken-Burns mp4 (cached) used as a Generator double's clip bytes."""
    audio = wav_bytes(duration_s) if with_audio else None
    return degrade.ken_burns_over_image(png_bytes(640, 360), duration_s, audio_bytes=audio)


def tts_result(duration_s: float = 2.0) -> TtsResult:
    """A real-WAV-backed TTS result with a few word timings."""
    words = [
        TtsWord(text="She", t_start=0.10, t_end=0.40),
        TtsWord(text="stood", t_start=0.40, t_end=0.90),
        TtsWord(text="still", t_start=0.90, t_end=1.40),
    ]
    return TtsResult(
        audio_bytes=wav_bytes(duration_s),
        sample_rate=24000,
        duration_s=duration_s,
        word_timestamps=words,
        alignment="proportional",
        voice_id="vc_fake",
        model="fake-tts",
    )


def generator_output(duration_s: float = 5.0) -> GeneratorOutput:
    """A canned successful Generator result (simulated live Wan + narration)."""
    return GeneratorOutput(
        clip_bytes=real_mp4(1.0, with_audio=True),
        clip_url=None,
        last_frame_bytes=png_bytes(640, 360),
        duration_s=duration_s,
        audio_bytes=wav_bytes(2.0),
        sample_rate=24000,
        word_timestamps=[
            TtsWord(text="She", t_start=0.1, t_end=0.4),
            TtsWord(text="stood", t_start=0.4, t_end=0.9),
            TtsWord(text="still", t_start=0.9, t_end=1.4),
        ],
        provider_task_id="fake-task",
    )


# --------------------------------------------------------------------------- #
# Canon / domain fixtures
# --------------------------------------------------------------------------- #

BOOK_ID = "book_demo"
SCENE_ID = "scene_001"
BEAT_ID = "beat_0007"
SHOT_ID = "shot_00042"
PAGE = 12
WORD_RANGE = (100, 102)
REF_KEY = "refs/book_demo/char_x/front.png"
STYLE_REF_KEY = "refs/book_demo/style_main/key.png"
STATE_ID = "state_sword_001"


def make_slice(*, with_endpoint: bool = False, with_style: bool = False) -> CanonSlice:
    """A minimal but real :class:`CanonSlice` (one voiced, locked-ref character).

    ``with_style`` attaches a Style node carrying a locked reference keyframe — the
    source the pipeline embeds into the §9.5 scene style centroid.
    """
    character = CanonEntitySlice(
        entity_key="char_x",
        type="character",
        name="X",
        version=1,
        description="a quiet figure",
        voice={"cosyvoice_voice_id": "vc_x"},
        reference_images=[RefImage(key=REF_KEY, url=None, pose="front", locked=True)],
        valid_from_beat=1,
    )
    state = StateSlice(
        state_id=STATE_ID,
        subject_entity_key="char_x",
        predicate="possesses",
        object_value="sword",
        valid_from_beat=1,
        valid_to_beat=4,
    )
    endpoint = (
        EndpointFrame(shot_id="shot_prev", last_frame_key="lastframes/book_demo/shot_prev.png")
        if with_endpoint
        else None
    )
    style = (
        CanonEntitySlice(
            entity_key="style_main",
            type="style",
            name="Painterly storybook",
            version=1,
            style_tokens={"palette": "cool", "lens": "wide"},
            reference_images=[RefImage(key=STYLE_REF_KEY, url=None, pose="key", locked=True)],
            valid_from_beat=1,
        )
        if with_style
        else None
    )
    return CanonSlice(
        book_id=BOOK_ID,
        beat_id=BEAT_ID,
        beat_index=7,
        scene_id=SCENE_ID,
        characters=[character],
        active_states=[state],
        previous_endpoint=endpoint,
        style=style,
    )


def word_boxes() -> list[dict[str, Any]]:
    """Three page words with normalized boxes, indices 100..102."""
    return [
        {"word_index": 100, "text": "She", "bbox": [0.10, 0.30, 0.04, 0.02]},
        {"word_index": 101, "text": "stood", "bbox": [0.16, 0.30, 0.06, 0.02]},
        {"word_index": 102, "text": "still", "bbox": [0.24, 0.30, 0.06, 0.02]},
    ]


# --------------------------------------------------------------------------- #
# In-memory rows + repositories
# --------------------------------------------------------------------------- #


@dataclass
class FakeShot:
    id: str
    book_id: str
    beat_id: str | None
    scene_id: str | None
    source_span: dict[str, Any] | None
    duration_s: float | None = 5.0
    shot_hash: str | None = None
    status: ShotStatus = ShotStatus.PLANNED
    output: dict[str, Any] | None = None
    narration: dict[str, Any] | None = None


@dataclass
class FakeBeat:
    id: str
    book_id: str
    scene_id: str
    beat_index: int
    summary: str
    entities: list[str]
    described_visuals: str | None
    mood: str | None
    source_span: dict[str, Any] | None


@dataclass
class FakePage:
    word_boxes: list[dict[str, Any]] | None
    image_key: str | None
    text: str | None


class FakeShotRepo:
    def __init__(self, shot: FakeShot) -> None:
        self._shots = {shot.id: shot}
        self.statuses: list[ShotStatus] = []
        self.accepted: list[str] = []
        self.updates: list[dict[str, Any]] = []

    async def get(self, shot_id: str) -> FakeShot | None:
        return self._shots.get(shot_id)

    async def set_status(self, shot_id: str, status: ShotStatus) -> None:
        self.statuses.append(status)
        if shot_id in self._shots:
            self._shots[shot_id].status = status

    async def mark_accepted(self, shot_id: str) -> None:
        self.accepted.append(shot_id)
        if shot_id in self._shots:
            self._shots[shot_id].status = ShotStatus.ACCEPTED

    async def update(self, shot_id: str, **fields: Any) -> FakeShot | None:
        self.updates.append(fields)
        shot = self._shots.get(shot_id)
        if shot is None:
            return None
        for key, value in fields.items():
            setattr(shot, key, value)
        return shot


class FakeBeatRepo:
    def __init__(self, beat: FakeBeat) -> None:
        self._beats = {beat.id: beat}

    async def get(self, beat_id: str) -> FakeBeat | None:
        return self._beats.get(beat_id)


class FakePageRepo:
    def __init__(self, page: FakePage | None) -> None:
        self._page = page

    async def get_by_number(self, book_id: str, page_number: int) -> FakePage | None:
        return self._page


class FakeDefectRepo:
    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    async def log(
        self,
        *,
        book_id: str,
        kind: str,
        shot_id: str | None = None,
        detail: dict[str, Any] | None = None,
        defect_id: str | None = None,
    ) -> dict[str, Any]:
        record = {"book_id": book_id, "kind": kind, "shot_id": shot_id, "detail": detail}
        self.logged.append(record)
        return record


# --------------------------------------------------------------------------- #
# In-memory memory services
# --------------------------------------------------------------------------- #


class FakeCanon:
    def __init__(self, canon_slice: CanonSlice) -> None:
        self._slice = canon_slice
        self.queries: list[tuple[str, str]] = []

    async def query(self, book_id: str, beat_id: str) -> CanonSlice:
        self.queries.append((book_id, beat_id))
        return self._slice


@dataclass
class FakeCached:
    clip_key: str | None = None
    last_frame_key: str | None = None
    sync_segment: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    video_seconds: float | None = None


class FakeCache:
    """Real content-hash math (delegates to ``CacheService``), in-memory store."""

    def __init__(self) -> None:
        from app.db.hashing import compute_shot_hash
        from app.memory.cache_service import CacheService

        self._compute = compute_shot_hash
        self._ref = CacheService.reference_set_hash
        self.store: dict[str, FakeCached] = {}
        self.puts: list[dict[str, Any]] = []

    def reference_set_hash(self, reference_image_ids: list[str]) -> str:
        return self._ref(reference_image_ids)

    def shot_hash(
        self,
        *,
        book_id: str,
        beat_id: str,
        canon_version_at_render: int,
        render_mode: str,
        seed: int,
        reference_set_hash: str,
    ) -> str:
        return self._compute(
            book_id=book_id,
            beat_id=beat_id,
            canon_version_at_render=canon_version_at_render,
            render_mode=render_mode,
            seed=seed,
            reference_set_hash=reference_set_hash,
        )

    async def get(self, shot_hash: str) -> FakeCached | None:
        return self.store.get(shot_hash)

    async def put(
        self,
        *,
        shot_hash: str,
        book_id: str,
        clip_key: str | None = None,
        last_frame_key: str | None = None,
        sync_segment: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        video_seconds: float | None = None,
    ) -> FakeCached:
        record = FakeCached(clip_key, last_frame_key, sync_segment, qa, video_seconds)
        self.store[shot_hash] = record
        self.puts.append(
            {"shot_hash": shot_hash, "clip_key": clip_key, "video_seconds": video_seconds}
        )
        return record


class FakeBudget:
    def __init__(self, *, live: bool = True, low: bool = False) -> None:
        self._live = live
        self._low = low
        self.reserved: list[float] = []
        self.committed: list[float] = []
        self.released: int = 0

    def can_render_live(self) -> bool:
        return self._live

    async def is_low(self) -> bool:
        return self._low

    def is_low_at(self, remaining: float) -> bool:
        return self._low

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        self.reserved.append(video_seconds)
        return Reservation(id=f"res_{len(self.reserved)}", video_seconds=video_seconds)

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        self.committed.append(
            actual_seconds if actual_seconds is not None else reservation.video_seconds
        )

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        self.released += 1


class FakeEpisodic:
    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> dict[str, Any]:
        self.logged.append(kwargs)
        return kwargs


class FakeObjectStore:
    """In-memory :class:`BlobStore` recording every put."""

    def __init__(self, seed: dict[str, bytes] | None = None) -> None:
        self.store: dict[str, bytes] = dict(seed or {})
        self.puts: list[str] = []

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.store[key] = data
        self.puts.append(key)

    def get_bytes(self, key: str) -> bytes:
        return self.store[key]

    def exists(self, key: str) -> bool:
        return key in self.store

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str:
        return f"https://oss.test/{key}"


# --------------------------------------------------------------------------- #
# Agent doubles (heavy model/provider calls)
# --------------------------------------------------------------------------- #


class FakeDesigner:
    def __init__(self) -> None:
        self.calls = 0
        self.last_notes: list[Any] | None = None
        self.last_priors: Any = None

    async def design_shot(
        self,
        beat: AgentBeat,
        canon_slice: CanonSlice,
        director_notes: list[Any] | None = None,
        *,
        shot_id: str | None = None,
        target_duration_s: float = 5.0,
        priors: Any = None,
    ) -> AgentShotSpec:
        self.calls += 1
        self.last_notes = director_notes
        self.last_priors = priors
        return AgentShotSpec(
            shot_id=shot_id or SHOT_ID,
            beat_id=beat.beat_id or None,
            scene_id=beat.scene_id,
            render_mode=RenderMode.REFERENCE_TO_VIDEO,
            prompt="X stands quietly at the window.",
            negative_prompt="warped face",
            reference_image_ids=["char_x@v1"],
            seed=88000 + self.calls,
            target_duration_s=target_duration_s,
        )


class FakeGenerator:
    def __init__(
        self, output: GeneratorOutput | None = None, *, raises: Exception | None = None
    ) -> None:
        self._output = output or generator_output()
        self._raises = raises
        self.calls = 0

    async def render(
        self,
        spec: AgentShotSpec,
        *,
        narration_text: str,
        voice_id: str,
        reference_image_bytes: list[bytes] | None = None,
        prev_last_frame_bytes: bytes | None = None,
    ) -> GeneratorOutput:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._output


class FakeCritic:
    """Drives the REAL §9.5 routing (``decide_qa``) from canned metrics per call.

    Set ``raises`` to a provider error to simulate the Critic itself failing (the
    §4.11 crash-proofing path).
    """

    def __init__(self, metrics: list[dict[str, Any]]) -> None:
        self._metrics = metrics
        self.calls = 0
        self.raises: Exception | None = None

    async def score(
        self,
        *,
        shot_id: str,
        clip_frames: list[bytes],
        canon_slice: CanonSlice,
        character_crop: bytes | None = None,
        locked_ref_image: bytes | None = None,
        scene_style_centroid: list[float] | None = None,
        textual_evolution_supported: bool = False,
        retries_exhausted: bool = False,
    ) -> QARecord:
        if self.raises is not None:
            raise self.raises
        metrics = self._metrics[min(self.calls, len(self._metrics) - 1)]
        self.calls += 1
        verdict, action, score = decide_qa(
            metrics["ccs"],
            metrics["style"],
            metrics["timeline_ok"],
            metrics["motion"],
            textual_evolution_supported=textual_evolution_supported,
            retries_exhausted=retries_exhausted,
        )
        return QARecord(
            shot_id=shot_id,
            ccs=metrics["ccs"],
            style_drift=metrics["style"],
            timeline_ok=metrics["timeline_ok"],
            contradicting_state_id=metrics.get("state_id"),
            motion_artifact=metrics["motion"],
            score=score,
            verdict=verdict,
            reason="fake-critic",
            repair_action=action,
        )


class FakeNarrator:
    def __init__(self, result: TtsResult | None = None) -> None:
        self._result = result or tts_result()
        self.calls = 0
        self.raises: Exception | None = None

    async def synthesize(self, text: str, *, voice_id: str) -> TtsResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self._result


# --------------------------------------------------------------------------- #
# Conflict-flow doubles (real build_conflict + real decide_arbitration)
# --------------------------------------------------------------------------- #


class FakeContinuity:
    """Raises a real, structured :class:`ConflictObject` for a timeline clash."""

    def __init__(self, *, contradicts: bool = True) -> None:
        self._contradicts = contradicts
        self.calls = 0

    async def check_shot(
        self,
        proposed: Any,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> ContinuityResult:
        self.calls += 1
        if not self._contradicts:
            return ContinuityResult(ok=True, conflict=None)
        judgment = ContinuityJudgment(
            contradicts=True,
            contradicting_state_id=STATE_ID,
            claim="the shot depicts X drawing a sword",
            reasoning="sword was retired earlier",
        )
        conflict = build_conflict(
            judgment,
            shot_id=shot_id,
            current_beat=current_beat_id,
            active_states=list(canon_slice.active_states),
            target_duration_s=target_duration_s,
        )
        return ContinuityResult(ok=False, conflict=conflict)


class FakeShowrunner:
    """Applies the REAL §7.2 policy (``decide_arbitration``) with injected support."""

    def __init__(self, *, supported: bool = False) -> None:
        self._supported = supported
        self.calls = 0

    async def arbitrate(
        self,
        conflict: ConflictObject,
        source_span_text: str,
        *,
        director_present: bool,
        textual_support: TextualSupport | None = None,
    ) -> DecisionRecord:
        self.calls += 1
        supported = textual_support.supported if textual_support is not None else self._supported
        chosen, evolved = decide_arbitration(
            conflict, textual_support=supported, director_present=director_present
        )
        return DecisionRecord(
            conflict_id=conflict.conflict_id,
            chosen_option=chosen,
            reasoning="fake-arbitration",
            evolved_canon=evolved,
        )


@dataclass
class FakeEvolver:
    asserts: list[tuple[str, str, str, int]] = field(default_factory=list)

    async def assert_state(
        self,
        *,
        book_id: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        source_span: dict[str, Any] | None = None,
        state_id: str | None = None,
    ) -> str:
        self.asserts.append((subject_entity_key, predicate, object_value, valid_from_beat))
        return "state_evolved"
