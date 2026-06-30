"""Local, self-contained deterministic fakes for the end-to-end harness.

These are the in-memory doubles the :class:`~app.e2e.world.FakeWorld` wires into
the *real* :class:`~app.render.pipeline.RenderPipeline`. They are deliberately
held inside ``app/e2e`` (not ``tests/``) so the harness is importable as a
reusable package — but they mirror the contracts the pipeline's collaborators
satisfy on main (canon reader, cache, budget, repos, the heavy agent/provider
calls). Every byte produced is deterministic: real PNG/WAV/mp4 builders feed the
ffmpeg degradation ladder, and the Critic/Showrunner doubles drive the *real*
§9.5 routing / §7.2 arbitration policy functions rather than hard-coding verdicts.

No network, no DashScope, no database. ``KINORA_LIVE_VIDEO`` stays off — the
"live" Generator double simply returns canned bytes, it never spends.
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
from app.memory.budget_service import BudgetExceeded, Reservation
from app.memory.interfaces import CanonSlice
from app.providers.types import TtsResult, TtsWord
from app.render import degrade

# --------------------------------------------------------------------------- #
# Real, deterministic asset builders (lazy, cached — importing never needs ffmpeg)
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=8)
def png_bytes(width: int = 1280, height: int = 720) -> bytes:
    """A real deterministic gradient PNG (cached by size)."""
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
    """A real mono 16-bit WAV tone (cached, deterministic)."""
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


def _tts_words(texts: list[str], *, duration_s: float) -> list[TtsWord]:
    """Deterministic, evenly-spaced word timings spanning ``[0, duration_s]``."""
    if not texts:
        return []
    step = duration_s / len(texts)
    words: list[TtsWord] = []
    for i, text in enumerate(texts):
        words.append(TtsWord(text=text, t_start=round(i * step, 3), t_end=round((i + 1) * step, 3)))
    return words


def tts_result(narration_text: str, *, duration_s: float = 2.0) -> TtsResult:
    """A real-WAV-backed TTS result whose word timings cover ``narration_text``."""
    texts = narration_text.split() or ["…"]
    return TtsResult(
        audio_bytes=wav_bytes(duration_s),
        sample_rate=24000,
        duration_s=duration_s,
        word_timestamps=_tts_words(texts, duration_s=duration_s),
        alignment="proportional",
        voice_id="vc_fake",
        model="fake-tts",
    )


# --------------------------------------------------------------------------- #
# In-memory rows + repositories (match the pipeline's BeatRow/ShotRow/PageRow)
# --------------------------------------------------------------------------- #


@dataclass
class RowShot:
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
    reference_set_hash: str | None = None


@dataclass
class RowBeat:
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
class RowPage:
    word_boxes: list[dict[str, Any]] | None
    image_key: str | None
    text: str | None


class FakeShotRepo:
    """In-memory ``ShotOps`` recording every transition for trace assertions."""

    def __init__(self, shots: list[RowShot]) -> None:
        self._shots = {s.id: s for s in shots}
        self.statuses: dict[str, list[ShotStatus]] = {}
        self.accepted: list[str] = []
        self.updates: list[dict[str, Any]] = []

    async def get(self, shot_id: str) -> RowShot | None:
        return self._shots.get(shot_id)

    async def set_status(self, shot_id: str, status: ShotStatus) -> None:
        self.statuses.setdefault(shot_id, []).append(status)
        if shot_id in self._shots:
            self._shots[shot_id].status = status

    async def mark_accepted(self, shot_id: str) -> None:
        self.accepted.append(shot_id)
        if shot_id in self._shots:
            self._shots[shot_id].status = ShotStatus.ACCEPTED

    async def update(self, shot_id: str, **fields: Any) -> RowShot | None:
        self.updates.append({"shot_id": shot_id, **fields})
        shot = self._shots.get(shot_id)
        if shot is None:
            return None
        for key, value in fields.items():
            setattr(shot, key, value)
        return shot


class FakeBeatRepo:
    def __init__(self, beats: list[RowBeat]) -> None:
        self._beats = {b.id: b for b in beats}

    async def get(self, beat_id: str) -> RowBeat | None:
        return self._beats.get(beat_id)


class FakePageRepo:
    def __init__(self, pages: dict[int, RowPage]) -> None:
        self._pages = pages

    async def get_by_number(self, book_id: str, page_number: int) -> RowPage | None:
        return self._pages.get(page_number)


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
    """Returns a per-beat :class:`CanonSlice` (defaults to a single shared slice)."""

    def __init__(
        self, default_slice: CanonSlice, *, per_beat: dict[str, CanonSlice] | None = None
    ) -> None:
        self._default = default_slice
        self._per_beat = dict(per_beat or {})
        self.queries: list[tuple[str, str]] = []

    async def query(self, book_id: str, beat_id: str) -> CanonSlice:
        self.queries.append((book_id, beat_id))
        return self._per_beat.get(beat_id, self._default)


@dataclass
class CachedEntry:
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
        self.store: dict[str, CachedEntry] = {}
        self.puts: list[dict[str, Any]] = []
        self.hits = 0
        self.misses = 0

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

    async def get(self, shot_hash: str) -> CachedEntry | None:
        entry = self.store.get(shot_hash)
        if entry is not None and entry.clip_key:
            self.hits += 1
        else:
            self.misses += 1
        return entry

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
    ) -> CachedEntry:
        record = CachedEntry(clip_key, last_frame_key, sync_segment, qa, video_seconds)
        self.store[shot_hash] = record
        self.puts.append(
            {"shot_hash": shot_hash, "clip_key": clip_key, "video_seconds": video_seconds}
        )
        return record


class FakeBudget:
    """Reserve/commit/release ledger over a finite video-second budget (§9.7).

    ``can_render_live`` mirrors the ``KINORA_LIVE_VIDEO`` gate. ``budget_s`` is a
    finite pool; reserving past it raises :class:`BudgetExceeded`, and committing
    debits actual seconds. The harness asserts no *double-spend* against the
    committed ledger.
    """

    def __init__(
        self, *, live: bool = True, budget_s: float = 1_000.0, low_floor_s: float = 0.0
    ) -> None:
        self._live = live
        self._budget_s = float(budget_s)
        self._low_floor_s = float(low_floor_s)
        self._reserved_outstanding = 0.0
        self._committed_total = 0.0
        self.reserve_calls: list[float] = []
        self.commit_calls: list[float] = []
        self.release_calls = 0

    # -- gates -------------------------------------------------------------- #
    def can_render_live(self) -> bool:
        return self._live

    async def remaining(self) -> float:
        return self._budget_s - self._committed_total - self._reserved_outstanding

    async def is_low(self) -> bool:
        return (await self.remaining()) <= self._low_floor_s

    def is_low_at(self, remaining: float) -> bool:
        return remaining <= self._low_floor_s

    # -- ledger ------------------------------------------------------------- #
    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation:
        used = self._committed_total + self._reserved_outstanding
        if used + video_seconds > self._budget_s:
            raise BudgetExceeded(
                "video_seconds",
                requested=video_seconds,
                used=used,
                cap=self._budget_s,
            )
        self._reserved_outstanding += video_seconds
        self.reserve_calls.append(video_seconds)
        return Reservation(id=f"res_{len(self.reserve_calls)}", video_seconds=video_seconds)

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        actual = actual_seconds if actual_seconds is not None else reservation.video_seconds
        outstanding = self._reserved_outstanding - reservation.video_seconds
        self._reserved_outstanding = max(0.0, outstanding)
        self._committed_total += actual
        self.commit_calls.append(actual)

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        outstanding = self._reserved_outstanding - reservation.video_seconds
        self._reserved_outstanding = max(0.0, outstanding)
        self.release_calls += 1

    @property
    def committed_total(self) -> float:
        return self._committed_total


class FakeEpisodic:
    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    async def log(self, **kwargs: Any) -> dict[str, Any]:
        self.logged.append(kwargs)
        return kwargs


class FakeObjectStore:
    """In-memory :class:`BlobStore` recording every put (a virtual MinIO)."""

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
# Heavy agent / provider doubles
# --------------------------------------------------------------------------- #


class FakeDesigner:
    """Cinematographer double — a deterministic, seed-stable :class:`ShotSpec`."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_notes: list[Any] | None = None

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
        # A note nudges the seed so a comment-driven regen is observably different.
        note_salt = len(director_notes or []) * 1000
        return AgentShotSpec(
            shot_id=shot_id or "shot_unknown",
            beat_id=beat.beat_id or None,
            scene_id=beat.scene_id,
            render_mode=RenderMode.REFERENCE_TO_VIDEO,
            prompt=beat.described_visuals or beat.summary or "a quiet figure",
            negative_prompt="warped face",
            reference_image_ids=[f"{canon_slice.characters[0].entity_key}@v1"]
            if canon_slice.characters
            else [],
            seed=70000 + note_salt + (beat.beat_index or 0),
            target_duration_s=target_duration_s,
        )


