"""The specification DSL — states, actions, fairness, properties.

A model is built from four pieces, mirroring a TLA+ module:

* a **state** — any hashable, immutable Python value. By convention a
  ``frozen`` dataclass or a tuple; the engine only requires ``__hash__`` and
  ``__eq__`` (so structurally-equal states collapse). :class:`State` is a tiny
  base you may inherit for ergonomics, but it is not mandatory.
* a set of **actions** — each an :class:`Action`: a name, a *guard* (is this
  step enabled in this state?), and an *effect* (given an enabled state, yield
  the successor state(s)). One action may be non-deterministic — its effect can
  yield several successors, modelling "any of these could happen next".
* a **fairness** annotation per action — ``WEAK`` (the default for actions that
  represent autonomous progress, e.g. a worker draining the queue) or ``NONE``
  (an action that need never be taken, e.g. an adversarial crash). Weak fairness
  is the assumption liveness checking runs under: an action continuously enabled
  is eventually taken.
* the **properties** to check — :class:`Invariant` (safety) and the liveness
  builders :func:`eventually` / :func:`leads_to`.

Everything is pure: guards and effects must be side-effect-free functions of the
state, because the engine calls them repeatedly while exploring.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, runtime_checkable

__all__ = [
    "Action",
    "Fairness",
    "Invariant",
    "LeadsTo",
    "Liveness",
    "Property",
    "Spec",
    "State",
    "StateT",
    "eventually",
    "invariant",
    "leads_to",
]


@runtime_checkable
class _Hashable(Protocol):
    def __hash__(self) -> int: ...


#: A protocol-bound type variable for the state. Any immutable, hashable value
#: works; :class:`State` is a convenience base, not a requirement.
StateT = TypeVar("StateT", bound=_Hashable)


class State:
    """Optional convenience base for a model state.

    Subclasses should be immutable (a ``frozen`` dataclass is ideal). This base
    deliberately adds nothing but a marker and a readable ``repr`` fallback — the
    engine works on any hashable value, so plain tuples or your own frozen
    dataclasses are equally welcome.
    """

    __slots__ = ()


class Fairness(enum.Enum):
    """The fairness assumption attached to an action for liveness checking."""

    #: The action need never be taken (an adversarial / optional step). It still
    #: contributes successors to the reachable graph, but a liveness cycle is
    #: allowed to ignore it forever.
    NONE = "none"
    #: If the action is *continuously* enabled along an infinite run, it is
    #: eventually taken. This is the standard weak-fairness (WF) assumption and
    #: the one that makes "every committed shot eventually accepted" provable.
    WEAK = "weak"


# A guard answers "is this action enabled in this state?".
Guard = Callable[[StateT], bool]
# An effect, applied to an enabled state, yields the successor state(s). Yielding
# more than one models non-determinism (any successor may occur).
Effect = Callable[[StateT], Iterable[StateT]]


@dataclass(frozen=True, slots=True)
class Action(Generic[StateT]):
    """A guarded, possibly non-deterministic transition (a ``Next`` disjunct).

    ``guard(s)`` decides whether the action is enabled in ``s``; ``effect(s)``
    yields the successor state(s) reachable by taking it. The pair must be pure:
    the engine evaluates them many times while exploring.

    Set ``fairness=Fairness.WEAK`` for actions that model autonomous progress
    (a worker step, a timer firing) so liveness checking may assume they are
    eventually taken when continuously enabled. Leave it ``NONE`` for optional /
    adversarial steps (a crash, a far seek) that the environment need never do.
    """

    name: str
    guard: Guard[StateT]
    effect: Effect[StateT]
    fairness: Fairness = Fairness.NONE

    def enabled(self, state: StateT) -> bool:
        """True iff this action may fire in ``state``."""
        return bool(self.guard(state))

    def successors(self, state: StateT) -> tuple[StateT, ...]:
        """The successor states of taking this action in ``state`` (assumes enabled)."""
        return tuple(self.effect(state))


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #

#: A state predicate.
Predicate = Callable[[StateT], bool]


@dataclass(frozen=True, slots=True)
class Invariant(Generic[StateT]):
    """A safety property: ``predicate`` must hold in **every** reachable state.

    The canonical Kinora invariants live here: ``buffer >= 0``, "reserved
    video-seconds never exceed the budget", "a job is in at most one lane". A
    violation produces the shortest trace to a state where ``predicate`` is
    false.
    """

    name: str
    predicate: Predicate[StateT]

    def holds(self, state: StateT) -> bool:
        return bool(self.predicate(state))


@dataclass(frozen=True, slots=True)
class Liveness(Generic[StateT]):
    """``eventually(P)``: from every initial state, every fair run reaches a P-state.

    Checked under the per-action weak-fairness assumption. A counterexample is a
    *lasso*: a finite stem to a fair cycle on which ``P`` never holds.
    """

    name: str
    target: Predicate[StateT]

    def kind(self) -> str:
        return "eventually"


@dataclass(frozen=True, slots=True)
class LeadsTo(Generic[StateT]):
    """``P ~> Q``: every P-state is, on every fair run, eventually followed by a Q-state.

    The workhorse temporal property for this codebase: "a committed enqueue
    leads-to an accepted-or-degraded shot", "a cancel request leads-to the job
    leaving the queue", "a raised conflict leads-to a logged decision". A
    counterexample is a fair cycle reachable *after* a P-state on which ``Q``
    never holds.
    """

    name: str
    trigger: Predicate[StateT]
    goal: Predicate[StateT]

    def kind(self) -> str:
        return "leads_to"


#: The union of property kinds a :class:`Spec` may carry.
Property = Invariant[StateT] | Liveness[StateT] | LeadsTo[StateT]


def invariant(name: str, predicate: Predicate[StateT]) -> Invariant[StateT]:
    """Build a safety :class:`Invariant`."""
    return Invariant(name=name, predicate=predicate)


def eventually(name: str, target: Predicate[StateT]) -> Liveness[StateT]:
    """Build an ``eventually(target)`` :class:`Liveness` property."""
    return Liveness(name=name, target=target)


def leads_to(
    name: str, trigger: Predicate[StateT], goal: Predicate[StateT]
) -> LeadsTo[StateT]:
    """Build a ``trigger ~> goal`` :class:`LeadsTo` property."""
    return LeadsTo(name=name, trigger=trigger, goal=goal)


@dataclass(frozen=True, slots=True)
class Spec(Generic[StateT]):
    """A complete model: initial states, actions, and properties to check.

    ``state_label`` is an optional pretty-printer used only when formatting a
    counterexample trace; it never affects exploration (states are compared by
    hash/equality). Provide it to make a Kinora trace read like a story
    ("buffer=20 inflight=[shot_3]") instead of a tuple dump.
    """

    name: str
    initial: tuple[StateT, ...]
    actions: tuple[Action[StateT], ...]
    invariants: tuple[Invariant[StateT], ...] = ()
    liveness: tuple[Liveness[StateT], ...] = ()
    leads_to_props: tuple[LeadsTo[StateT], ...] = ()
    state_label: Callable[[StateT], str] | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.initial:
            raise ValueError(f"spec {self.name!r} has no initial states")
        if not self.actions:
            raise ValueError(f"spec {self.name!r} has no actions")
        names = [a.name for a in self.actions]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"spec {self.name!r} has duplicate action names: {dupes}")

    @property
    def fair_actions(self) -> tuple[Action[StateT], ...]:
        """The weakly-fair actions — the ones liveness may assume eventually fire."""
        return tuple(a for a in self.actions if a.fairness is Fairness.WEAK)

    def label(self, state: StateT) -> str:
        """Render ``state`` for a trace line (falls back to ``repr``)."""
        if self.state_label is not None:
            return self.state_label(state)
        return repr(state)

    def enabled_actions(self, state: StateT) -> tuple[Action[StateT], ...]:
        """The actions enabled in ``state`` (the local ``Next`` disjuncts)."""
        return tuple(a for a in self.actions if a.enabled(state))

    def successors(self, state: StateT) -> tuple[tuple[str, StateT], ...]:
        """All ``(action_name, successor)`` edges out of ``state``.

        Deterministic in iteration order (actions in declaration order, then the
        effect's own yield order), so BFS counterexamples are reproducible.
        """
        out: list[tuple[str, StateT]] = []
        for action in self.actions:
            if action.enabled(state):
                for succ in action.successors(state):
                    out.append((action.name, succ))
        return tuple(out)

    def has_liveness(self) -> bool:
        """True if any liveness / leads-to property is declared."""
        return bool(self.liveness) or bool(self.leads_to_props)

    def all_properties(self) -> Sequence[Property[StateT]]:
        """Every declared property, in check order (invariants first)."""
        props: list[Property[StateT]] = []
        props.extend(self.invariants)
        props.extend(self.liveness)
        props.extend(self.leads_to_props)
        return props
