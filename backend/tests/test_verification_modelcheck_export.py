"""Tests for the DOT exporter and the trace-replay checker."""

from __future__ import annotations

import pytest

from app.verification.modelcheck import (
    Action,
    ModelChecker,
    Spec,
    invariant,
    replay,
    to_dot,
)
from app.verification.specs.render_queue import (
    RenderJobState,
    build_render_queue_spec,
)


def _counter_spec(n: int, *, broken: bool = False) -> Spec[int]:
    inc = Action[int]("inc", lambda s: s < n, lambda s: (s + 1,))
    dec = Action[int]("dec", lambda s: (s > -1 if broken else s > 0), lambda s: (s - 1,))
    return Spec(
        name="counter",
        initial=(0,),
        actions=(inc, dec),
        invariants=(invariant("non_negative", lambda s: s >= 0),),
    )


def test_to_dot_renders_nodes_and_edges() -> None:
    checker = ModelChecker[int]()
    graph, _ = checker.explore(_counter_spec(3))
    dot = to_dot(graph, label=lambda s: f"v={s}")
    assert dot.startswith("digraph model {")
    assert dot.rstrip().endswith("}")
    assert "v=0" in dot and "v=3" in dot
    assert "-> " in dot  # has edges
    assert '[label="inc"' in dot or 'label="inc"' in dot


def test_to_dot_highlights_counterexample() -> None:
    checker = ModelChecker[int](stop_on_violation=False)
    report = checker.check(_counter_spec(3, broken=True))
    graph, _ = checker.explore(_counter_spec(3, broken=True))
    res = report.result_for("non_negative")
    assert res is not None and res.counterexample is not None
    dot = to_dot(graph, highlight=res.counterexample)
    assert 'color="red"' in dot  # the offending path is highlighted


def test_to_dot_truncates_large_graphs() -> None:
    inc = Action[int]("inc", lambda s: True, lambda s: (s + 1,))
    spec = Spec(name="big", initial=(0,), actions=(inc,))
    checker = ModelChecker[int](max_states=300)
    graph, _ = checker.explore(spec)
    dot = to_dot(graph, max_nodes=20)
    assert "more states" in dot


def test_replay_validates_a_real_trace() -> None:
    spec = _counter_spec(3)
    reached = list(replay(spec, ["inc", "inc", "dec"]))
    assert [s for _a, s in reached] == [1, 2, 1]


def test_replay_rejects_a_bogus_trace() -> None:
    spec = _counter_spec(3)
    with pytest.raises(ValueError):
        list(replay(spec, ["dec"]))  # dec not enabled at 0


def test_replay_reproduces_a_render_queue_counterexample_shape() -> None:
    # A correct render-queue spec has no safety counterexample, but we can still
    # replay a hand-built valid action prefix to confirm the relation is sound.
    spec = build_render_queue_spec(workers=1)
    reached = list(replay(spec, ["claim_w1", "submit", "poll", "succeed"]))
    assert reached[-1][1].is_terminal()
    assert reached[-1][1].spent == 1
    assert all(isinstance(s, RenderJobState) for _a, s in reached)
