"""Unit tests for LiveEventShotRenderer: it must (a) call Generator.render for
each event-shot (which itself handles the still-bytes→WanSpec translation via
build_wan_spec — this adapter does not reimplement that), and (b) run the SAME
per-shot Critic gate RenderPipeline._render_shot already runs, degrading to
Ken-Burns on repeated failure exactly like the live single-shot path does — so
switching to event granularity cannot silently drop per-shot accuracy checking.

Beyond the plan's five required scenarios (accept-on-pass, retry-then-accept,
degrade-after-retry-cap, immediate-degrade-on-arbitration x2), this file also
locks in the real argument-sourcing decisions made after reading
``RenderPipeline._render_shot``/``_render_live_loop`` in full (see
``live_event_renderer.py``'s module docstring for the full rationale):
``still`` bytes must reach ``Generator.render`` as ``reference_image_bytes``
(the literal bug Task 6's own correction paragraph is about), ``canon_slice``
must never be ``None`` (the real Critic crashes on that), a wired canon reader
is actually used when given one, and the degrade rung falls back to the real
audio/text card (not empty bytes) when there is no still image at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.contracts import Camera, QARecord, RenderMode, RepairAction, SourceSpan, Verdict
from app.agents.generator import GeneratorOutput
from app.memory.budget_service import BudgetExceeded, Reservation
from app.memory.interfaces import CanonSlice
from app.providers.errors import LiveVideoDisabled, ProviderError
from app.providers.types import TtsWord
from app.render import degrade
from app.render.event_director import ContinuityDirective, EventShot
from app.render.live_event_renderer import LiveEventShotRenderer
from tests.conftest import FakeEmbedder
from tests.test_render_support import (
    BEAT_ID,
    BOOK_ID,
    REF_KEY,
    STYLE_REF_KEY,
    FakeCanon,
    FakeDefectRepo,
    FakeObjectStore,
    make_slice,
    png_bytes,
)

# The degrade rung (Ken-Burns / audio-text-card) uses real ffmpeg, exactly like
# RenderPipeline's own degrade ladder — matching test_render_pipeline.py's own
# module-level guard for the same family of real-ffmpeg-backed behaviour.
pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")


def _shot(shot_id: str = "s1", *, beat_id: str | None = None) -> EventShot:
    return EventShot(
        shot_id=shot_id,
        beat_id=beat_id,
        ordinal=0,
        render_mode=RenderMode.VIDEO_CONTINUATION,
        summary="a quiet meadow",
        camera=Camera(),
        duration_s=5.0,
        source_span=SourceSpan(),
        directive=ContinuityDirective(),
    )


class _FakeGenerator:
    """Fakes Generator.render's exact signature (generator.py:195-203) — the
    real class already handles still-bytes→WanSpec translation internally, so
    this fake never needs to construct a WanSpec at all. Records the kwargs of
    its most recent call so tests can verify the real argument-sourcing
    decisions (e.g. `still` -> `reference_image_bytes`, not dropped)."""

    def __init__(self, clip_bytes: bytes = b"CLIP", duration_s: float = 5.0) -> None:
        self._clip_bytes = clip_bytes
        self._duration_s = duration_s
        self.calls = 0
        self.last_spec: Any = None
        self.last_kwargs: dict[str, Any] = {}

    async def render(
        self,
        spec: Any,
        *,
        narration_text: str,
        voice_id: str,
        reference_image_bytes: list[bytes] | None = None,
        prev_last_frame_bytes: bytes | None = None,
    ) -> GeneratorOutput:
        self.calls += 1
        self.last_spec = spec
        self.last_kwargs = {
            "narration_text": narration_text,
            "voice_id": voice_id,
            "reference_image_bytes": reference_image_bytes,
            "prev_last_frame_bytes": prev_last_frame_bytes,
        }
        return GeneratorOutput(
            clip_bytes=self._clip_bytes,
            clip_url=None,
            last_frame_bytes=b"FRAME",
            duration_s=self._duration_s,
            audio_bytes=b"",
            sample_rate=0,
            word_timestamps=[TtsWord(text="hi", t_start=0.0, t_end=0.2)],
            provider_task_id="t1",
        )


def _qa(
    verdict: Verdict, repair_action: RepairAction = RepairAction.ACCEPT, *, shot_id: str = "s1"
) -> QARecord:
    """Build a minimal real QARecord for a fake Critic to return."""
    return QARecord(
        shot_id=shot_id,
        ccs=0.95 if verdict == Verdict.PASS else 0.40,
        style_drift=0.02,
        timeline_ok=True,
        motion_artifact=0.05,
        score=0.9 if verdict == Verdict.PASS else 0.3,
        verdict=verdict,
        repair_action=repair_action,
    )


class _FakeCriticAccept:
    async def score(self, **kwargs: Any) -> QARecord:
        return _qa(Verdict.PASS)


async def test_renders_via_generator_and_accepts_on_critic_pass() -> None:
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.calls == 1
    assert result.clip_bytes == b"CLIP"
    assert result.shot_id == "s1"
    assert result.degraded is False


class _FakeCriticRejectThenAccept:
    def __init__(self) -> None:
        self.calls = 0

    async def score(self, **kwargs: Any) -> QARecord:
        self.calls += 1
        if self.calls == 1:
            return _qa(Verdict.FAIL, RepairAction.REGEN_NEW_SEED)
        return _qa(Verdict.PASS)


async def test_retries_on_critic_fail_then_accepts() -> None:
    generator = _FakeGenerator()
    critic = _FakeCriticRejectThenAccept()
    renderer = LiveEventShotRenderer(generator=generator, critic=critic, max_retries=2)
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.calls == 2  # rendered again after the first Critic rejection
    assert critic.calls == 2
    assert result.degraded is False


class _FakeCriticAlwaysReject:
    async def score(self, **kwargs: Any) -> QARecord:
        return _qa(Verdict.FAIL, RepairAction.REGEN_NEW_SEED)


async def test_degrades_to_kenburns_after_retry_cap_exhausted() -> None:
    still = png_bytes()  # a real image: the degrade rung really runs ffmpeg
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAlwaysReject(), max_retries=2
    )
    result = await renderer.render_shot(_shot(), still=still, audio=None)
    assert generator.calls == 2  # both allowed attempts spent before degrading
    assert result.degraded is True  # RenderedShot gains a `degraded: bool = False` field (Step 3)
    assert result.clip_bytes  # a real, playable Ken-Burns mp4 — never empty bytes
    assert result.last_frame_bytes == still


class _FakeCriticRaisesConflict:
    async def score(self, **kwargs: Any) -> QARecord:
        return _qa(Verdict.FAIL, RepairAction.RAISE_CONFLICT)


async def test_conflict_degrades_immediately_without_retry() -> None:
    """RAISE_CONFLICT needs arbitration this adapter can't do — degrade
    immediately (no retry loop) rather than burn retries pointlessly."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticRaisesConflict(), max_retries=3
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert generator.calls == 1  # no retries burned on an un-retryable outcome
    assert result.degraded is True


