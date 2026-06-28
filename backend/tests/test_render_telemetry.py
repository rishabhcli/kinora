"""The render-engine telemetry bus (kinora.md §12.5).

Typed event stream, fan-out to sinks, sequence ordering, crash isolation, and
the additive metrics translation. No ffmpeg/DB/network.
"""

from __future__ import annotations

from app.observability import metrics
from app.render.ladder import Rung
from app.render.states import RenderState
from app.render.telemetry import (
    EventKind,
    MetricsSink,
    RecordingSink,
    RenderEvent,
    TelemetryBus,
    TelemetrySink,
    recording_bus,
)


def test_events_serialise_to_json_friendly_dicts() -> None:
    e = RenderEvent.state_entered("shot_1", RenderState.RENDERING)
    assert e.as_dict() == {
        "shot_id": "shot_1",
        "kind": "state_entered",
        "seq": 0,
        "state": "rendering",
    }
    rung = RenderEvent.rung_selected("shot_1", Rung.KEN_BURNS_KEYFRAME, reason="budget_low")
    d = rung.as_dict()
    assert d["rung"] == "ken_burns_keyframe"
    assert d["data"]["reason"] == "budget_low"


def test_bus_assigns_monotone_sequence_and_fans_out() -> None:
    bus, recorder = recording_bus()
    bus.publish(RenderEvent.state_entered("s", RenderState.CACHE_CHECK))
    bus.publish(RenderEvent.state_entered("s", RenderState.RENDERING))
    bus.publish(
        RenderEvent.shot_finished(
            "s", RenderState.ACCEPTED, rung=Rung.FULL_WAN, video_seconds=5.0, attempts=1
        )
    )
    seqs = [e.seq for e in recorder.events()]
    assert seqs == [1, 2, 3]
    assert bus.seq == 3


def test_recorder_filters_by_shot_and_kind() -> None:
    bus, recorder = recording_bus()
    bus.publish(RenderEvent.state_entered("a", RenderState.RENDERING))
    bus.publish(RenderEvent.state_entered("b", RenderState.RENDERING))
    bus.publish(RenderEvent.poisoned("a", failures=3, reason="crash"))
    assert len(recorder.events(shot_id="a")) == 2
    assert len(recorder.events(shot_id="b")) == 1
    assert recorder.count(EventKind.POISONED) == 1
    assert recorder.as_dicts(kind=EventKind.POISONED)[0]["data"]["failures"] == 3


def test_recorder_is_bounded() -> None:
    recorder = RecordingSink(capacity=4)
    bus = TelemetryBus([recorder])
    for i in range(10):
        bus.publish(RenderEvent.step_skipped(f"s{i}", "reserve"))
    assert len(recorder) == 4  # only the last 4 retained
    # The bus seq keeps counting past the recorder's window.
    assert bus.seq == 10


def test_bad_sink_is_isolated() -> None:
    class Exploding:
        def emit(self, event: RenderEvent) -> None:
            raise RuntimeError("boom")

    recorder = RecordingSink()
    bus = TelemetryBus([Exploding(), recorder])
    # The good sink still receives the event despite the bad one raising.
    bus.publish(RenderEvent.state_entered("s", RenderState.QA))
    assert len(recorder.events()) == 1


def test_metrics_sink_translates_additive_series() -> None:
    sink: TelemetrySink = MetricsSink()
    before_cp = metrics.render_checkpoints_total._value.get()
    before_poison = metrics.render_poison_total._value.get()
    before_resume = metrics.render_resumes_total._value.get()
    sink.emit(RenderEvent.checkpointed("s", RenderState.RENDERING, attempt=1))
    sink.emit(RenderEvent.resumed("s", RenderState.RENDERING, attempt=1))
    sink.emit(RenderEvent.poisoned("s", failures=3, reason="crash"))
    sink.emit(RenderEvent.step_skipped("s", "generate"))
    assert metrics.render_checkpoints_total._value.get() == before_cp + 1
    assert metrics.render_poison_total._value.get() == before_poison + 1
    assert metrics.render_resumes_total._value.get() == before_resume + 1
    # The labelled step-skip series accepts the step label without error.
    assert metrics.render_steps_skipped_total.labels(step="generate")._value.get() >= 1


def test_add_sink_after_construction() -> None:
    bus = TelemetryBus()
    recorder = RecordingSink()
    bus.publish(RenderEvent.state_entered("s", RenderState.RENDERING))  # no sinks yet
    bus.add_sink(recorder)
    bus.publish(RenderEvent.state_entered("s", RenderState.QA))
    assert len(recorder.events()) == 1  # only the post-add event reached the recorder
