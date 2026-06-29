"""Structured results of a model-checking run.

A :class:`CheckReport` is what :class:`~app.verification.modelcheck.engine.ModelChecker.check`
returns: per-property pass/fail, the state-space size explored, and the
counterexample (a :class:`~app.verification.modelcheck.trace.Trace` for safety /
deadlock, a :class:`~app.verification.modelcheck.trace.Lasso` for liveness) when
something failed. It is the object a test asserts on and the object that prints
a readable summary into DESIGN.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.verification.modelcheck.trace import Lasso, Trace

StateT = TypeVar("StateT")

__all__ = ["CheckReport", "PropertyResult"]


@dataclass(frozen=True, slots=True)
class PropertyResult(Generic[StateT]):
    """The verdict for one property: held, or failed with a witness."""

    name: str
    kind: str  # "invariant" | "deadlock" | "eventually" | "leads_to"
    holds: bool
    #: Present iff ``not holds``. A finite :class:`Trace` for safety/deadlock; a
    #: :class:`Lasso` for liveness.
    counterexample: Trace[StateT] | Lasso[StateT] | None = None
    detail: str = ""

    def render(self, label: Callable[[StateT], str] | None = None) -> str:
        status = "PASS" if self.holds else "FAIL"
        head = f"[{status}] {self.kind}: {self.name}"
        if self.detail:
            head += f" — {self.detail}"
        if self.holds or self.counterexample is None:
            return head
        body = self.counterexample.render(label)
        return f"{head}\n{body}"


@dataclass(frozen=True, slots=True)
class CheckReport(Generic[StateT]):
    """The full result of checking a spec."""

    spec_name: str
    results: tuple[PropertyResult[StateT], ...]
    states_explored: int
    transitions_explored: int
    truncated: bool = False
    symmetry: str = "none"
    state_label: Callable[[StateT], str] | None = field(default=None)

    @property
    def ok(self) -> bool:
        """True iff every checked property held."""
        return all(r.holds for r in self.results)

    @property
    def failures(self) -> tuple[PropertyResult[StateT], ...]:
        return tuple(r for r in self.results if not r.holds)

    def result_for(self, name: str) -> PropertyResult[StateT] | None:
        for r in self.results:
            if r.name == name:
                return r
        return None

    def summary(self) -> str:
        """A one-line headline for the run."""
        passed = sum(1 for r in self.results if r.holds)
        total = len(self.results)
        trunc = " (TRUNCATED)" if self.truncated else ""
        return (
            f"{self.spec_name}: {passed}/{total} properties hold · "
            f"{self.states_explored} states / {self.transitions_explored} transitions"
            f" · symmetry={self.symmetry}{trunc}"
        )

    def render(self) -> str:
        """A full, human-readable report (headline + every property + traces)."""
        lines = [self.summary(), "-" * len(self.summary())]
        for r in self.results:
            lines.append(r.render(self.state_label))
        return "\n".join(lines)

    def assert_ok(self) -> None:
        """Raise :class:`AssertionError` with the failing traces if any property failed.

        The one-liner a test uses: ``checker.check(spec).assert_ok()``.
        """
        if self.ok:
            return
        details = "\n\n".join(r.render(self.state_label) for r in self.failures)
        raise AssertionError(f"{self.spec_name}: property violation(s):\n\n{details}")

    @staticmethod
    def merge(
        spec_name: str, reports: Sequence[CheckReport[StateT]]
    ) -> CheckReport[StateT]:
        """Combine several reports (e.g. one per property batch) into one."""
        results: list[PropertyResult[StateT]] = []
        states = transitions = 0
        truncated = False
        symmetry = "none"
        label: Callable[[StateT], str] | None = None
        for rep in reports:
            results.extend(rep.results)
            states = max(states, rep.states_explored)
            transitions = max(transitions, rep.transitions_explored)
            truncated = truncated or rep.truncated
            if rep.symmetry != "none":
                symmetry = rep.symmetry
            label = label or rep.state_label
        return CheckReport(
            spec_name=spec_name,
            results=tuple(results),
            states_explored=states,
            transitions_explored=transitions,
            truncated=truncated,
            symmetry=symmetry,
            state_label=label,
        )