class FakeGenerator:
    """Wan double — canned clip bytes; can be told to fail a fixed number of times.

    ``fail_first`` simulates a provider failover: the first N renders raise the
    supplied error, then subsequent renders succeed (the real pipeline's repair
    loop / degradation ladder reacts).
    """

    def __init__(
        self,
        output: GeneratorOutput | None = None,
        *,
        raises: Exception | None = None,
        fail_first: int = 0,
        fail_error: Exception | None = None,
    ) -> None:
        self._output = output
        self._raises = raises
        self._fail_first = fail_first
        self._fail_error = fail_error
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
        if self.calls <= self._fail_first:
            from app.providers.errors import ProviderError

            raise self._fail_error or ProviderError("simulated provider failure")
        return self._output or _default_generator_output(narration_text, spec.target_duration_s)


def _default_generator_output(narration_text: str, duration_s: float) -> GeneratorOutput:
    """A canned successful Generator result whose timings cover the narration."""
    texts = narration_text.split() or ["…"]
    return GeneratorOutput(
        clip_bytes=real_mp4(1.0, with_audio=True),
        clip_url=None,
        last_frame_bytes=png_bytes(640, 360),
        duration_s=duration_s,
        audio_bytes=wav_bytes(2.0),
        sample_rate=24000,
        word_timestamps=_tts_words(texts, duration_s=2.0),
        provider_task_id="fake-task",
    )


