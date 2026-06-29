"""The explicit-state exploration engine — reachability, safety, deadlock.

:class:`ModelChecker` enumerates the reachable states of a
:class:`~app.verification.modelcheck.spec.Spec`, optionally under a
:class:`~app.verification.modelcheck.symmetry.SymmetryReduction`, and checks:

* every :class:`~app.verification.modelcheck.spec.Invariant` — a violating state
  yields the shortest action trace to it (BFS) ;
* **deadlock** — a non-terminal reachable state with no enabled action, when
  ``check_deadlock`` is on (some protocols legitimately terminate, so it is
  opt-in / parametrised by a "is this an intended terminal state?" predicate);

and hands the reachable graph to :mod:`app.verification.modelcheck.liveness`
for any temporal properties.

Exploration is **breadth-first by default** so safety counterexamples are the
shortest possible — the most useful for a human. DFS is available (smaller
frontier memory on deep, narrow graphs). Either way the engine bounds itself
with ``max_states`` so a mis-specified, infinite model fails loudly (truncated)
instead of hanging.

The engine builds, as it explores, a :class:`StateGraph` — the adjacency the
liveness checker needs — so a single exploration pass serves both safety and
liveness.
"""

from __future__ import annotations

import enum
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.verification.modelcheck.report import CheckReport, PropertyResult
from app.verification.modelcheck.spec import Spec
from app.verification.modelcheck.symmetry import SymmetryReduction
from app.verification.modelcheck.trace import Trace, TraceStep

StateT = TypeVar("StateT", bound=Hashable)

__all__ = [
    "CheckOutcome",
    "ExplorationOrder",
    "ModelChecker",
    "StateGraph",
]

#: A sentinel default; a state count past which exploration aborts as truncated.
DEFAULT_MAX_STATES = 500_000


class ExplorationOrder(enum.Enum):
    """How the reachable graph is walked."""

    BFS = "bfs"  # shortest safety counterexamples
    DFS = "dfs"  # lower frontier memory on deep graphs


class CheckOutcome(enum.Enum):
    """High-level outcome of a run (a convenience over ``report.ok``)."""

    HOLDS = "holds"
    VIOLATED = "violated"
    TRUNCATED = "truncated"


@dataclass(slots=True)
class StateGraph(Generic[StateT]):
    """The explored reachable graph, in canonical-state space.

    ``edges[s]`` is the list of ``(action_name, successor)`` out of canonical
    state ``s``. ``initial`` are the canonical initial states. ``parent`` is the
    BFS spanning forest used to reconstruct the shortest trace to any state.
    ``enabled_count`` records how many concrete actions were enabled at ``s``
    (for deadlock detection — a state with zero enabled actions is a sink).
    """

    initial: tuple[StateT, ...]
    edges: dict[StateT, list[tuple[str, StateT]]] = field(default_factory=dict)
    parent: dict[StateT, tuple[StateT, str]] = field(default_factory=dict)
    enabled_count: dict[StateT, int] = field(default_factory=dict)
    #: Set by a halting exploration: ``(invariant_name, offending_state)``.
    violation: tuple[str, StateT] | None = None

    @property
    def size(self) -> int:
        return len(self.edges)

    @property
    def transition_count(self) -> int:
        return sum(len(v) for v in self.edges.values())

    def trace_to(self, target: StateT) -> Trace[StateT]:
        """Reconstruct the shortest action trace from an initial state to ``target``."""
        chain: list[TraceStep[StateT]] = []
        cur = target
        seen: set[StateT] = set()
        while True:
            if cur in seen:  # cycle guard (should not happen on a BFS forest)
                break
            seen.add(cur)
            if cur in self.parent:
                prev, action = self.parent[cur]
                chain.append(TraceStep(state=cur, action=action))
                cur = prev
            else:
                chain.append(TraceStep(state=cur, action=None))
                break
        chain.reverse()
        return Trace(steps=tuple(chain))


