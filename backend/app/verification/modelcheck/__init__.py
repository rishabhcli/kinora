"""An explicit-state model checker, TLA+/Spin in the small (facet B).

This is a from-scratch, pure-Python explicit-state checker. You describe a
protocol as a :class:`~app.verification.modelcheck.spec.Spec` — an immutable
``State`` type, a set of initial states, a set of named *actions* (guarded
transitions, the moral equivalent of a TLA+ ``Next`` disjunct), and the
properties you want to hold — and the engine enumerates the reachable state
space, checking the properties over **every** interleaving rather than the few a
unit test happens to schedule.

What it can check
-----------------

* **Safety invariants** — a state predicate that must hold in every reachable
  state (``buffer >= 0``, "budget is never over-reserved"). A violation yields
  the shortest action trace from an initial state to the offending state.
* **Deadlock / stuck-state** detection — a non-terminal state with no enabled
  action.
* **Liveness under weak fairness** — ``eventually(P)`` and ``leads_to(P, Q)``
  ("every committed shot is eventually accepted-or-degraded", "a cancel request
  is eventually honoured"). These are checked over the strongly-connected
  components of the reachable graph: a fair cycle that traps the system away
  from the goal is a liveness counterexample, reported as a ``lasso`` (a finite
  stem + a repeating cycle).

How it explores
---------------

* :class:`~app.verification.modelcheck.engine.ModelChecker` does BFS (shortest
  counterexamples) or DFS over states hashed by a canonical fingerprint.
* :mod:`~app.verification.modelcheck.symmetry` supplies an optional symmetry
  reduction: when a set of components is interchangeable (e.g. two render
  workers, three sessions), the checker canonicalises each state to a
  representative of its symmetry orbit, collapsing the factorial blow-up.

The submodules:

``spec``       the DSL — ``State``, ``Action``, ``Spec``, property builders.
``engine``     BFS/DFS reachability + safety + deadlock checking.
``liveness``   SCC-based weak-fairness liveness checking.
``symmetry``   permutation-orbit canonicalisation.
``trace``      counterexample formatting (action traces + lassos).
``report``     a structured :class:`CheckReport` summarising a run.
"""

from __future__ import annotations

from app.verification.modelcheck.engine import (
    CheckOutcome,
    ExplorationOrder,
    ModelChecker,
)
from app.verification.modelcheck.export import replay, to_dot
from app.verification.modelcheck.report import CheckReport, PropertyResult
from app.verification.modelcheck.spec import (
    Action,
    Invariant,
    LeadsTo,
    Liveness,
    Property,
    Spec,
    State,
    eventually,
    invariant,
    leads_to,
)
from app.verification.modelcheck.symmetry import SymmetryReduction
from app.verification.modelcheck.trace import Lasso, Trace, TraceStep

__all__ = [
    "Action",
    "CheckOutcome",
    "CheckReport",
    "ExplorationOrder",
    "Invariant",
    "Lasso",
    "LeadsTo",
    "Liveness",
    "ModelChecker",
    "Property",
    "PropertyResult",
    "Spec",
    "State",
    "SymmetryReduction",
    "Trace",
    "TraceStep",
    "eventually",
    "invariant",
    "leads_to",
    "replay",
    "to_dot",
]
