"""Fast, ffmpeg-free unit coverage of the harness building blocks.

These exercise the deterministic pieces — the virtual clock, the synthetic book
fixture, the golden-trace recorder, and the scheduler zone math the harness
borrows — without ever touching ffmpeg, so they run in milliseconds and pin the
harness's own contracts.
"""

from __future__ import annotations

import pytest

from app.e2e.clock import VirtualClock
from app.e2e.synthetic_book import CHAR_KEY, make_synthetic_book
from app.e2e.trace import GoldenTrace, TraceRecorder
from app.e2e.world import FakeWorld, WorldConfig
from app.scheduler import zones

# --------------------------------------------------------------------------- #
# Virtual clock
# --------------------------------------------------------------------------- #


def test_virtual_clock_is_monotonic_and_deterministic() -> None:
    clock = VirtualClock()
    assert clock.now() == 0.0
    assert clock.now_ms() == 0
    assert clock.advance(1.5) == 1.5
    assert clock.now_ms() == 1500
    assert clock.advance_ms(500) == 2000
    assert clock.now() == 2.0


def test_virtual_clock_rejects_backward_time() -> None:
    clock = VirtualClock()
    clock.advance(3.0)
    with pytest.raises(ValueError):
        clock.advance(-1.0)
    with pytest.raises(ValueError):
        VirtualClock(start_s=-1.0)


# --------------------------------------------------------------------------- #
# Synthetic book fixture
# --------------------------------------------------------------------------- #


def test_synthetic_book_is_structurally_complete() -> None:
    book = make_synthetic_book()
    assert book.pages and book.beats and book.shots
    # Every shot binds to a real beat + a real page.
    for shot in book.shots:
        assert book.beat(shot.beat_id) is not None
        page_no = book.beat_pages[shot.beat_id]
        assert book.page(page_no) is not None
    # The canon slice carries the voiced character with a locked reference.
    char = book.canon_slice.characters[0]
    assert char.entity_key == CHAR_KEY
    assert char.voice and char.reference_images[0].locked


def test_synthetic_book_spans_are_contiguous_and_non_overlapping() -> None:
    book = make_synthetic_book()
    # Beats are emitted in reading order; spans never overlap and stay on-page.
    last_end = -1
    for beat in book.beats:
        start, end = beat.source_span["word_range"]
        assert start <= end
        assert start > last_end, "beat spans must not overlap"
        last_end = end
        # Every word in the span lives on the beat's page.
        page = book.page(book.beat_pages[beat.beat_id])
        assert page is not None
        indices = {b["word_index"] for b in page.word_boxes}
        assert start in indices and end in indices


def test_synthetic_book_is_deterministic() -> None:
    a = make_synthetic_book()
    b = make_synthetic_book()
    assert [s.shot_id for s in a.shots] == [s.shot_id for s in b.shots]
    assert [bt.source_span for bt in a.beats] == [bt.source_span for bt in b.beats]


# --------------------------------------------------------------------------- #
# Golden trace
# --------------------------------------------------------------------------- #


def test_trace_recorder_is_ordered_and_canonical() -> None:
    rec = TraceRecorder()
    rec.record("a", x=1.123456, nested={"b": 2.0, "a": 1.0})
    rec.record("b", y=3)
    trace = rec.trace()
    assert isinstance(trace, GoldenTrace)
    assert trace.kinds() == ["a", "b"]
    # Floats are rounded and dict keys sorted in the canonical form.
    first = trace.as_list()[0]
    assert first == {"seq": 0, "kind": "a", "data": {"nested": {"a": 1.0, "b": 2.0}, "x": 1.123}}
    # Canonical JSON is byte-stable across recorders with the same events.
    rec2 = TraceRecorder()
    rec2.record("a", x=1.123456, nested={"a": 1.0, "b": 2.0})
    rec2.record("b", y=3)
    assert rec2.trace().canonical() == trace.canonical()


def test_trace_of_kind_filters() -> None:
    rec = TraceRecorder()
    rec.record("page_turn", page=1)
    rec.record("shot_rendered", shot_id="s0")
    rec.record("shot_rendered", shot_id="s1")
    trace = rec.trace()
    assert len(trace.of_kind("shot_rendered")) == 2
    assert len(trace.of_kind("page_turn")) == 1


# --------------------------------------------------------------------------- #
# Scheduler zone math wired into the world (no ffmpeg)
# --------------------------------------------------------------------------- #


def test_world_zone_classification_tracks_the_reader() -> None:
    world = FakeWorld(config=WorldConfig(commit_horizon_s=8.0, spec_horizon_s=30.0))
    first_shot = world.book.shots[0].shot_id
    # Reader at word 0: the opening shot is within the commit horizon.
    world.reader.focus_word = 0
    eta, zone = world.zone_for_shot(first_shot)
    assert zone is zones.Zone.COMMITTED
    assert first_shot in world.committed_shots()
    # A distant shot is cold.
    far = world.book.shots[-1].shot_id
    _eta_far, zone_far = world.zone_for_shot(far)
    assert zone_far in (zones.Zone.SPECULATIVE, zones.Zone.COLD)


def test_world_skim_is_unstable() -> None:
    world = FakeWorld()
    world.reader.raw_velocity_wps = zones.VELOCITY_CLAMP_HIGH * 2
    assert world.stable() is False
    world.reader.raw_velocity_wps = world.config.velocity_wps
    world.reader.oscillating = False
    assert world.stable() is True
