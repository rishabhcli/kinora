"""The render engine facade — resume, poison, telemetry, scene DAG (kinora.md §9.7).

The hardening control flow is exercised with a light fake renderer (no
ffmpeg/DB/network), plus one end-to-end pass over the REAL RenderPipeline with the
shared render doubles to prove the engine is a transparent drop-in. KINORA_LIVE_VIDEO
stays off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.agents.contracts import DirectorNote
from app.core.config import Settings
from app.db.models.enums import ShotStatus
from app.render.checkpoint import InMemoryCheckpointStore
from app.render.engine import RenderEngine
from app.render.ladder import Rung
from app.render.pipeline import RenderResult, UnknownShotError
from app.render.poison import InMemoryPoisonStore, PoisonTracker
from app.render.states import RenderState
from app.render.telemetry import EventKind, recording_bus


@dataclass
class FakeRenderer:
    """A scriptable :class:`ShotRenderer`: per-shot results or a raised crash."""

    results: dict[str, RenderResult] = field(default_factory=dict)
    raises: dict[str, Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def render_shot(
        self,
        book_id: str,
        shot_id: str,
        *,
        session_id: str | None = None,
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult:
        self.calls.append(shot_id)
        if shot_id in self.raises:
            raise self.raises[shot_id]
        return self.results.get(
            shot_id,
            RenderResult(
                shot_id=shot_id, status=ShotStatus.ACCEPTED, rung="full_video", video_seconds=5.0
            ),
        )


def _settings() -> Settings:
    return Settings(dashscope_api_key="test", render_poison_threshold=2)


def _engine(renderer: FakeRenderer, **over: object) -> RenderEngine:
    bus, _ = recording_bus()
    return RenderEngine(
        renderer,
        checkpoints=InMemoryCheckpointStore(),
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=2, bus=bus),
        bus=bus,
        settings=_settings(),
        **over,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Transparent pass-through
# --------------------------------------------------------------------------- #


async def test_accept_passes_through_and_clears_checkpoint() -> None:
    renderer = FakeRenderer()
    engine = _engine(renderer)
    result = await engine.render_shot("book", "shot_1")
    assert result.status is ShotStatus.ACCEPTED
    assert renderer.calls == ["shot_1"]
    # A completed shot leaves no lingering checkpoint (cleared after terminal save).
    assert await engine._checkpoints.load("shot_1") is None


# --------------------------------------------------------------------------- #
# Resume / idempotent re-claim
# --------------------------------------------------------------------------- #


async def test_terminal_checkpoint_short_circuits_re_render() -> None:
    renderer = FakeRenderer()
    store = InMemoryCheckpointStore()
    bus, recorder = recording_bus()
    engine = RenderEngine(
        renderer, checkpoints=store, bus=bus, settings=_settings(),
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=2, bus=bus),
    )
    # Pre-seed a terminal checkpoint (as if a prior worker finished but the clear
    # raced a re-claim).
    from app.render.checkpoint import ShotCheckpoint

    await store.save(
        ShotCheckpoint(shot_id="shot_1", book_id="book", state=RenderState.ACCEPTED)
    )
    result = await engine.render_shot("book", "shot_1")
    assert result.cache_hit is True
    assert renderer.calls == []  # the pipeline was NOT re-run (no double-spend)
    assert recorder.count(EventKind.RESUMED) == 1


async def test_mid_flight_checkpoint_emits_resume_then_runs() -> None:
    renderer = FakeRenderer()
    store = InMemoryCheckpointStore()
    bus, recorder = recording_bus()
    engine = RenderEngine(
        renderer, checkpoints=store, bus=bus, settings=_settings(),
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=2, bus=bus),
    )
    from app.render.checkpoint import ShotCheckpoint

    await store.save(
        ShotCheckpoint(shot_id="shot_1", book_id="book", state=RenderState.RENDERING, attempts=1)
    )
    result = await engine.render_shot("book", "shot_1")
    assert result.status is ShotStatus.ACCEPTED
    assert renderer.calls == ["shot_1"]  # resumed → still ran the pipeline
    assert recorder.count(EventKind.RESUMED) == 1


async def test_checkpoint_disabled_never_probes() -> None:
    renderer = FakeRenderer()
    store = InMemoryCheckpointStore()
    engine = RenderEngine(
        renderer,
        checkpoints=store,
        settings=Settings(dashscope_api_key="test", render_checkpoint_enabled=False),
    )
    await engine.render_shot("book", "shot_1")
    # No checkpoint written when the feature is off.
    assert await store.load("shot_1") is None


# --------------------------------------------------------------------------- #
# Poison / dead-shot
# --------------------------------------------------------------------------- #


async def test_repeated_crashes_quarantine_then_ship_bottom_rung() -> None:
    renderer = FakeRenderer(raises={"shot_x": UnknownShotError("broken")})
    engine = _engine(renderer)
    # Permanent failure (UnknownShotError) is double-weight → poisons in one hit
    # at threshold 2.
    with pytest.raises(UnknownShotError):
        await engine.render_shot("book", "shot_x")
    assert engine.poison.is_poisoned("shot_x")
    # The next claim is shipped at the bottom rung WITHOUT touching the pipeline.
    result = await engine.render_shot("book", "shot_x")
    assert result.status is ShotStatus.DEGRADED
    assert result.rung == Rung.AUDIO_TEXT_ONLY.value
    assert renderer.calls == ["shot_x"]  # only the first (crashing) attempt ran


async def test_transient_crash_does_not_quarantine_immediately() -> None:
    renderer = FakeRenderer(raises={"shot_x": RuntimeError("blip")})
    engine = _engine(renderer)  # threshold 2; transient weight 1
    with pytest.raises(RuntimeError):
        await engine.render_shot("book", "shot_x")
    assert not engine.poison.is_poisoned("shot_x")
    assert engine.poison.failures("shot_x") == 1


async def test_poison_uses_injected_degrader() -> None:
    async def degrader(book_id: str, shot_id: str) -> RenderResult:
        return RenderResult(
            shot_id=shot_id, status=ShotStatus.DEGRADED, rung="ken_burns_illustration"
        )

    renderer = FakeRenderer(raises={"shot_x": UnknownShotError("broken")})
    bus, _ = recording_bus()
    engine = RenderEngine(
        renderer,
        checkpoints=InMemoryCheckpointStore(),
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=2, bus=bus),
        bus=bus,
        settings=_settings(),
        poison_degrader=degrader,
    )
    with pytest.raises(UnknownShotError):
        await engine.render_shot("book", "shot_x")
    result = await engine.render_shot("book", "shot_x")
    assert result.rung == "ken_burns_illustration"  # the injected degrader ran


# --------------------------------------------------------------------------- #
# Scene DAG
# --------------------------------------------------------------------------- #


async def test_render_scene_orders_continuation_after_predecessor() -> None:
    renderer = FakeRenderer()
    engine = _engine(renderer)
    results = await engine.render_scene(
        "book",
        [
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
            {"shot_id": "x", "render_mode": "text_to_video"},
        ],
    )
    assert set(results) == {"a", "b", "x"}
    assert all(r.status is ShotStatus.ACCEPTED for r in results.values())
    # 'b' continuation rendered strictly after 'a'.
    assert renderer.calls.index("a") < renderer.calls.index("b")


async def test_render_scene_ships_continuation_blocked_on_degrade() -> None:
    renderer = FakeRenderer(
        results={
            "a": RenderResult(
                shot_id="a", status=ShotStatus.DEGRADED, rung="ken_burns_keyframe"
            )
        }
    )
    engine = _engine(renderer)
    results = await engine.render_scene(
        "book",
        [
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
        ],
    )
    # 'b' couldn't continuation-render off a degrade → shipped the bottom rung.
    assert results["b"].status is ShotStatus.DEGRADED
    assert results["b"].rung == Rung.AUDIO_TEXT_ONLY.value
    assert "b" not in renderer.calls  # never handed to the pipeline


# --------------------------------------------------------------------------- #
# End-to-end over the REAL RenderPipeline (drop-in transparency)
# --------------------------------------------------------------------------- #


async def test_engine_is_a_transparent_drop_in_over_the_real_pipeline() -> None:
    from app.render import degrade

    if not degrade.ffmpeg_available():
        pytest.skip("no ffmpeg binary available")

    from tests.test_render_pipeline import make_bundle

    passing = {"ccs": 0.95, "style": 0.02, "timeline_ok": True, "motion": 0.05}
    bundle = make_bundle(critic_metrics=[passing], budget_live=True)
    engine = RenderEngine(
        bundle.pipeline,
        checkpoints=InMemoryCheckpointStore(),
        poison=PoisonTracker(store=InMemoryPoisonStore(), threshold=2),
        settings=_settings(),
    )
    from tests.test_render_support import BOOK_ID, SHOT_ID

    result = await engine.render_shot(BOOK_ID, SHOT_ID)
    # Identical outcome to calling the pipeline directly (accept, full video).
    assert result.status is ShotStatus.ACCEPTED
    assert result.rung == "full_video"
    assert result.video_seconds == 5.0
    # The engine recorded the success (poison history clean) + cleared the checkpoint.
    assert not engine.poison.is_poisoned(SHOT_ID)


async def test_build_render_engine_constructs_without_network() -> None:
    """The production factory wires over the real pipeline lazily (no DashScope/DB).

    Mirrors ``create_app()`` / ``build_render_pipeline``: construction is cheap and
    network-free with ``DASHSCOPE_API_KEY=test``, so a worker can build the engine
    at startup. We don't *render* here (that needs infra) — only prove the wiring.
    """
    from app.providers import create_providers
    from app.render.engine import RenderEngine, build_render_engine
    from app.storage.object_store import ObjectStore

    settings = Settings(dashscope_api_key="test")
    providers = create_providers(settings)
    try:
        engine = build_render_engine(
            session=None,
            providers=providers,
            object_store=ObjectStore.from_settings(settings),
            settings=settings,
        )
        assert isinstance(engine, RenderEngine)
        # Metrics + log sinks were attached for the §12.5 surface.
        from app.render.telemetry import LogSink, MetricsSink

        sinks = engine.bus._sinks
        assert any(isinstance(s, MetricsSink) for s in sinks)
        assert any(isinstance(s, LogSink) for s in sinks)
    finally:
        await providers.aclose()
