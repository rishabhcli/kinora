"""Unit tests for the reader state machine (app.reliability.reader_model)."""

from __future__ import annotations

from collections import Counter

import pytest

from app.reliability.reader_model import (
    SETTLE_INTERVAL_S,
    ActionKind,
    ReaderModel,
    ReaderPersona,
    ReaderState,
    is_skim_velocity,
)
from app.scheduler.zones import VELOCITY_CLAMP_HIGH, VELOCITY_CLAMP_LOW


def _drain(model: ReaderModel, duration_s: float) -> list:
    return list(model.steps(duration_s=duration_s))


def test_deterministic_given_seed() -> None:
    a = _drain(ReaderModel(seed=42), 20.0)
    b = _drain(ReaderModel(seed=42), 20.0)
    assert len(a) == len(b)
    assert [(x.kind, x.focus_word, round(x.velocity_wps, 6)) for x in a] == [
        (y.kind, y.focus_word, round(y.velocity_wps, 6)) for y in b
    ]


def test_different_seeds_diverge() -> None:
    a = _drain(ReaderModel(seed=1), 30.0)
    b = _drain(ReaderModel(seed=2), 30.0)
    assert [x.focus_word for x in a] != [x.focus_word for x in b]


def test_clock_advances_by_settle_interval_per_step() -> None:
    model = ReaderModel(seed=3)
    actions = _drain(model, 2.0)
    # The loop emits until the model clock crosses the duration, so ~10-11 steps
    # of one settle interval each (float drift means the last step lands just over).
    assert 10 <= len(actions) <= 11
    assert actions[0].t_s == SETTLE_INTERVAL_S
    assert actions[-1].t_s >= 2.0
    # Evenly spaced by one settle interval, strictly monotonic.
    for prev, cur in zip(actions, actions[1:], strict=False):
        assert cur.t_s > prev.t_s
        assert cur.t_s - prev.t_s == pytest.approx(SETTLE_INTERVAL_S)


def test_velocity_always_clamped_for_intent() -> None:
    # An aggressive persona that skims a lot; emitted velocity_wps stays in band.
    persona = ReaderPersona(name="skimmer", p_skim=0.5, skim_velocity_mult=10.0)
    model = ReaderModel(persona=persona, seed=9)
    for action in model.steps(duration_s=40.0):
        if action.kind is ActionKind.INTENT:
            assert VELOCITY_CLAMP_LOW <= action.velocity_wps <= VELOCITY_CLAMP_HIGH


def test_skim_raw_velocity_exceeds_clamp_ceiling() -> None:
    persona = ReaderPersona(p_skim=1.0, skim_velocity_mult=4.0, velocity_jitter=0.0)
    model = ReaderModel(persona=persona, seed=11)
    skims = [a for a in model.steps(duration_s=20.0) if a.state is ReaderState.SKIMMING]
    assert skims, "expected skim actions with p_skim=1.0"
    for action in skims:
        assert is_skim_velocity(action.raw_velocity_wps)
        # …but the clamped velocity used for ETA stays at the ceiling.
        assert action.velocity_wps == VELOCITY_CLAMP_HIGH


def test_seek_emits_seek_action_with_target() -> None:
    persona = ReaderPersona(p_seek=1.0)  # always seek
    model = ReaderModel(persona=persona, seed=5, start_word=10_000)
    actions = _drain(model, 4.0)
    seeks = [a for a in actions if a.kind is ActionKind.SEEK]
    assert seeks, "expected seek actions with p_seek=1.0"
    for action in seeks:
        assert action.seek_word is not None
        assert action.focus_word == action.seek_word
        assert 0 <= action.seek_word < persona.book_words


def test_idle_emits_no_request_actions() -> None:
    persona = ReaderPersona(p_pause=1.0, mean_pause_s=100.0)  # pause immediately, stay paused
    model = ReaderModel(persona=persona, seed=7)
    actions = _drain(model, 10.0)
    kinds = Counter(a.kind for a in actions)
    # After the first pause, the reader stays idle: mostly IDLE actions, no traffic.
    assert kinds[ActionKind.IDLE] > 0
    idle_actions = [a for a in actions if a.kind is ActionKind.IDLE]
    assert all(a.velocity_wps == 0.0 for a in idle_actions)


def test_forward_progress_under_steady_reading() -> None:
    # No skim/seek/pause: pure forward reading advances the focus word.
    persona = ReaderPersona(p_skim=0.0, p_seek=0.0, p_pause=0.0)
    model = ReaderModel(persona=persona, seed=13)
    actions = _drain(model, 30.0)
    assert all(a.kind is ActionKind.INTENT for a in actions)
    assert actions[-1].focus_word > actions[0].focus_word


def test_book_end_wraps_for_reread() -> None:
    persona = ReaderPersona(
        p_skim=0.0, p_seek=0.0, p_pause=0.0, book_words=500, base_velocity_wps=12.0
    )
    model = ReaderModel(persona=persona, seed=2)
    words = [a.focus_word for a in model.steps(duration_s=60.0)]
    # The reader runs off the end and loops back at least once.
    assert max(words) < persona.book_words
    # Non-monotonic somewhere (the wrap) but never out of range.
    assert all(0 <= w < persona.book_words for w in words)


def test_state_property_tracks_machine() -> None:
    persona = ReaderPersona(p_skim=0.0, p_seek=0.0, p_pause=0.0)
    model = ReaderModel(persona=persona, seed=1)
    _drain(model, 5.0)
    assert model.state is ReaderState.READING
    assert model.focus_word > 0
    assert model.clock_s >= 5.0