class _FakeCriticEvolvesCanon:
    async def score(self, **kwargs: Any) -> QARecord:
        return _qa(Verdict.FAIL, RepairAction.EVOLVE_CANON)


async def test_evolve_canon_also_degrades_immediately_without_retry() -> None:
    """EVOLVE_CANON is the same 'needs arbitration' bucket as RAISE_CONFLICT —
    both route to an immediate degrade, never a retry."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticEvolvesCanon(), max_retries=3
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert generator.calls == 1
    assert result.degraded is True


async def test_defect_logged_when_arbitration_needed_and_repo_wired() -> None:
    """RAISE_CONFLICT/EVOLVE_CANON must surface in the campaign's defect log
    (DefectRepo.log), not vanish silently, when a defect_repo is wired."""
    generator = _FakeGenerator()
    defects = FakeDefectRepo()
    renderer = LiveEventShotRenderer(
        generator=generator,
        critic=_FakeCriticRaisesConflict(),
        defect_repo=defects,
        book_id="book_demo",
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert len(defects.logged) == 1
    logged = defects.logged[0]
    assert logged["book_id"] == "book_demo"
    assert logged["kind"] == "event_shot_needs_arbitration"
    assert logged["shot_id"] == "s1"
    assert logged["detail"] == {"repair_action": "raise_conflict"}


async def test_no_defect_logged_when_repo_not_wired() -> None:
    """defect_repo=None (the unit-test default) must not be dereferenced."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticRaisesConflict())
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True  # no AttributeError from a None defect_repo


