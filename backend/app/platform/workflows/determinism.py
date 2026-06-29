"""Deterministic sources for workflow code.

A workflow body must be *pure with respect to its inputs and history*: replaying
it must reproduce the same commands. Anything that would normally be a source of
non-determinism — the wall clock, a random number, a UUID — has to be derived
**deterministically from the run** so that replay yields the identical value.

This module provides:

* :class:`DeterministicRandom` — a ``random.Random`` seeded from the workflow's
  run id, so ``workflow.random()`` is stable across replays. (For values that
  genuinely must vary per-execution-attempt, use ``workflow.side_effect`` instead,
  which *records* the value into history.)
* :func:`uuid_from` — a deterministic UUID derived from the run id + a sequence
  number (UUID5 over a fixed namespace), for stable ids inside a workflow.
* :class:`WorkflowTime` — exposes the workflow's notion of "now", which is **not**
  the wall clock but the timestamp of the event currently being processed, so
  ``workflow.now()`` returns the same instant on replay.

Workflow code never imports ``random``/``time``/``uuid`` directly — it goes
through the :class:`~app.platform.workflows.context.WorkflowContext`, which owns
instances of these. Lint/static checks for forbidden imports live in
:mod:`app.platform.workflows.sandbox`.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime
from typing import Any

#: Fixed namespace for deterministic workflow UUIDs (a constant, never changes).
_WORKFLOW_UUID_NAMESPACE = uuid.UUID("6f6e1c6e-0000-4000-8000-6b696e6f7261")  # "kinora"-ish


class DeterministicRandom:
    """A run-seeded RNG so ``workflow.random()`` replays identically.

    Seeded from the run id (a stable per-execution string). Two replays of the
    same run draw the same sequence; two different runs draw different sequences.
    """

    __slots__ = ("_rng",)

    def __init__(self, seed: str) -> None:
        # Hash the seed to an int deterministically (Python's hash() is salted).
        self._rng = random.Random(_stable_seed(seed))

    def random(self) -> float:
        return self._rng.random()

    def randint(self, a: int, b: int) -> int:
        return self._rng.randint(a, b)

    def choice(self, seq: list[Any]) -> Any:
        return self._rng.choice(seq)

    def shuffle(self, seq: list[Any]) -> None:
        self._rng.shuffle(seq)

    def uniform(self, a: float, b: float) -> float:
        return self._rng.uniform(a, b)


def uuid_from(run_id: str, seq: int) -> str:
    """A deterministic UUID (hex) from a run id + sequence number."""
    return uuid.uuid5(_WORKFLOW_UUID_NAMESPACE, f"{run_id}:{seq}").hex


class WorkflowTime:
    """The workflow's deterministic clock: the timestamp of the current event.

    The executor sets :attr:`current` to the timestamp of each event as it
    processes it during replay; ``workflow.now()`` reads it. This makes time a
    *replayed* value, not a live read, so a workflow that branches on ``now()``
    takes the same branch on resume.
    """

    __slots__ = ("current",)

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current


def _stable_seed(text: str) -> int:
    """A stable, salt-free integer seed from a string (FNV-1a 64-bit)."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


__all__ = ["DeterministicRandom", "WorkflowTime", "uuid_from"]
