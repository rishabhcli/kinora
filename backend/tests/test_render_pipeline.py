"""The per-shot Phase-B orchestrator end-to-end (kinora.md §9.2/§9.5/§9.7).

Exercised with injected doubles for the heavy provider calls (Cinematographer /
Generator / Critic / TTS) and in-memory fakes for the memory services + repos,
so the whole §9.7 state machine — cache hit, live accept, the §9.5 repair loop,
the §7.2 conflict flow, and the real ffmpeg degradation ladder — runs without a
database or DashScope. The degradation artifacts are real mp4s (verified here
with ffprobe). ``KINORA_LIVE_VIDEO`` stays off; no real Wan render happens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from app.agents.contracts import QARecord
from app.agents.critic import decide_qa
from app.core.config import Settings
from app.db.models.enums import ShotStatus
from app.memory.interfaces import CanonSlice
from app.render import degrade
from app.render.conflict import ConflictResolver
from app.render.pipeline import RenderPipeline, RenderResult
from app.storage.object_store import keys
from tests.conftest import FakeEmbedder
from tests.test_render_support import (
    BEAT_ID,
    BOOK_ID,
    REF_KEY,
    SCENE_ID,
    SHOT_ID,
    STYLE_REF_KEY,
    FakeBeat,
    FakeBudget,
    FakeCache,
    FakeCached,
    FakeCanon,
    FakeContinuity,
    FakeCritic,
    FakeDefectRepo,
    FakeDesigner,
    FakeEpisodic,
    FakeEvolver,
    FakeGenerator,
    FakeNarrator,
    FakeObjectStore,
    FakePage,
    FakeShot,
    FakeShotRepo,
    FakeShowrunner,
    make_slice,
    png_bytes,
    word_boxes,
)

pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")

_SPAN = {"page": 12, "word_range": [100, 102]}
_PASS = {"ccs": 0.95, "style": 0.02, "timeline_ok": True, "motion": 0.05}
_IDENTITY_FAIL = {"ccs": 0.50, "style": 0.02, "timeline_ok": True, "motion": 0.05}
_TIMELINE_FAIL = {
    "ccs": 0.95,
    "style": 0.02,
    "timeline_ok": False,
    "motion": 0.05,
    "state_id": "state_sword_001",
}


@dataclass
class Bundle:
    pipeline: RenderPipeline
    shots: FakeShotRepo
    cache: FakeCache
    budget: FakeBudget
    episodic: FakeEpisodic
    defects: FakeDefectRepo
    store: FakeObjectStore
    designer: FakeDesigner
    generator: FakeGenerator
    critic: FakeCritic
    narrator: FakeNarrator
    continuity: FakeContinuity | None = None
    showrunner: FakeShowrunner | None = None
    evolver: FakeEvolver | None = None


def make_bundle(
    *,
    critic_metrics: list[dict],
    budget_live: bool = True,
    budget_low: bool = False,
    generator: FakeGenerator | None = None,
    shot_hash: str | None = None,
    cached: FakeCached | None = None,
    seed_store: dict[str, bytes] | None = None,
    page_image_key: str | None = None,
    conflict: bool = False,
    arbiter_supported: bool = False,
) -> Bundle:
    shot = FakeShot(
        id=SHOT_ID,
        book_id=BOOK_ID,
        beat_id=BEAT_ID,
        scene_id=SCENE_ID,
        source_span=dict(_SPAN),
        duration_s=5.0,
        shot_hash=shot_hash,
    )
    beat = FakeBeat(
        id=BEAT_ID,
        book_id=BOOK_ID,
        scene_id=SCENE_ID,
        beat_index=7,
        summary="X stands at the window.",
        entities=["char_x"],
        described_visuals="a quiet figure at a frosted window",
        mood="still",
        source_span=dict(_SPAN),
    )
    page = FakePage(word_boxes=word_boxes(), image_key=page_image_key, text="She stood still")

    shots = FakeShotRepo(shot)
    cache = FakeCache()
    if shot_hash is not None and cached is not None:
        cache.store[shot_hash] = cached
    budget = FakeBudget(live=budget_live, low=budget_low)
    episodic = FakeEpisodic()
    defects = FakeDefectRepo()
    store = FakeObjectStore(seed_store)
    designer = FakeDesigner()
    gen = generator or FakeGenerator()
    critic = FakeCritic(critic_metrics)
    narrator = FakeNarrator()

    resolver = continuity = showrunner = evolver = None
    if conflict:
        continuity = FakeContinuity(contradicts=True)
        showrunner = FakeShowrunner(supported=arbiter_supported)
        evolver = FakeEvolver()
        resolver = ConflictResolver(continuity=continuity, showrunner=showrunner, canon=evolver)

    pipeline = RenderPipeline(
        canon=FakeCanon(make_slice()),
        episodic=episodic,
        cache=cache,
        budget=budget,
        object_store=store,
        shots=shots,
        beats=FakeBeatRepoFor(beat),
        pages=FakePageRepoFor(page),
        defects=defects,
        designer=designer,
        generator=gen,
        critic=critic,
        narrator=narrator,
        conflict_resolver=resolver,
        image_gen=None,
        settings=Settings(dashscope_api_key="test"),
    )
    return Bundle(
        pipeline=pipeline,
        shots=shots,
        cache=cache,
        budget=budget,
        episodic=episodic,
        defects=defects,
        store=store,
        designer=designer,
        generator=gen,
        critic=critic,
        narrator=narrator,
        continuity=continuity,
        showrunner=showrunner,
        evolver=evolver,
    )


# Tiny repo adapters that bind a single row (kept here so support stays generic).
class FakeBeatRepoFor:
    def __init__(self, beat: FakeBeat) -> None:
        self._beat = beat

    async def get(self, beat_id: str) -> FakeBeat | None:
        return self._beat if beat_id == self._beat.id else None


class FakePageRepoFor:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def get_by_number(self, book_id: str, page_number: int) -> FakePage | None:
        return self._page


# --------------------------------------------------------------------------- #
# Happy path — live accept
# --------------------------------------------------------------------------- #


async def test_accept_path_logs_episodic_cache_budget_and_anchor() -> None:
    bundle = make_bundle(critic_metrics=[_PASS], budget_live=True)
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert isinstance(result, RenderResult)
    assert result.status is ShotStatus.ACCEPTED
    assert result.rung == "full_video"
    assert result.video_seconds == 5.0
    assert result.attempts == 1

    # episodic logged as accepted, with the last frame for the embedding + anchor.
    assert len(bundle.episodic.logged) == 1
    logged = bundle.episodic.logged[0]
    assert logged["status"] is ShotStatus.ACCEPTED
    assert logged["last_frame_bytes"] is not None
    assert logged["output"]["last_frame_key"].startswith("lastframes/")

    # cache populated + budget committed for the accepted seconds.
    assert len(bundle.cache.puts) == 1
    assert bundle.budget.committed == [5.0]
    assert bundle.budget.released == 0

    # outputs written to object storage (clip + last-frame anchor + audio).
    assert any(k.startswith("clips/") for k in bundle.store.puts)
    assert any(k.startswith("lastframes/") for k in bundle.store.puts)
    assert any(k.startswith("audio/") for k in bundle.store.puts)

    # §9.7 transitions persisted through rendering → qa → accepted.
    assert ShotStatus.RENDERING in bundle.shots.statuses
    assert ShotStatus.QA in bundle.shots.statuses
    assert ShotStatus.ACCEPTED in bundle.shots.statuses
    assert bundle.designer.calls == 1
    assert bundle.generator.calls == 1
    assert bundle.critic.calls == 1


# --------------------------------------------------------------------------- #
# Cache hit — zero video-seconds
# --------------------------------------------------------------------------- #


async def test_cache_hit_returns_zero_video_seconds() -> None:
    cached = FakeCached(
        clip_key="clips/book_demo/shot_00042.mp4",
        last_frame_key="lastframes/book_demo/shot_00042.png",
        sync_segment={"shot_id": SHOT_ID, "video_start_s": 0.0, "video_end_s": 5.0},
        qa={"verdict": "pass"},
        video_seconds=5.0,
    )
    bundle = make_bundle(
        critic_metrics=[_PASS], shot_hash="sha1:known", cached=cached, budget_live=True
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.cache_hit is True
    assert result.status is ShotStatus.ACCEPTED
    assert result.rung == "cache_hit"
    assert result.video_seconds == 0.0
    assert result.clip_key == "clips/book_demo/shot_00042.mp4"
    # The fast path never designs, renders, commits budget, or logs episodic.
    assert bundle.designer.calls == 0
    assert bundle.generator.calls == 0
    assert bundle.budget.committed == []
    assert bundle.episodic.logged == []


# --------------------------------------------------------------------------- #
# Repair loop — retry cap then real degradation + defect
# --------------------------------------------------------------------------- #


async def test_repair_respects_retry_cap_then_degrades_and_logs_defect() -> None:
    # Identity drift every attempt → REGEN_TIGHTEN_REFS twice, then DEGRADE.
    bundle = make_bundle(
        critic_metrics=[_IDENTITY_FAIL],
        budget_live=True,
        seed_store={REF_KEY: png_bytes(640, 360)},  # a locked ref → Ken-Burns rung
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.status is ShotStatus.DEGRADED
    assert result.rung == degrade.DegradeRung.KEN_BURNS_KEYFRAME.value
    # retry_cap=2 → 3 attempts: initial design + 2 tighten-refs redesigns.
    assert bundle.designer.calls == 3
    assert bundle.generator.calls == 3
    assert bundle.critic.calls == 3
    assert len(bundle.budget.committed) == 3  # each live attempt spent its seconds
    assert result.video_seconds == pytest.approx(15.0)

    assert len(bundle.defects.logged) == 1
    defect = bundle.defects.logged[0]
    assert defect["kind"] == "degraded"
    assert defect["detail"]["reason"] == "retries_exhausted"
    assert ShotStatus.DEGRADED in bundle.shots.statuses

    # The degraded clip is a REAL, playable mp4 (Ken-Burns + narration).
    clip = bundle.store.store[keys.clip(BOOK_ID, SHOT_ID)]
    info = degrade.probe(clip)
    assert info.has_video and info.has_audio
    # Degraded shots are not cached as accepted footage.
    assert bundle.cache.puts == []


# --------------------------------------------------------------------------- #
# Budget-aware degradation — live gate off, no Wan render
# --------------------------------------------------------------------------- #


async def test_live_gate_off_goes_straight_to_degradation() -> None:
    bundle = make_bundle(
        critic_metrics=[_PASS],
        budget_live=False,
        seed_store={keys.keyframe(BOOK_ID, BEAT_ID): png_bytes(1280, 720)},
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.status is ShotStatus.DEGRADED
    assert result.rung == degrade.DegradeRung.KEN_BURNS_KEYFRAME.value
    assert result.video_seconds == 0.0
    assert bundle.generator.calls == 0  # never touched the Wan provider
    assert bundle.budget.reserved == []
    assert bundle.budget.committed == []
    assert bundle.defects.logged[0]["detail"]["reason"] == "live_video_disabled"

    clip = bundle.store.store[keys.clip(BOOK_ID, SHOT_ID)]
    assert degrade.verify_playable(clip) is True
    assert result.sync_segment is not None


async def test_provider_error_degrades_and_releases_budget() -> None:
    from app.providers.errors import LiveVideoDisabled

    bundle = make_bundle(
        critic_metrics=[_PASS],
        budget_live=True,
        generator=FakeGenerator(raises=LiveVideoDisabled("gated")),
        seed_store={keys.keyframe(BOOK_ID, BEAT_ID): png_bytes(1280, 720)},
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.status is ShotStatus.DEGRADED
    assert bundle.budget.reserved == [5.0]  # reserved before the doomed render
    assert bundle.budget.released == 1  # released when no clip was produced
    assert bundle.budget.committed == []


# --------------------------------------------------------------------------- #
# Conflict (§7.2): timeline contradiction → Continuity → Showrunner → apply
# --------------------------------------------------------------------------- #


async def test_timeline_conflict_honor_canon_regenerates_then_accepts() -> None:
    # Attempt 0: timeline contradiction → honor_canon → regen. Attempt 1: pass.
    bundle = make_bundle(
        critic_metrics=[_TIMELINE_FAIL, _PASS],
        budget_live=True,
        conflict=True,
        arbiter_supported=False,  # no textual support + no director → honor canon
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID, director_present=False)

    assert result.status is ShotStatus.ACCEPTED
    assert bundle.continuity is not None and bundle.continuity.calls == 1
    assert bundle.showrunner is not None and bundle.showrunner.calls == 1
    assert bundle.designer.calls == 2  # initial + honor-canon regen
    assert bundle.generator.calls == 2
    assert result.attempts == 2
    assert ShotStatus.CONFLICT in bundle.shots.statuses
    # The Showrunner's autonomous decision rides the result so the feed can show
    # it even though the reader was never asked (§7.2 transparency).
    assert result.decision is not None
    assert result.decision["chosen_option"] == "honor_canon"
    assert result.decision["evolved_canon"] is False


async def test_timeline_conflict_surface_to_user_returns_conflict() -> None:
    bundle = make_bundle(
        critic_metrics=[_TIMELINE_FAIL],
        budget_live=True,
        conflict=True,
        arbiter_supported=False,  # director present + no support → surface
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID, director_present=True)

    assert result.status is ShotStatus.CONFLICT
    assert result.rung == "conflict"
    assert result.conflict is not None
    assert result.conflict.conflict_id
    # Surfaced conflicts are not accepted/logged as footage.
    assert bundle.episodic.logged == []
    assert bundle.generator.calls == 1
    # A surfaced conflict carries the conflict object, not an auto-decision.
    assert result.decision is None


async def test_timeline_conflict_evolve_canon_writes_state_then_regenerates() -> None:
    bundle = make_bundle(
        critic_metrics=[_TIMELINE_FAIL, _PASS],
        budget_live=True,
        conflict=True,
        arbiter_supported=True,  # textual support → evolve canon
    )
    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID, director_present=False)

    assert result.status is ShotStatus.ACCEPTED
    assert bundle.evolver is not None
    assert len(bundle.evolver.asserts) == 1  # canon evolved via a real assert_state
    assert bundle.designer.calls == 2
    assert result.decision is not None
    assert result.decision["chosen_option"] == "evolve_canon"
    assert result.decision["evolved_canon"] is True


# --------------------------------------------------------------------------- #
# Style-drift gate (§9.5): the scene style centroid is now real (Fix 4)
# --------------------------------------------------------------------------- #


class StyleSpyCritic:
    """Records the ``scene_style_centroid`` the pipeline passes and fails the style
    gate whenever one is present, so the once-inert gate is exercised end-to-end.
    CCS / timeline / motion are forced to pass to isolate the *style* check."""

    def __init__(self) -> None:
        self.calls = 0
        self.received_centroids: list[list[float] | None] = []

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
        self.calls += 1
        self.received_centroids.append(scene_style_centroid)
        style_drift = 0.0 if scene_style_centroid is None else 0.5  # divergent clip
        verdict, action, score = decide_qa(
            0.95, style_drift, True, 0.05, retries_exhausted=retries_exhausted
        )
        return QARecord(
            shot_id=shot_id,
            ccs=0.95,
            style_drift=style_drift,
            timeline_ok=True,
            contradicting_state_id=None,
            motion_artifact=0.05,
            score=score,
            verdict=verdict,
            reason="style-spy",
            repair_action=action,
        )


async def test_pipeline_computes_real_style_centroid_and_drives_the_gate() -> None:
    """Fix 4: the pipeline embeds the Style node's locked reference into a real
    scene style centroid and passes it to the Critic on every scoring call (it
    used to pass ``None``, making the §9.5 style gate inert). With a divergent
    clip the now-live gate fires, the repair loop exhausts, and the shot degrades.
    """
    shot = FakeShot(
        id=SHOT_ID,
        book_id=BOOK_ID,
        beat_id=BEAT_ID,
        scene_id=SCENE_ID,
        source_span=dict(_SPAN),
        duration_s=5.0,
    )
    beat = FakeBeat(
        id=BEAT_ID,
        book_id=BOOK_ID,
        scene_id=SCENE_ID,
        beat_index=7,
        summary="X stands at the window.",
        entities=["char_x"],
        described_visuals="a quiet figure at a frosted window",
        mood="still",
        source_span=dict(_SPAN),
    )
    page = FakePage(word_boxes=word_boxes(), image_key=None, text="She stood still")
    embedder = FakeEmbedder()
    spy = StyleSpyCritic()
    store = FakeObjectStore({STYLE_REF_KEY: png_bytes(800, 600), REF_KEY: png_bytes(640, 360)})

    pipeline = RenderPipeline(
        canon=FakeCanon(make_slice(with_style=True)),
        episodic=FakeEpisodic(),
        cache=FakeCache(),
        budget=FakeBudget(live=True),
        object_store=store,
        shots=FakeShotRepo(shot),
        beats=FakeBeatRepoFor(beat),
        pages=FakePageRepoFor(page),
        defects=FakeDefectRepo(),
        designer=FakeDesigner(),
        generator=FakeGenerator(),
        critic=spy,
        narrator=FakeNarrator(),
        embedder=embedder,
        settings=Settings(dashscope_api_key="test"),
    )
    result = await pipeline.render_shot(BOOK_ID, SHOT_ID)

    # The centroid was computed from the Style node's locked reference (not None).
    assert spy.received_centroids
    assert all(c is not None for c in spy.received_centroids)
    expected = (await embedder.embed_images([png_bytes(800, 600)]))[0]
    assert spy.received_centroids[0] == expected
    # The live gate now has teeth: a divergent clip degrades (was silently accepted).
    assert result.status is ShotStatus.DEGRADED


# --------------------------------------------------------------------------- #
# Live-path degradation crash-proofing (§4.11): Critic / TTS provider failures
# --------------------------------------------------------------------------- #


async def test_critic_provider_error_degrades_instead_of_dlq() -> None:
    """Fix 5: a ProviderError from the Critic yields a degraded, playable result
    (the rendered seconds are committed) rather than bubbling up to a worker DLQ."""
    from app.providers.errors import ProviderError

    bundle = make_bundle(
        critic_metrics=[_PASS],
        budget_live=True,
        seed_store={REF_KEY: png_bytes(640, 360)},  # a locked ref → Ken-Burns rung
    )
    bundle.critic.raises = ProviderError("VL critic is down")

    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.status is ShotStatus.DEGRADED
    assert bundle.generator.calls == 1  # rendered once, then degraded (no retry storm)
    assert bundle.budget.committed == [5.0]  # the rendered seconds were charged once
    clip = bundle.store.store[keys.clip(BOOK_ID, SHOT_ID)]
    assert degrade.probe(clip).has_video is True
    assert bundle.defects.logged[0]["detail"]["reason"].startswith("critic_")


async def test_tts_provider_error_in_degrade_yields_silent_playable_clip() -> None:
    """Fix 5: a ProviderError from the TTS inside _degrade still yields a degraded,
    playable (silent) clip — never a crash/DLQ."""
    from app.providers.errors import ProviderError

    bundle = make_bundle(
        critic_metrics=[_PASS],
        budget_live=False,  # straight to the degradation ladder
        seed_store={keys.keyframe(BOOK_ID, BEAT_ID): png_bytes(1280, 720)},
    )
    bundle.narrator.raises = ProviderError("TTS is down")

    result = await bundle.pipeline.render_shot(BOOK_ID, SHOT_ID)

    assert result.status is ShotStatus.DEGRADED
    clip = bundle.store.store[keys.clip(BOOK_ID, SHOT_ID)]
    info = degrade.probe(clip)
    assert info.has_video is True  # playable video even with no narration audio
    assert degrade.verify_playable(clip) is True


# --------------------------------------------------------------------------- #
# Guarded live smoke — NOT run (would spend real Wan video-seconds)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("KINORA_LIVE_VIDEO", "").lower() not in {"1", "true"},
    reason="live Wan render is gated off; not run in CI",
)
async def test_live_render_smoke_when_gate_on() -> None:  # pragma: no cover - never run here
    from app.agents.cinematographer import Cinematographer
    from app.agents.critic import Critic
    from app.agents.generator import Generator
    from app.providers import create_providers

    live_settings = Settings(
        dashscope_api_key=os.environ["DASHSCOPE_API_KEY"], kinora_live_video=True
    )
    providers = create_providers(live_settings)
    try:
        bundle = make_bundle(critic_metrics=[_PASS], budget_live=True)
        beat = FakeBeat(
            BEAT_ID, BOOK_ID, SCENE_ID, 7, "X at window", ["char_x"], None, None, dict(_SPAN)
        )
        # Swap in the REAL Cinematographer/Generator/Critic for a true render.
        live = RenderPipeline(
            canon=FakeCanon(make_slice()),
            episodic=bundle.episodic,
            cache=bundle.cache,
            budget=bundle.budget,
            object_store=bundle.store,
            shots=bundle.shots,
            beats=FakeBeatRepoFor(beat),
            pages=FakePageRepoFor(FakePage(word_boxes(), None, "She stood still")),
            defects=bundle.defects,
            designer=Cinematographer(providers),
            generator=Generator(providers),
            critic=Critic(providers),
            narrator=providers.tts,
            settings=live_settings,
        )
        result = await live.render_shot(BOOK_ID, SHOT_ID)
        assert result.status in {ShotStatus.ACCEPTED, ShotStatus.DEGRADED}
    finally:
        await providers.aclose()