# --------------------------------------------------------------------------- #
# The real argument-sourcing pattern (pipeline.py-derived), not the brief's
# literal placeholders — see live_event_renderer.py's module docstring.
# --------------------------------------------------------------------------- #


async def test_still_bytes_flow_into_reference_image_bytes_not_prev_frame() -> None:
    """The actual bug Task 6's correction paragraph is about: `still` bytes
    must reach Generator.render as `reference_image_bytes` (never silently
    dropped, never used to hand-build a WanSpec directly) — and
    `prev_last_frame_bytes` stays None, since EventDirector's concurrent
    fan-out (asyncio.gather) makes a sibling shot's last frame unavailable
    here."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.last_kwargs["reference_image_bytes"] == [b"STILL"]
    assert generator.last_kwargs["prev_last_frame_bytes"] is None


async def test_no_still_means_no_reference_image_bytes() -> None:
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    await renderer.render_shot(_shot(), still=None, audio=None)
    assert generator.last_kwargs["reference_image_bytes"] is None


class _FakeCriticCapturing:
    """Returns PASS but records every kwargs dict it was scored with, and
    touches `canon_slice.active_states` exactly like the real Critic._vision
    does — so a regression to `canon_slice=None` fails this fake loudly the
    same way it would crash the real Critic, instead of silently."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def score(self, **kwargs: Any) -> QARecord:
        self.calls.append(kwargs)
        _ = kwargs["canon_slice"].active_states  # would raise on None, like the real Critic
        return _qa(Verdict.PASS)


async def test_canon_slice_passed_to_critic_is_never_none_without_canon_reader() -> None:
    """No canon reader wired (the unit-test default) must still produce a real
    CanonSlice for the Critic, never None — Critic._vision reads
    canon_slice.active_states unconditionally."""
    generator = _FakeGenerator()
    critic = _FakeCriticCapturing()
    renderer = LiveEventShotRenderer(generator=generator, critic=critic, book_id="book_demo")
    await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert len(critic.calls) == 1
    canon_slice = critic.calls[0]["canon_slice"]
    assert isinstance(canon_slice, CanonSlice)
    assert canon_slice.book_id == "book_demo"


async def test_uses_wired_canon_reader_for_canon_slice_and_voice_id() -> None:
    """When a canon reader IS wired and the shot carries a beat_id, the real
    CanonSlice is queried (mirroring RenderPipeline._render_shot's own
    `canon.query(book_id, beat_id)`) and its locked character's voice drives
    narration — mirroring RenderPipeline._voice_id."""
    generator = _FakeGenerator()
    canon = FakeCanon(make_slice())  # book_id=BOOK_ID, beat_id=BEAT_ID, voice vc_x
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), canon=canon, book_id=BOOK_ID
    )
    await renderer.render_shot(_shot(beat_id=BEAT_ID), still=b"STILL", audio=None)
    assert canon.queries == [(BOOK_ID, BEAT_ID)]
    assert generator.last_kwargs["voice_id"] == "vc_x"


async def test_falls_back_to_default_voice_without_canon_reader() -> None:
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), default_voice="Cherry"
    )
    await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.last_kwargs["voice_id"] == "Cherry"


async def test_narration_text_falls_back_to_shot_summary() -> None:
    """No PageOps wired (unlike RenderPipeline), so the beat/segment summary —
    RenderPipeline._narration_text's own fallback when there is no page-word-
    box match — is this adapter's only available narration source."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert generator.last_kwargs["narration_text"] == "a quiet meadow"


async def test_word_timestamps_carried_through_on_accept() -> None:
    """The Generator's real TTS timing (GeneratorOutput.word_timestamps)
    survives onto the RenderedShot, exactly like RenderPipeline's own
    build_sync_segment(word_timestamps=output.word_timestamps) usage — the
    off-gate KenBurnsEventRenderer can't do this (it has no TTS of its own),
    but the live renderer's Generator does synthesize real timings."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert len(result.word_timestamps) == 1
    assert result.word_timestamps[0].text == "hi"