class FakeCritic:
    """Drives the REAL §9.5 routing (``decide_qa``) from canned metrics per call."""

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
    """TTS double — a real-WAV-backed result whose timings cover the narration."""

    def __init__(self, *, duration_s: float = 2.0) -> None:
        self._duration_s = duration_s
        self.calls = 0
        self.raises: Exception | None = None

    async def synthesize(self, text: str, *, voice_id: str) -> TtsResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return tts_result(text, duration_s=self._duration_s)


class FakeContinuity:
    """Raises a real, structured :class:`ConflictObject` for a timeline clash."""

    def __init__(self, *, contradicts: bool = False, state_id: str = "state_x") -> None:
        self._contradicts = contradicts
        self._state_id = state_id
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
        states = list(canon_slice.active_states)
        state_id = states[0].state_id if states else self._state_id
        judgment = ContinuityJudgment(
            contradicts=True,
            contradicting_state_id=state_id,
            claim="the shot contradicts an active state",
            reasoning="the depicted action breaks an earlier-established fact",
        )
        conflict = build_conflict(
            judgment,
            shot_id=shot_id,
            current_beat=current_beat_id,
            active_states=states,
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


__all__ = [
    "CachedEntry",
    "FakeBeatRepo",
    "FakeBudget",
    "FakeCache",
    "FakeCanon",
    "FakeContinuity",
    "FakeCritic",
    "FakeDefectRepo",
    "FakeDesigner",
    "FakeEpisodic",
    "FakeEvolver",
    "FakeGenerator",
    "FakeNarrator",
    "FakeObjectStore",
    "FakePageRepo",
    "FakeShotRepo",
    "FakeShowrunner",
    "RowBeat",
    "RowPage",
    "RowShot",
    "png_bytes",
    "real_mp4",
    "tts_result",
    "wav_bytes",
]
