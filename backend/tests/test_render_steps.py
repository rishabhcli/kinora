"""The idempotent render step ledger (kinora.md §9.7 resumability).

A step keyed by content runs once; a resume on the same key skips it (no
double-spend / double-write); a key change re-runs it. Serialisation roundtrips.
No ffmpeg/DB/network.
"""

from __future__ import annotations

from app.render.steps import Step, StepLedger, StepRecord, step_key
from app.render.telemetry import EventKind, recording_bus


def test_step_key_is_deterministic_and_input_sensitive() -> None:
    assert step_key("book", "shot", 88001) == step_key("book", "shot", 88001)
    assert step_key("book", "shot", 88001) != step_key("book", "shot", 88002)
    assert step_key("a").startswith("ck1:")


async def test_run_executes_once_then_skips_on_same_key() -> None:
    bus, recorder = recording_bus()
    ledger = StepLedger(shot_id="s", bus=bus)
    calls = 0

    async def generate() -> str:
        nonlocal calls
        calls += 1
        return "clips/s.mp4"

    key = step_key("s", 88001)
    first = await ledger.run(Step.GENERATE, key, generate)
    second = await ledger.run(Step.GENERATE, key, generate)
    assert first == second == "clips/s.mp4"
    assert calls == 1  # the second call skipped the function (idempotent)
    assert recorder.count(EventKind.STEP_SKIPPED) == 1
    assert ledger.result_of(Step.GENERATE) == "clips/s.mp4"


async def test_key_change_reruns_the_step() -> None:
    ledger = StepLedger(shot_id="s")
    calls = 0

    async def generate() -> int:
        nonlocal calls
        calls += 1
        return calls

    await ledger.run(Step.GENERATE, step_key("seed", 1), generate)
    # A new seed → a new key → the step re-runs (a fresh attempt, not a stale serve).
    result = await ledger.run(Step.GENERATE, step_key("seed", 2), generate)
    assert calls == 2
    assert result == 2


async def test_run_sync_variant() -> None:
    ledger = StepLedger(shot_id="s")
    calls = 0

    def persist() -> str:
        nonlocal calls
        calls += 1
        return "lastframes/s.png"

    key = step_key("s", "lastframe")
    a = ledger.run_sync(Step.PERSIST_LASTFRAME, key, persist)
    b = ledger.run_sync(Step.PERSIST_LASTFRAME, key, persist)
    assert a == b
    assert calls == 1


async def test_record_result_false_does_not_retain_heavy_value() -> None:
    ledger = StepLedger(shot_id="s")
    key = step_key("s", "heavy")
    await ledger.run(Step.GENERATE, key, lambda: _coro(b"x" * 1000), record_result=False)
    assert ledger.is_done(Step.GENERATE, key)
    assert ledger.result_of(Step.GENERATE) is None  # not retained


async def _coro(value: object) -> object:
    return value


def test_forget_allows_rerun() -> None:
    ledger = StepLedger(shot_id="s")
    ledger.record(Step.RESERVE, "k1", "res_1")
    assert ledger.is_done(Step.RESERVE, "k1")
    ledger.forget(Step.RESERVE)
    assert not ledger.is_done(Step.RESERVE, "k1")


def test_serialisation_roundtrips() -> None:
    ledger = StepLedger(shot_id="shot_42")
    ledger.record(Step.RESERVE, "k1", "res_1")
    ledger.record(Step.GENERATE, "k2", "clips/shot_42.mp4")
    data = ledger.as_dict()
    restored = StepLedger.from_dict(data)
    assert restored.shot_id == "shot_42"
    assert restored.is_done(Step.RESERVE, "k1")
    assert restored.result_of(Step.GENERATE) == "clips/shot_42.mp4"
    assert len(restored) == 2


def test_step_record_from_dict() -> None:
    rec = StepRecord.from_dict({"name": "qa", "key": "k", "result": {"verdict": "pass"}})
    assert rec.name == "qa"
    assert rec.result["verdict"] == "pass"