async def test_degrades_to_audio_text_card_when_no_still_available() -> None:
    """No still + exhausted retries falls back to the real audio/text card rung
    (KenBurnsEventRenderer's own bottom rung) — never empty/invalid bytes."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAlwaysReject(), max_retries=1
    )
    result = await renderer.render_shot(_shot(), still=None, audio=None)
    assert result.degraded is True
    assert result.clip_bytes  # a real, playable mp4 — never empty bytes
    assert result.last_frame_bytes is None


# --------------------------------------------------------------------------- #
# Fix 1 (Important, review c6fbcfb): render_shot had NO exception handling
# around Generator.render / Critic.score. EventDirector.render_event fans every
# shot out via a bare `asyncio.gather(...)` with no try/except and no
# `return_exceptions=True`, so an uncaught ProviderError/LiveVideoDisabled from
# either call crashes the ENTIRE event's render, not just this one shot. The
# baseline this class must match is RenderPipeline._render_live_loop's own
# `except (LiveVideoDisabled, ProviderError)` around the same two calls
# (pipeline.py:664-674, :683-704), which degrades instead of propagating.
# --------------------------------------------------------------------------- #


class _FakeGeneratorRaises:
    """A Generator whose render() call always fails with a real provider error —
    mirrors the reviewer's own reproduction of the uncaught-exception bug."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def render(
        self,
        spec: Any,
        *,
        narration_text: str,
        voice_id: str,
        reference_image_bytes: list[bytes] | None = None,
        prev_last_frame_bytes: bytes | None = None,
    ) -> GeneratorOutput:
        self.calls += 1
        raise self._exc


class _FakeCriticRaises:
    """A Critic whose score() call always fails with a real provider error."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def score(self, **kwargs: Any) -> QARecord:
        self.calls += 1
        raise self._exc


async def test_generator_provider_error_degrades_instead_of_propagating() -> None:
    """A ProviderError from Generator.render must not propagate out of
    render_shot — it must degrade this shot instead, exactly like a
    non-retryable QA fail does."""
    generator = _FakeGeneratorRaises(ProviderError("wan is down"))
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert result.clip_bytes  # a real, playable Ken-Burns mp4 — never empty bytes
    assert generator.calls == 1  # no retry burned on a provider outage


async def test_generator_live_video_disabled_degrades_instead_of_propagating() -> None:
    """LiveVideoDisabled (the deliberate KINORA_LIVE_VIDEO=off gate) is not a
    bug, but must still degrade rather than propagate, exactly like a real
    ProviderError — RenderPipeline's own baseline catches both identically."""
    generator = _FakeGeneratorRaises(LiveVideoDisabled("KINORA_LIVE_VIDEO is off"))
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept())
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert result.clip_bytes


async def test_critic_provider_error_degrades_instead_of_propagating() -> None:
    """The Critic side of the same gap: a ProviderError from Critic.score must
    also degrade this shot rather than propagate."""
    generator = _FakeGenerator()
    critic = _FakeCriticRaises(ProviderError("vl critic is down"))
    renderer = LiveEventShotRenderer(generator=generator, critic=critic)
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert result.clip_bytes
    assert generator.calls == 1  # no retry burned on an unavailable Critic


async def test_critic_live_video_disabled_degrades_instead_of_propagating() -> None:
    generator = _FakeGenerator()
    critic = _FakeCriticRaises(LiveVideoDisabled("KINORA_LIVE_VIDEO is off"))
    renderer = LiveEventShotRenderer(generator=generator, critic=critic)
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert result.clip_bytes


# --------------------------------------------------------------------------- #
# Fix 2 (Important, review c6fbcfb): Critic._ccs / Critic._style_drift are
# trivially-passing whenever locked_ref_image / scene_style_centroid are None —
# and this class hardcoded both to None with no path to anything else, making
# the identity + style-drift checks permanently inert for every shot it
# renders. build_render_pipeline's real factory always wires an object_store +
# embedder, so this is a real production-parity gap, not just a test gap.
# --------------------------------------------------------------------------- #


async def test_locked_ref_image_is_real_when_object_store_and_canon_wired() -> None:
    """With a locked character reference present in the canon slice and a real
    object store wired, locked_ref_image reaching the Critic must be the real
    image bytes — not the hardcoded None that made Critic._ccs permanently
    trivially-passing for every shot this class renders."""
    generator = _FakeGenerator()
    critic = _FakeCriticCapturing()
    ref_bytes = png_bytes(64, 64)
    store = FakeObjectStore({REF_KEY: ref_bytes})
    canon = FakeCanon(make_slice())
    renderer = LiveEventShotRenderer(
        generator=generator,
        critic=critic,
        canon=canon,
        book_id=BOOK_ID,
        object_store=store,
    )
    await renderer.render_shot(_shot(beat_id=BEAT_ID), still=b"STILL", audio=None)
    assert critic.calls[0]["locked_ref_image"] == ref_bytes


