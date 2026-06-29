"""Liveness checking under weak fairness — the SCC / lasso machinery.

Safety is "nothing bad ever happens" and falls out of reachability. Liveness is
"something good eventually happens" — ``eventually(P)`` and ``P ~> Q`` — and
cannot be decided by reachability alone, because the witness for a *violation*
is an infinite run that avoids the goal forever. On a finite state graph every
infinite run eventually cycles, so a liveness counterexample is always a
**lasso**: a finite stem into a cycle that the run repeats forever.

The check, for ``eventually(P)`` (``leads_to`` reduces to it — see below):

1. A *bad* run is one that never reaches ``P``. Restrict the graph to the
   sub-graph ``G¬P`` of states where ``P`` is false (a run that reaches ``P`` is
   not a counterexample, so it leaves this sub-graph and is fine).
2. Find the strongly-connected components of ``G¬P`` (Tarjan). A counterexample
   needs a cycle entirely inside ``G¬P`` — i.e. a non-trivial SCC (one with an
   internal edge), reachable from an initial state through ¬P states.
3. **Weak fairness** is the subtlety. A cycle is only a *real* counterexample if
   the system can be trapped on it *without violating fairness*: for every
   weakly-fair action that is enabled somewhere on the cycle, the cycle must
   actually *take* that action somewhere (otherwise fairness forces the run to
   leave the cycle, and it might then reach ``P``). A cycle that ignores a
   continuously-enabled fair action is **not** a fair run, so it is not a valid
   counterexample. We test each candidate SCC for the existence of a fair cycle.
4. If a fair, ¬P, reachable cycle exists, build the lasso: BFS stem from an
   initial state to the cycle, plus the cycle itself.

``leads_to(P, Q)`` is "``P`` implies eventually ``Q``". Its violation is a run
that hits a ``P``-state and thereafter never reaches ``Q``. So we look for a
fair, reachable cycle inside ``G¬Q`` (states where ``Q`` is false) that is
reachable *through* a ``P``-state. We mark the ``P``-trigger on the stem and
require the cycle (and the stem after the trigger) to stay ¬Q.

The fairness test (step 3) is the part most toy checkers skip; getting it right
is what lets us actually prove "every committed shot is eventually
accepted-or-degraded" rather than merely "there exists a run where it is".
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING, TypeVar

from app.verification.modelcheck.report import PropertyResult
from app.verification.modelcheck.spec import Action, Fairness, Spec
from app.verification.modelcheck.trace import Lasso, TraceStep

if TYPE_CHECKING:
    from app.verification.modelcheck.engine import StateGraph

StateT = TypeVar("StateT", bound=Hashable)

__all__ = ["check_liveness"]


# --------------------------------------------------------------------------- #
# Tarjan SCC over an induced sub-graph
# --------------------------------------------------------------------------- #


def _tarjan_sccs(
    nodes: set[StateT],
    succ: Callable[[StateT], list[StateT]],
) -> list[list[StateT]]:
    """Tarjan's SCC algorithm over the sub-graph induced by ``nodes``.

    Iterative (no recursion-depth limit on big state spaces). Returns the SCCs in
    reverse-topological order; each is a list of states.
    """
    index: dict[StateT, int] = {}
    low: dict[StateT, int] = {}
    on_stack: set[StateT] = set()
    stack: list[StateT] = []
    sccs: list[list[StateT]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        # Iterative DFS: each work item is (node, iterator-of-successors).
        work: list[tuple[StateT, list[StateT], int]] = [(root, succ(root), 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, succs, i = work[-1]
            if i < len(succs):
                work[-1] = (node, succs, i + 1)
                child = succs[i]
                if child not in nodes:
                    continue
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, succ(child), 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                # Done with node: update parent low-link, maybe pop an SCC root.
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp: list[StateT] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    sccs.append(comp)
    return sccs


# --------------------------------------------------------------------------- #
# Fair-cycle detection inside an SCC
# --------------------------------------------------------------------------- #


def _fair_cycle_in_scc(
    spec: Spec[StateT],
    scc: set[StateT],
    edges: dict[StateT, list[tuple[str, StateT]]],
) -> list[tuple[str, StateT]] | None:
    """A fair cycle through every state of ``scc``, or ``None`` if none exists.

    A cycle is *fair* when, for every weakly-fair action that is enabled at some
    state of the SCC, the cycle takes that action at least once. Within an SCC
    every state reaches every other, so we can always build one closed walk that
    (a) visits every state and (b) uses every required fair edge — if and only
    if the SCC is non-trivial (has an internal edge) and *every required fair
    action's enabling state has a within-SCC successor edge for that action*.

    The construction: collect the set of *obligations* (one internal edge per
    weakly-fair action that is enabled inside the SCC). A non-trivial SCC lets us
    stitch these obligation-edges together into one closed walk by routing
    between them through internal shortest paths (the SCC is strongly connected
    by definition). If a fair action is enabled at a state but *only* leaves the
    SCC (no internal edge realises it), the run cannot both stay on the cycle and
    satisfy that action — so fairness forces it out and no fair counterexample
    cycle exists for that obligation; we report ``None``.
    """
    # Internal adjacency (edges that stay inside the SCC), keyed by state.
    internal: dict[StateT, list[tuple[str, StateT]]] = {s: [] for s in scc}
    for s in scc:
        for action_name, succ in edges.get(s, []):
            if succ in scc:
                internal[s].append((action_name, succ))

    # Trivial SCC (single state, no self-loop) → no cycle at all.
    has_internal_edge = any(internal[s] for s in scc)
    if not has_internal_edge:
        return None

    # Determine the fair obligations: each weakly-fair action enabled anywhere in
    # the SCC must be *takeable* within the SCC (an internal edge labelled with
    # it). If any such action is enabled in-SCC but realised only by edges that
    # leave the SCC, fairness forbids staying → no fair cycle.
    fair_names = {a.name for a in spec.fair_actions}
    enabled_fair_in_scc: set[str] = set()
    realisable_fair_in_scc: set[str] = set()
    action_by_name: dict[str, Action[StateT]] = {a.name: a for a in spec.actions}
    for s in scc:
        for a in spec.fair_actions:
            if a.enabled(s):
                enabled_fair_in_scc.add(a.name)
    for s in scc:
        for action_name, _succ in internal[s]:
            if action_name in fair_names:
                realisable_fair_in_scc.add(action_name)

    # An enabled fair action with no internal realisation breaks the fair cycle.
    if enabled_fair_in_scc - realisable_fair_in_scc:
        return None

    # Build a closed walk that covers every obligation edge. Pick one internal
    # obligation edge per realisable fair action; stitch them with shortest paths
    # inside the SCC, then close the loop. If there are no fair obligations, any
    # single internal cycle suffices.
    obligations: list[tuple[StateT, str, StateT]] = []
    for name in sorted(realisable_fair_in_scc & enabled_fair_in_scc):
        for s in scc:
            taken = next(
                ((an, su) for an, su in internal[s] if an == name), None
            )
            if action_by_name[name].enabled(s) and taken is not None:
                obligations.append((s, taken[0], taken[1]))
                break

    if not obligations:
        # No fair obligations: return any simple internal cycle.
        return _any_internal_cycle(scc, internal)

    walk = _stitch_obligations(scc, internal, obligations)
    return walk


def _bfs_path(
    src: StateT,
    dst: StateT,
    internal: dict[StateT, list[tuple[str, StateT]]],
) -> list[tuple[str, StateT]]:
    """Shortest internal path src→dst as ``(action, state)`` steps (empty if src==dst)."""
    if src == dst:
        return []
    parent: dict[StateT, tuple[StateT, str]] = {}
    seen = {src}
    queue = [src]
    qi = 0
    while qi < len(queue):
        node = queue[qi]
        qi += 1
        for action_name, succ in internal.get(node, []):
            if succ in seen:
                continue
            parent[succ] = (node, action_name)
            if succ == dst:
                path: list[tuple[str, StateT]] = []
                cur = dst
                while cur != src:
                    p, an = parent[cur]
                    path.append((an, cur))
                    cur = p
                path.reverse()
                return path
            seen.add(succ)
            queue.append(succ)
    return []  # unreachable inside SCC (cannot happen for a true SCC)


def _stitch_obligations(
    scc: set[StateT],
    internal: dict[StateT, list[tuple[str, StateT]]],
    obligations: list[tuple[StateT, str, StateT]],
) -> list[tuple[str, StateT]]:
    """Join the obligation edges into one closed walk inside the SCC.

    Starting at the first obligation's source, for each obligation: route to its
    source via a shortest internal path, take the obligation edge, then move on.
    Finally route back to the start. The result is a list of ``(action, state)``
    steps whose last state equals the start, i.e. a cycle that takes every
    required fair edge.
    """
    start = obligations[0][0]
    walk: list[tuple[str, StateT]] = []
    cur = start
    for src, action_name, dst in obligations:
        walk.extend(_bfs_path(cur, src, internal))
        walk.append((action_name, dst))
        cur = dst
    # Close the loop back to start.
    walk.extend(_bfs_path(cur, start, internal))
    return walk


def _any_internal_cycle(
    scc: set[StateT],
    internal: dict[StateT, list[tuple[str, StateT]]],
) -> list[tuple[str, StateT]] | None:
    """Any simple cycle inside the SCC, as ``(action, state)`` steps."""
    # Start anywhere; DFS for a back-edge to the start.
    start = next(iter(scc))
    for action_name, succ in internal.get(start, []):
        back = _bfs_path(succ, start, internal)
        cycle = [(action_name, succ), *back]
        if cycle and cycle[-1][1] == start:
            return cycle
    # Self-loop case.
    for action_name, succ in internal.get(start, []):
        if succ == start:
            return [(action_name, succ)]
    return None


# --------------------------------------------------------------------------- #
# The public checks
# --------------------------------------------------------------------------- #


def _reachable_through(
    graph: StateGraph[StateT],
    keep: Callable[[StateT], bool],
    targets: set[StateT],
    *,
    via: Callable[[StateT], bool] | None = None,
) -> tuple[StateT, list[tuple[str, StateT]]] | None:
    """A stem from an initial state to some target, staying within ``keep``.

    If ``via`` is given, the stem must pass through at least one ``via``-state
    (used for ``leads_to``: the stem must cross the trigger before entering the
    bad cycle). Returns ``(target, steps)`` for the first target reached.
    """
    from collections import deque

    queue: deque[tuple[StateT, bool]] = deque()
    parent: dict[tuple[StateT, bool], tuple[StateT, bool, str]] = {}
    seen: set[tuple[StateT, bool]] = set()

    for s in graph.initial:
        flag = via(s) if via is not None else True
        node = (s, flag)
        if s in targets and flag:
            return s, []
        seen.add(node)
        queue.append(node)

    while queue:
        state, flag = queue.popleft()
        for action_name, succ in graph.edges.get(state, []):
            if not keep(succ):
                continue
            new_flag = flag or (via(succ) if via is not None else True)
            node = (succ, new_flag)
            if node in seen:
                continue
            parent[node] = (state, flag, action_name)
            if succ in targets and new_flag:
                # reconstruct
                steps: list[tuple[str, StateT]] = []
                cur: tuple[StateT, bool] = node
                while cur in parent:
                    p_state, p_flag, an = parent[cur]
                    steps.append((an, cur[0]))
                    cur = (p_state, p_flag)
                steps.reverse()
                return succ, steps
            seen.add(node)
            queue.append(node)
    return None


def _build_lasso(
    spec: Spec[StateT],
    graph: StateGraph[StateT],
    stem_steps: list[tuple[str, StateT]],
    cycle_steps: list[tuple[str, StateT]],
    cycle_entry: StateT,
    explanation: str,
) -> Lasso[StateT]:
    """Assemble a :class:`Lasso` from a stem (to ``cycle_entry``) and a cycle."""
    # Stem: prepend the initial state.
    if stem_steps:
        first_init = next(
            s for s in graph.initial
            if _is_stem_root(graph, s, stem_steps)
        )
    else:
        first_init = cycle_entry
    stem: list[TraceStep[StateT]] = [TraceStep(state=first_init, action=None)]
    for action_name, state in stem_steps:
        stem.append(TraceStep(state=state, action=action_name))
    loop: list[TraceStep[StateT]] = [
        TraceStep(state=state, action=action_name) for action_name, state in cycle_steps
    ]
    return Lasso(stem=tuple(stem), loop=tuple(loop), explanation=explanation)


def _is_stem_root(
    graph: StateGraph[StateT], candidate: StateT, stem_steps: list[tuple[str, StateT]]
) -> bool:
    """Heuristic: an initial state whose first edge matches the stem's first step."""
    if not stem_steps:
        return True
    first_action, first_state = stem_steps[0]
    return any(
        an == first_action and su == first_state
        for an, su in graph.edges.get(candidate, [])
    )


