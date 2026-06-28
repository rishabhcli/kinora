"""SDK client tests — typed accessors, exposure logging, de-duplication."""

from __future__ import annotations

from app.flags.client import ExposureEvent, FlagsClient, MemoryExposureSink
from app.flags.context import EvalContext
from app.flags.experiment import Experiment, ExperimentStatus, Variant
from app.flags.models import (
    Flag,
    FlagKind,
    FlagSnapshot,
    Variation,
)


def snap(*flags: Flag, version: int = 1) -> FlagSnapshot:
    return FlagSnapshot.from_flags(flags, version=version)


def test_bool_variation() -> None:
    c = FlagsClient(snap(Flag.boolean("x", enabled=True, rollout_percent=100.0)))
    assert c.bool_variation("x", EvalContext.of("u")) is True
    assert c.bool_variation("missing", EvalContext.of("u"), default=True) is True


def test_bool_variation_type_mismatch_falls_back() -> None:
    f = Flag.multivariate(
        "s", (Variation("a", "hello"), Variation("b", "world")), default="a"
    )
    c = FlagsClient(snap(f))
    # string flag asked for a bool -> default
    assert c.bool_variation("s", EvalContext.of("u"), default=True) is True


def test_string_int_float_json_accessors() -> None:
    flags = (
        Flag.multivariate("s", (Variation("a", "hi"),), default="a", kind=FlagKind.STRING),
        Flag.multivariate(
            "n", (Variation("a", 7),), default="a", kind=FlagKind.NUMBER
        ),
        Flag.multivariate(
            "f", (Variation("a", 1.5),), default="a", kind=FlagKind.NUMBER
        ),
        Flag.multivariate(
            "j", (Variation("a", {"k": [1, 2]}),), default="a", kind=FlagKind.JSON
        ),
    )
    c = FlagsClient(snap(*flags))
    u = EvalContext.of("u")
    assert c.string_variation("s", u) == "hi"
    assert c.int_variation("n", u) == 7
    assert c.float_variation("f", u) == 1.5
    assert c.json_variation("j", u) == {"k": [1, 2]}


def test_int_variation_rejects_bool_and_float_noninteger() -> None:
    fb = Flag.boolean("b", enabled=True, rollout_percent=100.0)
    ff = Flag.multivariate("f", (Variation("a", 1.5),), default="a", kind=FlagKind.NUMBER)
    c = FlagsClient(snap(fb, ff))
    u = EvalContext.of("u")
    assert c.int_variation("b", u, default=99) == 99  # bool not coerced to int
    assert c.int_variation("f", u, default=99) == 99  # 1.5 is not integral
    # integral float is accepted
    fi = Flag.multivariate("fi", (Variation("a", 4.0),), default="a", kind=FlagKind.NUMBER)
    c2 = FlagsClient(snap(fi))
    assert c2.int_variation("fi", u) == 4


def test_is_enabled() -> None:
    c = FlagsClient(snap(Flag.boolean("on", enabled=True, rollout_percent=100.0)))
    assert c.is_enabled("on", EvalContext.of("u"))
    assert not c.is_enabled("absent", EvalContext.of("u"))


def test_experiment_assignment_and_exposure_logging() -> None:
    sink = MemoryExposureSink()
    exp = Experiment(
        key="ab",
        variants=(Variant("control", 5000, is_control=True), Variant("treatment", 5000)),
        salt="ab-salt",
        status=ExperimentStatus.RUNNING,
    )
    c = FlagsClient(snap(Flag.boolean("x")), experiments=(exp,), exposure_sink=sink)
    a = c.assign("ab", EvalContext.of("u1"))
    assert a is not None and a.in_experiment
    assert len(sink.events) == 1
    # repeat for same unit -> deduped
    c.assign("ab", EvalContext.of("u1"))
    assert len(sink.events) == 1
    # new unit -> new exposure
    c.assign("ab", EvalContext.of("u2"))
    assert len(sink.events) == 2


def test_assign_unknown_experiment_returns_none() -> None:
    c = FlagsClient(snap(Flag.boolean("x")))
    assert c.assign("ghost", EvalContext.of("u")) is None
    assert c.variant_key("ghost", EvalContext.of("u")) is None


def test_flag_exposure_logging_opt_in() -> None:
    sink = MemoryExposureSink()
    f = Flag.boolean("x", enabled=True, rollout_percent=100.0)
    c = FlagsClient(snap(f), exposure_sink=sink, log_flag_exposures=True)
    c.bool_variation("x", EvalContext.of("u1"))
    c.bool_variation("x", EvalContext.of("u1"))  # deduped by (flag,key)
    c.bool_variation("x", EvalContext.of("u2"))
    assert len(sink.events) == 2
    # a missing flag (default) does NOT log an exposure
    c.bool_variation("ghost", EvalContext.of("u3"))
    assert len(sink.events) == 2


def test_flag_exposure_not_logged_by_default() -> None:
    sink = MemoryExposureSink()
    c = FlagsClient(snap(Flag.boolean("x", rollout_percent=100.0)), exposure_sink=sink)
    c.bool_variation("x", EvalContext.of("u1"))
    assert sink.events == []  # opt-in only


def test_memory_sink_counts() -> None:
    sink = MemoryExposureSink()
    for i in range(5):
        sink.record(
            ExposureEvent(
                kind="experiment",
                subject_key="ab",
                variation="treatment",
                context=EvalContext.of(f"u{i}"),
                dedup_key=f"ab:u{i}",
                payload={},
            )
        )
    assert sink.count("ab", "treatment") == 5
    assert sink.count("ab", "control") == 0


def test_snapshot_version_and_flag_keys() -> None:
    c = FlagsClient(snap(Flag.boolean("a"), Flag.boolean("b"), version=7))
    assert c.snapshot_version == 7
    assert c.flag_keys == ("a", "b")


def test_anonymous_context_not_exposed() -> None:
    sink = MemoryExposureSink()
    exp = Experiment(
        key="ab",
        variants=(Variant("c", 5000, is_control=True), Variant("t", 5000)),
        salt="s",
        status=ExperimentStatus.RUNNING,
    )
    c = FlagsClient(snap(Flag.boolean("x")), experiments=(exp,), exposure_sink=sink)
    anon = EvalContext(key="anon-1", anonymous=True)
    a = c.assign("ab", anon)
    assert a is not None and a.in_experiment  # still bucketed
    # exposure recorded with dedup_key None -> appended once but never deduped;
    # the engine yields None so anonymous units are not tracked durably.
    assert all(e.dedup_key is None for e in sink.events)