async def test_locked_ref_image_stays_none_without_object_store() -> None:
    """No object_store wired (the unit-test default) must not crash — and must
    keep the documented gate-inert fallback rather than guess at bytes."""
    generator = _FakeGenerator()
    critic = _FakeCriticCapturing()
    canon = FakeCanon(make_slice())
    renderer = LiveEventShotRenderer(
        generator=generator, critic=critic, canon=canon, book_id=BOOK_ID
    )
    await renderer.render_shot(_shot(beat_id=BEAT_ID), still=b"STILL", audio=None)
    assert critic.calls[0]["locked_ref_image"] is None


async def test_scene_style_centroid_is_real_when_style_node_and_embedder_wired() -> None:
    """With a Style node's locked reference present and a real embedder + object
    store wired, scene_style_centroid reaching the Critic must be a real
    embedding vector — not the hardcoded None that made Critic._style_drift
    permanently trivially-passing."""
    generator = _FakeGenerator()
    critic = _FakeCriticCapturing()
    style_ref = png_bytes(96, 96)
    store = FakeObjectStore({STYLE_REF_KEY: style_ref})
    embedder = FakeEmbedder()
    canon = FakeCanon(make_slice(with_style=True))
    renderer = LiveEventShotRenderer(
        generator=generator,
        critic=critic,
        canon=canon,
        book_id=BOOK_ID,
        object_store=store,
        embedder=embedder,
    )
    await renderer.render_shot(_shot(beat_id=BEAT_ID), still=b"STILL", audio=None)
    centroid = critic.calls[0]["scene_style_centroid"]
    assert centroid is not None
    expected = (await embedder.embed_images([style_ref]))[0]
    assert centroid == expected


async def test_scene_style_centroid_stays_none_without_embedder() -> None:
    """An object_store alone (no embedder) must not compute a centroid — mirrors
    RenderPipeline._scene_style_centroid's own None-if-no-embedder fallback."""
    generator = _FakeGenerator()
    critic = _FakeCriticCapturing()
    store = FakeObjectStore({STYLE_REF_KEY: png_bytes(96, 96)})
    canon = FakeCanon(make_slice(with_style=True))
    renderer = LiveEventShotRenderer(
        generator=generator,
        critic=critic,
        canon=canon,
        book_id=BOOK_ID,
        object_store=store,
    )
    await renderer.render_shot(_shot(beat_id=BEAT_ID), still=b"STILL", audio=None)
    assert critic.calls[0]["scene_style_centroid"] is None


# --------------------------------------------------------------------------- #
# Finding 3 (resilience audit): this class had NO budget accounting at all —
# an event-granularity render spent real Wan/MiniMax seconds without ever
# reserving/committing against BudgetService, so the Scheduler's own ledger
# and its live-gate/low-buffer checks never saw the spend. The baseline this
# class must match is RenderPipeline._render_shot's pre-loop gate + the
# reserve/commit/release around Generator.render inside _render_live_loop
# (pipeline.py:628, :654-674). `budget=None` (every test above) must keep
# behaving exactly as before — these tests exercise `budget` wired in.
# --------------------------------------------------------------------------- #


class _FakeBudget:
    """A minimal real BudgetOps double: records reserve/commit/release calls
    instead of touching a DB, and can simulate the gate or a hard cap."""

    def __init__(
        self, *, can_render: bool = True, low: bool = False, exceeded: bool = False
    ) -> None:
        self._can_render = can_render
        self._low = low
        self._exceeded = exceeded
        self.reserved: list[float] = []
        self.committed: list[tuple[Reservation, float | None]] = []
        self.released: list[Reservation] = []
        self._next_id = 0

    def can_render_live(self) -> bool:
        return self._can_render

    async def is_low(self) -> bool:
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
        if self._exceeded:
            raise BudgetExceeded("scene", requested=video_seconds, used=0.0, cap=0.0)
        self._next_id += 1
        reservation = Reservation(
            id=f"r{self._next_id}", video_seconds=video_seconds, book_id=book_id, scene_id=scene_id
        )
        self.reserved.append(video_seconds)
        return reservation

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None:
        self.committed.append((reservation, actual_seconds))

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None:
        self.released.append(reservation)