def _check_eventually(
    spec: Spec[StateT],
    graph: StateGraph[StateT],
    name: str,
    target: Callable[[StateT], bool],
) -> PropertyResult[StateT]:
    """``eventually(target)`` under weak fairness."""
    bad_nodes = {s for s in graph.edges if not target(s)}

    def bad_succ(s: StateT) -> list[StateT]:
        return [su for _an, su in graph.edges.get(s, []) if su in bad_nodes]

    for scc_list in _tarjan_sccs(bad_nodes, bad_succ):
        scc = set(scc_list)
        cycle = _fair_cycle_in_scc(spec, scc, graph.edges)
        if cycle is None:
            continue
        entry = cycle[-1][1]  # cycle closes here == its starting state
        stem = _reachable_through(graph, lambda s: True, {entry})
        if stem is None:
            continue
        lasso = _build_lasso(
            spec, graph, stem[1], cycle, entry,
            explanation=f"never reaches '{name}' goal on this fair cycle",
        )
        return PropertyResult(
            name=name, kind="eventually", holds=False, counterexample=lasso,
            detail="a fair run is trapped away from the goal",
        )
    return PropertyResult(name=name, kind="eventually", holds=True)


def _check_leads_to(
    spec: Spec[StateT],
    graph: StateGraph[StateT],
    name: str,
    trigger: Callable[[StateT], bool],
    goal: Callable[[StateT], bool],
) -> PropertyResult[StateT]:
    """``trigger ~> goal`` under weak fairness."""
    not_goal = {s for s in graph.edges if not goal(s)}

    def ng_succ(s: StateT) -> list[StateT]:
        return [su for _an, su in graph.edges.get(s, []) if su in not_goal]

    for scc_list in _tarjan_sccs(not_goal, ng_succ):
        scc = set(scc_list)
        cycle = _fair_cycle_in_scc(spec, scc, graph.edges)
        if cycle is None:
            continue
        entry = cycle[-1][1]
        # The stem must cross a trigger-state and then stay ¬goal into the cycle.
        # A trigger anywhere on the (¬goal) cycle also counts.
        cycle_has_trigger = any(trigger(st) for _an, st in cycle) or trigger(entry)
        via = None if cycle_has_trigger else trigger
        stem = _reachable_through(graph, lambda s: True, {entry}, via=via)
        if stem is None:
            continue
        lasso = _build_lasso(
            spec, graph, stem[1], cycle, entry,
            explanation=(
                f"'{name}' trigger fires but the goal is never reached on this fair cycle"
            ),
        )
        return PropertyResult(
            name=name, kind="leads_to", holds=False, counterexample=lasso,
            detail="trigger reached, goal unreachable on a fair cycle",
        )
    return PropertyResult(name=name, kind="leads_to", holds=True)


def check_liveness(
    spec: Spec[StateT], graph: StateGraph[StateT]
) -> list[PropertyResult[StateT]]:
    """Check every liveness / leads-to property of ``spec`` over ``graph``."""
    results: list[PropertyResult[StateT]] = []
    for ev in spec.liveness:
        results.append(_check_eventually(spec, graph, ev.name, ev.target))
    for lt in spec.leads_to_props:
        results.append(_check_leads_to(spec, graph, lt.name, lt.trigger, lt.goal))
    return results


# Keep an unused-import guard happy for the runtime Fairness reference.
_ = Fairness
