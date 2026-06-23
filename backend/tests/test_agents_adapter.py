"""Unit tests for the Adapter: beat parsing, the "never invent a character"
guardrail, deterministic beat→shot splitting, and the ShotPlanner protocol. No
network."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.adapter import (
    MAX_SHOT_SECONDS,
    MIN_SHOT_SECONDS,
    SHOT_SECONDS,
    WORDS_PER_SHOT,
    Adapter,
)
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
    # A full WORDS_PER_SHOT shot lands on the §4.2 anchor (~SHOT_SECONDS).
    assert all(s.est_duration_s == SHOT_SECONDS for s in shots)
    # est_cost.video_seconds (the scarce budget unit) tracks the shot's duration.
    assert all(s.est_cost.video_seconds == s.est_duration_s for s in shots)
    assert all(s.est_cost.tokens > 0 for s in shots)


def test_plan_shots_sets_variable_per_shot_durations_within_bounds(
    providers: Providers,  # noqa: F811
) -> None:
    """The planner (not a fixed constant) decides each shot's target_duration_s
    from that shot's own narration length: a sparse beat earns a short clip, a
    denser one a longer clip, and every shot stays inside
    [MIN_SHOT_SECONDS, MAX_SHOT_SECONDS]. Fully deterministic — no LLM."""
    beats = [
        # 12 words → 1s of screen-time, floored at the minimum.
        Beat(beat_id="beat_0001", summary="sparse", source_span=SourceSpan(word_range=(0, 12))),
        # 48 words → between the floor and the anchor.
        Beat(beat_id="beat_0002", summary="brisk", source_span=SourceSpan(word_range=(0, 48))),
        # 60 words → the §4.2 anchor (~SHOT_SECONDS).
        Beat(beat_id="beat_0003", summary="full", source_span=SourceSpan(word_range=(0, 60))),
    ]
    shots = Adapter(providers).plan_shots(beats)
    by_beat = {s.beat_id: s for s in shots}
    durations = [s.est_duration_s for s in shots]

    # Per-shot, not one fixed constant: the plan contains differing durations.
    assert len(set(durations)) > 1
    # Every shot is inside the agreed bounds.
    assert all(MIN_SHOT_SECONDS <= d <= MAX_SHOT_SECONDS for d in durations)
    # Sparse 12-word beat → below the anchor but floored at the minimum.
    assert by_beat["beat_0001"].est_duration_s == MIN_SHOT_SECONDS
    # 48-word beat → strictly between the floor and the anchor.
    assert MIN_SHOT_SECONDS < by_beat["beat_0002"].est_duration_s < SHOT_SECONDS
    # Full 60-word beat → the §4.2 anchor.
    assert by_beat["beat_0003"].est_duration_s == SHOT_SECONDS
    # est_cost.video_seconds (the scarce budget unit) tracks each shot's duration.
    assert all(s.est_cost.video_seconds == s.est_duration_s for s in shots)


def test_duration_for_words_clamps_to_bounds(providers: Providers) -> None:  # noqa: F811
    """The deterministic word→seconds map saturates at both ends of [3, 8]s."""
    fn = Adapter._duration_for_words
    assert fn(0) == MIN_SHOT_SECONDS  # empty/unknown → floor, never zero
    assert fn(1) == MIN_SHOT_SECONDS  # 0.08s → floor
    assert fn(10_000) == MAX_SHOT_SECONDS  # absurdly dense → ceiling
    assert MIN_SHOT_SECONDS <= fn(60) <= MAX_SHOT_SECONDS


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
    # The planner's per-shot duration flows through to the render spec (two full
    # 60-word shots → the §4.2 anchor), and stays inside the agreed bounds.
    assert all(s.target_duration_s == SHOT_SECONDS for s in specs)
    assert all(MIN_SHOT_SECONDS <= s.target_duration_s <= MAX_SHOT_SECONDS for s in specs)
