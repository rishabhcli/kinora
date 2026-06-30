"""Shared deterministic doubles for the saga test suite.

Everything here is in-memory and driven by a :class:`app.sagas.FakeClock`; no
infra, no network, no wall-clock sleeps. The :class:`AdvancingSleeper` advances
the fake clock by exactly the requested duration so retry backoff and total /
per-attempt deadlines are reproducible. :class:`Recorder` captures the order of
side effects so tests can assert idempotency (a step ran once) and reverse
compensation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.sagas import FakeClock, RunState, StepRecord


class AdvancingSleeper:
    """A sleeper that advances a :class:`FakeClock` instead of blocking.

    Each ``await sleeper(s)`` records the requested duration, advances the clock
    by ``s``, and yields once to the event loop (so concurrent tasks interleave
    deterministically). Real time never passes.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Yield first so a racing action task gets a chance to run, then advance.
        await asyncio.sleep(0)
        if seconds > 0:
            self.clock.advance(seconds)


class Recorder:
    """Records ordered ``(op, *args)`` side effects + per-op call counts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def add(self, *parts: Any) -> None:
        self.calls.append(tuple(str(p) for p in parts))

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)

    def ops_matching(self, *prefixes: str) -> list[str]:
        return [c[0] for c in self.calls if c[0] in prefixes]


def seq_run_ids(prefix: str = "run") -> Callable[[], str]:
    """A deterministic run-id factory yielding ``run-0``, ``run-1``, …."""
    counter = {"n": 0}

    def factory() -> str:
        rid = f"{prefix}-{counter['n']}"
        counter["n"] += 1
        return rid

    return factory


def record_of(state: RunState, name: str) -> StepRecord:
    """The step record for ``name`` (asserts presence — keeps tests type-clean)."""
    rec = state.step_by_name(name)
    assert rec is not None, f"no step record for {name!r}"
    return rec