class ModelChecker(Generic[StateT]):
    """Explore a spec's reachable states and check its properties.

    ``order`` chooses BFS (default, shortest counterexamples) or DFS.
    ``symmetry`` collapses orbits (default: none). ``max_states`` bounds the run.
    ``check_deadlock`` enables sink detection; ``is_terminal`` marks states that
    are *allowed* to be sinks (intended termination) so only *unintended* sinks
    are flagged.
    """

    def __init__(
        self,
        *,
        order: ExplorationOrder = ExplorationOrder.BFS,
        symmetry: SymmetryReduction | None = None,
        max_states: int = DEFAULT_MAX_STATES,
        check_deadlock: bool = False,
        is_terminal: Callable[[StateT], bool] | None = None,
        stop_on_violation: bool = True,
    ) -> None:
        self._order = order
        self._symmetry = symmetry or SymmetryReduction.none()
        self._max_states = max_states
        self._check_deadlock = check_deadlock
        self._is_terminal = is_terminal or (lambda _s: False)
        self._stop_on_violation = stop_on_violation

    # -- exploration --------------------------------------------------------- #

    def explore(
        self, spec: Spec[StateT], *, halt_invariants: bool = False
    ) -> tuple[StateGraph[StateT], bool]:
        """Build the reachable :class:`StateGraph`. Returns ``(graph, truncated)``.

        States are canonicalised before being recorded, so a whole symmetry orbit
        collapses to one node. BFS records a spanning forest in ``graph.parent``
        for shortest-trace reconstruction.

        When ``halt_invariants`` is set, BFS stops the moment it *discovers* a
        state that violates one of ``spec.invariants``. Because BFS discovers
        states in non-decreasing depth, the offending state is found at minimal
        distance, so the partial graph still yields the shortest counterexample —
        and a model with an *unbounded* faulty trajectory (the kind a real bug
        produces) is caught in milliseconds instead of running to the
        ``max_states`` cap. ``graph.violation`` records the halting state, if any.
        Liveness is *not* checked on a halted graph (it needs the full space), so
        :meth:`check` only halts when there is no liveness property to check, or
        when an invariant has already failed (liveness is moot then).
        """
        canon = self._symmetry.apply
        invariants = spec.invariants if halt_invariants else ()
        initial = tuple(dict.fromkeys(canon(s) for s in spec.initial))
        graph: StateGraph[StateT] = StateGraph(initial=initial)
        truncated = False

        # An initial state may itself violate an invariant.
        for s in initial:
            graph.edges.setdefault(s, [])
            for inv in invariants:
                if not inv.predicate(s):
                    graph.violation = (inv.name, s)
                    return graph, truncated

        frontier: deque[StateT] = deque(initial)

        def pop() -> StateT:
            return frontier.popleft() if self._order is ExplorationOrder.BFS else frontier.pop()

        while frontier:
            state = pop()
            if state in graph.enabled_count:
                continue  # already expanded (re-queued before expansion)
            enabled = 0
            out: list[tuple[str, StateT]] = []
            halt = False
            for action in spec.actions:
                if not action.enabled(state):
                    continue
                enabled += 1
                for raw_succ in action.successors(state):
                    succ = canon(raw_succ)
                    out.append((action.name, succ))
                    if succ not in graph.edges:
                        if len(graph.edges) >= self._max_states:
                            truncated = True
                            continue
                        graph.edges[succ] = []
                        graph.parent[succ] = (state, action.name)
                        for inv in invariants:
                            if not inv.predicate(succ):
                                graph.violation = (inv.name, succ)
                                halt = True
                                break
                        if halt:
                            break
                        frontier.append(succ)
                if halt:
                    break
            graph.edges[state] = out
            graph.enabled_count[state] = enabled
            if halt or truncated:
                break

        return graph, truncated

    # -- top-level check ----------------------------------------------------- #

    def check(self, spec: Spec[StateT]) -> CheckReport[StateT]:
        """Explore ``spec`` and check every declared property. The headline call.

        If ``stop_on_violation`` is on (the default) the first pass halts BFS at
        the first invariant breach, so a buggy spec with an unbounded faulty
        trajectory is reported in milliseconds rather than after exhausting the
        ``max_states`` cap. When that pass finds a breach, only the violated
        invariant carries a trace and exploration is reported as *halted*; the
        other properties are left unverified (a single safety failure already
        sinks the run). When the halting pass finds nothing — or when the spec
        has liveness properties (which need the full state graph) — the full
        space is explored and every property is checked.
        """
        results: list[PropertyResult[StateT]] = []

        # Phase 1: a cheap halting pass for safety, when enabled and the spec has
        # invariants. A breach here short-circuits the whole run.
        if self._stop_on_violation and spec.invariants:
            halted_graph, truncated = self.explore(spec, halt_invariants=True)
            if halted_graph.violation is not None:
                name, bad = halted_graph.violation
                for inv in spec.invariants:
                    if inv.name == name:
                        results.append(
                            PropertyResult(
                                name=inv.name,
                                kind="invariant",
                                holds=False,
                                counterexample=halted_graph.trace_to(bad),
                                detail="invariant violated (exploration halted at first breach)",
                            )
                        )
                    else:
                        results.append(
                            PropertyResult(
                                name=inv.name,
                                kind="invariant",
                                holds=True,
                                detail="not fully verified — run halted at another breach",
                            )
                        )
                return CheckReport(
                    spec_name=spec.name,
                    results=tuple(results),
                    states_explored=halted_graph.size,
                    transitions_explored=halted_graph.transition_count,
                    truncated=truncated,
                    symmetry=self._symmetry.description,
                    state_label=spec.state_label,
                )

        # Phase 2: full exploration (safety holds so far, or liveness is needed).
        graph, truncated = self.explore(spec)

        # 1. Safety invariants — shortest violating trace via the BFS forest.
        for inv in spec.invariants:
            violating: StateT | None = self._first_violating(graph, inv.predicate)
            if violating is None:
                results.append(
                    PropertyResult(name=inv.name, kind="invariant", holds=True)
                )
            else:
                results.append(
                    PropertyResult(
                        name=inv.name,
                        kind="invariant",
                        holds=False,
                        counterexample=graph.trace_to(violating),
                        detail="invariant violated at the final state of the trace",
                    )
                )

        # 2. Deadlock — an unintended sink.
        if self._check_deadlock:
            results.append(self._check_deadlock_prop(graph))

        # 3. Liveness — delegated to the SCC checker over the same graph.
        if spec.has_liveness():
            from app.verification.modelcheck.liveness import check_liveness

            results.extend(check_liveness(spec, graph))

        return CheckReport(
            spec_name=spec.name,
            results=tuple(results),
            states_explored=graph.size,
            transitions_explored=graph.transition_count,
            truncated=truncated,
            symmetry=self._symmetry.description,
            state_label=spec.state_label,
        )

    # -- helpers ------------------------------------------------------------- #

    def _first_violating(
        self, graph: StateGraph[StateT], predicate: Callable[[StateT], bool]
    ) -> StateT | None:
        """The BFS-nearest reachable state where ``predicate`` is false, if any.

        Walking ``graph.edges`` in insertion order keeps the search breadth-first
        (states were inserted in BFS order), so the returned state is at minimal
        distance and ``trace_to`` yields the shortest counterexample.
        """
        for state in graph.edges:
            if not predicate(state):
                return state
        return None

    def _check_deadlock_prop(self, graph: StateGraph[StateT]) -> PropertyResult[StateT]:
        for state, count in graph.enabled_count.items():
            if count == 0 and not self._is_terminal(state):
                return PropertyResult(
                    name="no_deadlock",
                    kind="deadlock",
                    holds=False,
                    counterexample=graph.trace_to(state),
                    detail="reached a non-terminal state with no enabled action",
                )
        return PropertyResult(name="no_deadlock", kind="deadlock", holds=True)
