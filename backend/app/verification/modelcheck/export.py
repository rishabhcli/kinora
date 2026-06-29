"""Export a reachable state graph to Graphviz DOT, and replay a trace.

Two debugging conveniences built on a :class:`~app.verification.modelcheck.engine.StateGraph`:

* :func:`to_dot` renders the explored graph as Graphviz DOT — nodes labelled by
  the spec's ``state_label``, edges by action name, initial states boxed, and
  (optionally) the states/edges of a counterexample trace highlighted. Paste the
  output into any Graphviz renderer to *see* the protocol's reachable space and
  exactly where a counterexample walks.

* :func:`replay` walks a trace's action sequence against the spec's transition
  relation and yields each ``(action, state)`` it actually reaches — a check
  that a reported counterexample is a genuine path of the model (every step is
  an enabled transition), and the hook for turning a counterexample into a
  regression test that drives the real objects.

Pure formatting + re-execution; no I/O, no graphviz dependency (DOT is text).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, TypeVar

from app.verification.modelcheck.spec import Spec
from app.verification.modelcheck.trace import Lasso, Trace

if TYPE_CHECKING:
    from app.verification.modelcheck.engine import StateGraph

StateT = TypeVar("StateT")

__all__ = ["replay", "to_dot"]


def _esc(text: str) -> str:
    """Escape a label for a DOT double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def to_dot(
    graph: StateGraph[StateT],
    *,
    label: Callable[[StateT], str] | None = None,
    highlight: Trace[StateT] | Lasso[StateT] | None = None,
    name: str = "model",
    max_nodes: int = 500,
) -> str:
    """Render ``graph`` as Graphviz DOT.

    ``label`` names the nodes (defaults to ``repr``). ``highlight`` colours the
    states and edges of a counterexample. ``max_nodes`` truncates huge graphs so
    the output stays paste-able (a note is emitted when truncated).
    """
    fmt = label or (lambda s: repr(s))
    ids: dict[StateT, str] = {}

    def node_id(state: StateT) -> str:
        if state not in ids:
            ids[state] = f"n{len(ids)}"
        return ids[state]

    hi_states, hi_edges = _highlight_sets(highlight)

    lines = [f"digraph {name} {{", "  rankdir=LR;", '  node [shape=box, fontname="monospace"];']
    initial = set(graph.initial)

    truncated = False
    count = 0
    for state in graph.edges:
        if count >= max_nodes:
            truncated = True
            break
        count += 1
        attrs = [f'label="{_esc(fmt(state))}"']
        if state in initial:
            attrs.append("style=bold")
            attrs.append("peripheries=2")
        if state in hi_states:
            attrs.append('color="red"')
            attrs.append('fontcolor="red"')
        lines.append(f"  {node_id(state)} [{', '.join(attrs)}];")

    rendered = set(list(graph.edges)[:count])
    for state in rendered:
        for action, succ in graph.edges.get(state, []):
            if succ not in rendered:
                continue
            eattrs = [f'label="{_esc(action)}"']
            if (state, action, succ) in hi_edges:
                eattrs.append('color="red"')
                eattrs.append("penwidth=2")
            lines.append(f"  {node_id(state)} -> {node_id(succ)} [{', '.join(eattrs)}];")

    if truncated:
        lines.append(f'  _trunc [label="… {graph.size - count} more states", shape=plaintext];')
    lines.append("}")
    return "\n".join(lines)


def _highlight_sets(
    cex: Trace[StateT] | Lasso[StateT] | None,
) -> tuple[set[StateT], set[tuple[StateT, str, StateT]]]:
    states: set[StateT] = set()
    edges: set[tuple[StateT, str, StateT]] = set()
    if cex is None:
        return states, edges
    steps = list(cex.steps) if isinstance(cex, Trace) else [*cex.stem, *cex.loop]
    prev = None
    for step in steps:
        states.add(step.state)
        if prev is not None and step.action is not None:
            edges.add((prev, step.action, step.state))
        prev = step.state
    return states, edges


def replay(
    spec: Spec[StateT], actions: Sequence[str]
) -> Iterator[tuple[str, StateT]]:
    """Replay an action sequence against ``spec``'s transition relation.

    Starting from each initial state, follow ``actions`` step by step. Yields the
    ``(action, reached_state)`` of each successful step. Raises
    :class:`ValueError` if an action is not enabled / does not exist at the
    current state, so a malformed counterexample is caught loudly. When an action
    is non-deterministic, the first successor labelled with it is taken (traces
    from this engine are deterministic, so this reproduces them exactly).
    """
    by_name = {a.name: a for a in spec.actions}
    for init in spec.initial:
        state = init
        try:
            for name in actions:
                action = by_name.get(name)
                if action is None:
                    raise ValueError(f"unknown action {name!r}")
                if not action.enabled(state):
                    raise ValueError(f"action {name!r} not enabled in the reached state")
                succ = next(iter(action.successors(state)))
                state = succ
                yield name, state
            return  # this initial state replayed the whole sequence
        except ValueError:
            continue  # try the next initial state
    raise ValueError("no initial state replays the given action sequence")