async def test_budget_none_preserves_unmetered_behaviour() -> None:
    """The default (no budget wired) must render exactly as every test above
    already assumes — no gate check, no reserve/commit/release calls made."""
    generator = _FakeGenerator()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept(), budget=None)
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert result.degraded is False
    assert generator.calls == 1


async def test_budget_gate_closed_skips_straight_to_degrade() -> None:
    """can_render_live() False (KINORA_LIVE_VIDEO off) must degrade without
    ever calling the generator — mirrors RenderPipeline's own pre-loop gate."""
    generator = _FakeGenerator()
    budget = _FakeBudget(can_render=False)
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), budget=budget
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert generator.calls == 0
    assert result.degraded is True
    assert budget.reserved == []


async def test_budget_low_skips_straight_to_degrade() -> None:
    """is_low() True (remaining seconds below the floor) is the other half of
    the same pre-loop gate."""
    generator = _FakeGenerator()
    budget = _FakeBudget(low=True)
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), budget=budget
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert generator.calls == 0
    assert result.degraded is True


async def test_budget_reserved_and_committed_on_successful_render() -> None:
    """A real render must reserve the shot's duration up front and commit the
    actual rendered duration once Generator.render succeeds."""
    generator = _FakeGenerator(duration_s=4.5)
    budget = _FakeBudget()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), budget=budget, book_id="book_demo"
    )
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert result.degraded is False
    assert budget.reserved == [5.0]  # shot.duration_s, before the actual is known
    assert len(budget.committed) == 1
    committed_reservation, actual = budget.committed[0]
    assert committed_reservation.id == "r1"
    assert actual == 4.5  # the Generator's real output duration, not the target
    assert budget.released == []  # committed, never released, on success


async def test_budget_exceeded_on_reserve_degrades_without_calling_generator() -> None:
    """BudgetExceeded from reserve() is a hard cap hit — degrade immediately,
    never call the generator (would spend real, unbudgeted provider seconds)."""
    generator = _FakeGenerator()
    budget = _FakeBudget(exceeded=True)
    renderer = LiveEventShotRenderer(
        generator=generator, critic=_FakeCriticAccept(), budget=budget
    )
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert generator.calls == 0
    assert result.degraded is True


async def test_budget_released_when_generator_provider_error() -> None:
    """A reservation outstanding when Generator.render fails with a provider
    error must be released, not left dangling or wrongly committed."""
    generator = _FakeGeneratorRaises(ProviderError("wan is down"))
    budget = _FakeBudget()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept(), budget=budget)
    result = await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert result.degraded is True
    assert budget.reserved == [5.0]
    assert len(budget.released) == 1
    assert budget.committed == []


async def test_budget_released_when_generator_raises_unclassified_exception() -> None:
    """Regression (independent review finding, 2026-07-05): an exception from
    Generator.render that isn't LiveVideoDisabled/ProviderError must still
    release the outstanding reservation before propagating — without this,
    it would leak (never committed, never released), permanently eroding the
    budget toward is_low(). EventDirector's own gather-level catch
    (return_exceptions=True) is what degrades the shot; this class's job is
    only to not leak the reservation on the way out."""
    generator = _FakeGeneratorRaises(RuntimeError("unexpected bug"))
    budget = _FakeBudget()
    renderer = LiveEventShotRenderer(generator=generator, critic=_FakeCriticAccept(), budget=budget)
    with pytest.raises(RuntimeError, match="unexpected bug"):
        await renderer.render_shot(_shot(), still=png_bytes(), audio=None)
    assert budget.reserved == [5.0]
    assert len(budget.released) == 1
    assert budget.committed == []


async def test_budget_reserves_and_commits_once_per_retry_attempt() -> None:
    """Each retry attempt is its own render, hence its own reserve/commit pair —
    mirrors RenderPipeline._render_live_loop's own per-attempt accounting."""
    generator = _FakeGenerator()
    critic = _FakeCriticRejectThenAccept()
    budget = _FakeBudget()
    renderer = LiveEventShotRenderer(
        generator=generator, critic=critic, budget=budget, max_retries=2
    )
    result = await renderer.render_shot(_shot(), still=b"STILL", audio=None)
    assert result.degraded is False
    assert generator.calls == 2
    assert budget.reserved == [5.0, 5.0]
    assert len(budget.committed) == 2
    assert budget.released == []
