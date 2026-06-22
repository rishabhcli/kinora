"""Unit tests for the Adapter: beat parsing, the "never invent a character"
guardrail, deterministic beat→shot splitting, and the ShotPlanner protocol. No
network."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.adapter import WORDS_PER_SHOT, Adapter
from app.agents.contracts import Beat, SourceSpan
from app.memory.interfaces import ShotSpec as RenderShotSpec
from app.providers import Providers
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)

_PAGE_REPLY = {
    "beats": [
        {
            "summary": "The knight meets a dragon at the gate.",
            "entities": ["Hero", "Dragon"],
            "unresolved_entities": [],
            "described_visuals": "a knight faces a dragon",
            "mood": "tense",
            "source_span": {"page": 3, "para": 1, "word_range": [10, 40]},
        }
    ]
}


async def test_analyze_page_refuses_invented_characters(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer(_PAGE_REPLY)  # type: ignore[method-assign]
    adapter = Adapter(providers)

    beats = await adapter.analyze_page(
        "The knight meets a dragon.", page=3, scene_id="scene_001", known_entities={"Hero"}
    )

    assert len(beats) == 1
    beat = beats[0]
    assert beat.beat_id == "beat_0000"  # the Adapter assigns canonical ids
    assert beat.scene_id == "scene_001"
    # "Dragon" is not in the known canon → refused (moved to unresolved).
    assert beat.entities == ["Hero"]
    assert "Dragon" in beat.unresolved_entities
    assert beat.source_span.page == 3


async def test_analyze_page_trusts_model_when_no_canon(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer(_PAGE_REPLY)  # type: ignore[method-assign]
    beats = await Adapter(providers).analyze_page("...", page=3)
    assert beats[0].entities == ["Hero", "Dragon"]  # no known set => no filtering


def test_plan_shots_splits_long_beat_into_five_second_shots(providers: Providers) -> None:  # noqa: F811
    beat = Beat(
        beat_id="beat_0001",
        scene_id="scene_001",
        summary="a long action beat",
        source_span=SourceSpan(page=12, word_range=(0, 3 * WORDS_PER_SHOT)),
    )
    shots = Adapter(providers).plan_shots([beat])

    assert len(shots) == 3
    assert [s.shot_id for s in shots] == [
        "beat_0001_shot_00",
        "beat_0001_shot_01",
        "beat_0001_shot_02",
    ]
    # Contiguous, monotonic word ranges that tile the beat span exactly.
    assert shots[0].source_span.word_range == (0, WORDS_PER_SHOT)
    assert shots[-1].source_span.word_range == (2 * WORDS_PER_SHOT, 3 * WORDS_PER_SHOT)
    assert all(s.est_duration_s == 5.0 for s in shots)
    assert all(s.est_cost.video_seconds == 5.0 and s.est_cost.tokens > 0 for s in shots)


def test_plan_shots_one_shot_when_span_unknown(providers: Providers) -> None:  # noqa: F811
    beat = Beat(beat_id="beat_0009", summary="short beat")  # default span (0,0)
    shots = Adapter(providers).plan_shots([beat])
    assert len(shots) == 1


async def test_plan_scene_reads_beats_and_returns_render_specs(providers: Providers) -> None:  # noqa: F811
    rows = [
        SimpleNamespace(
            id="beat_0001",
            book_id="book_x",
            scene_id="scene_001",
            beat_index=1,
            summary="s",
            entities=["Hero"],
            described_visuals="v",
            mood="m",
            source_span={"page": 5, "para": 1, "word_range": [0, 2 * WORDS_PER_SHOT]},
        )
    ]

    async def loader(scene_id: str) -> list[SimpleNamespace]:
        assert scene_id == "scene_001"
        return rows

    adapter = Adapter(providers, beats_loader=loader)
    specs = await adapter.plan_scene("scene_001")

    assert len(specs) == 2  # 120 words → two ~5s shots
    assert all(isinstance(s, RenderShotSpec) for s in specs)
    assert {s.book_id for s in specs} == {"book_x"}
    assert {s.beat_id for s in specs} == {"beat_0001"}
    assert [s.shot_id for s in specs] == ["beat_0001_shot_00", "beat_0001_shot_01"]
    assert all(s.target_duration_s == 5.0 for s in specs)
