"""Counterexample traces — the *why* of a failed check.

A model checker is only useful if, when a property fails, it hands you the
schedule that broke it. Two shapes:

* :class:`Trace` — a finite action sequence from an initial state to a state of
  interest (an invariant violation, a deadlock). This is the shortest such
  sequence when the engine ran in BFS order.
* :class:`Lasso` — a *stem* (finite prefix) plus a *loop* (a cycle the run gets
  trapped in). This is the canonical witness for a liveness failure: the system
  can run forever along ``stem -> loop -> loop -> …`` and never reach the goal.

Both render to a numbered, human-readable transcript using the spec's
``state_label`` so a Kinora trace reads in domain terms.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

StateT = TypeVar("StateT")

__all__ = ["Lasso", "Trace", "TraceStep"]


@dataclass(frozen=True, slots=True)
class TraceStep(Generic[StateT]):
    """One step of a trace: the state, and the action taken to *reach* it.

    The first step of a trace has ``action is None`` — it is an initial state.
    """

    state: StateT
    action: str | None


def _render_steps(
    steps: Sequence[TraceStep[StateT]],
    label: Callable[[StateT], str] | None,
    *,
    start_index: int = 0,
    marker: str = "",
) -> list[str]:
    fmt = label or (lambda s: repr(s))
    lines: list[str] = []
    for i, step in enumerate(steps, start=start_index):
        prefix = f"{marker}{i:>3} "
        if step.action is None:
            lines.append(f"{prefix}[init]            {fmt(step.state)}")
        else:
            lines.append(f"{prefix}--{step.action:<16}-> {fmt(step.state)}")
    return lines


@dataclass(frozen=True, slots=True)
class Trace(Generic[StateT]):
    """A finite witness path from an initial state to a target state."""

    steps: tuple[TraceStep[StateT], ...]

    @property
    def length(self) -> int:
        """Number of *transitions* (states minus one)."""
        return max(0, len(self.steps) - 1)

    @property
    def actions(self) -> tuple[str, ...]:
        """The action names along the trace, in order."""
        return tuple(s.action for s in self.steps if s.action is not None)

    @property
    def final_state(self) -> StateT:
        return self.steps[-1].state

    def render(self, label: Callable[[StateT], str] | None = None) -> str:
        """A numbered transcript of the path."""
        return "\n".join(_render_steps(self.steps, label))


@dataclass(frozen=True, slots=True)
class Lasso(Generic[StateT]):
    """A liveness counterexample: a ``stem`` into a repeating ``loop``.

    ``stem`` runs from an initial state to the entry of the cycle; ``loop`` is
    the cycle itself (its first state equals the stem's last state, repeated to
    close the loop). The run ``stem · loop^ω`` is a fair, infinite execution on
    which the liveness goal never holds.
    """

    stem: tuple[TraceStep[StateT], ...]
    loop: tuple[TraceStep[StateT], ...]
    explanation: str = field(default="")

    @property
    def stem_length(self) -> int:
        return max(0, len(self.stem) - 1)

    @property
    def loop_length(self) -> int:
        return len(self.loop)

    def render(self, label: Callable[[StateT], str] | None = None) -> str:
        """A numbered transcript: stem lines, then a marked, repeating loop."""
        lines = _render_steps(self.stem, label)
        if self.explanation:
            lines.append(f"    ↺ {self.explanation}")
        loop_start = len(self.stem)
        lines.append("    ┌─ loop (repeats forever) ─┐")
        lines.extend(_render_steps(self.loop, label, start_index=loop_start, marker="*"))
        lines.append("    └──────────────────────────┘")
        return "\n".join(lines)
