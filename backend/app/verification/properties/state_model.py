"""An independent reference model of the §9.7 per-shot state machine.

For stateful / model-based testing, the rule is: never check the system against
*itself*. So this module re-derives the §9.7 legal-edge relation **from the
diagram in kinora.md**, written out by hand here, and the stateful test asserts
that :class:`app.render.states.ShotStateMachine` agrees with this model on every
generated command sequence. If the production transition table drifts from the
documented diagram (or vice-versa), the two disagree and the test fails — which is
the whole point of a reference model.

The §9.7 diagram (verbatim, from kinora.md):

    [*]       -> Planned
    Planned   -> Keyframed | Promoted
    Keyframed -> Promoted
    Promoted  -> CacheCheck
    CacheCheck-> Accepted | Rendering
    Rendering -> QA | Degraded
    QA        -> Accepted | Repair
    Repair    -> Rendering | Conflict | Degraded
    Conflict  -> Rendering | Accepted | Degraded | Conflict
    Accepted  -> [*]
    Degraded  -> [*]

This model is deliberately tiny and dependency-free: it imports only the
``RenderState`` enum (to share the symbol set) and otherwise stands alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.render.states import RenderState as S

#: The §9.7 legal edges, transcribed independently from the kinora.md diagram.
#: (Compared against ``app.render.states.ALLOWED_TRANSITIONS`` by the suite.)
REFERENCE_EDGES: dict[S, frozenset[S]] = {
    S.PLANNED: frozenset({S.KEYFRAMED, S.PROMOTED}),
    S.KEYFRAMED: frozenset({S.PROMOTED}),
    S.PROMOTED: frozenset({S.CACHE_CHECK}),
    S.CACHE_CHECK: frozenset({S.ACCEPTED, S.RENDERING}),
    S.RENDERING: frozenset({S.QA, S.DEGRADED}),
    S.QA: frozenset({S.ACCEPTED, S.REPAIR}),
    S.REPAIR: frozenset({S.RENDERING, S.CONFLICT, S.DEGRADED}),
    S.CONFLICT: frozenset({S.RENDERING, S.ACCEPTED, S.DEGRADED, S.CONFLICT}),
    S.ACCEPTED: frozenset(),
    S.DEGRADED: frozenset(),
}

#: Terminal sinks (no outgoing edges), transcribed independently.
REFERENCE_TERMINAL: frozenset[S] = frozenset({S.ACCEPTED, S.DEGRADED})


def ref_is_allowed(src: S, dst: S) -> bool:
    """Whether ``src -> dst`` is a legal edge per the reference diagram."""
    return dst in REFERENCE_EDGES.get(src, frozenset())


@dataclass(slots=True)
class ReferenceShot:
    """A minimal model shot: just the current state + the path it walked.

    Mirrors :class:`ShotStateMachine`'s *observable* contract — a no-op self-edge
    is allowed (and not recorded), an illegal edge is rejected, and a legal edge
    records one history entry — without sharing any of its code.
    """

    state: S = S.PLANNED
    history: list[tuple[S, S]] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.state in REFERENCE_TERMINAL

    def can_step(self, dst: S) -> bool:
        """True if stepping to ``dst`` would be accepted (legal edge or a no-op)."""
        return dst == self.state or ref_is_allowed(self.state, dst)

    def step(self, dst: S) -> S:
        """Apply ``dst`` if legal; raise ``ValueError`` on an illegal edge.

        A no-op self-edge (``dst == state``) is a legal idempotent move that does
        **not** append to history — matching the production machine exactly.
        """
        if dst == self.state:
            return self.state
        if not ref_is_allowed(self.state, dst):
            raise ValueError(f"illegal {self.state} -> {dst}")
        src = self.state
        self.state = dst
        self.history.append((src, dst))
        return dst


def reachable_states(start: S = S.PLANNED) -> frozenset[S]:
    """All states reachable from ``start`` under the reference edges (BFS)."""
    seen: set[S] = {start}
    frontier = [start]
    while frontier:
        node = frontier.pop()
        for nxt in REFERENCE_EDGES.get(node, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)


def terminal_is_reachable_from_every_state() -> bool:
    """Liveness: a terminal sink is reachable from every non-terminal state.

    The §9.7 machine must never be able to wedge in a state from which no sink is
    reachable — otherwise a shot could spin forever. (The model checks the
    *structure*; the simulator separately proves the live loop always terminates.)
    """
    for state in REFERENCE_EDGES:
        if state in REFERENCE_TERMINAL:
            continue
        if not (reachable_states(state) & REFERENCE_TERMINAL):
            return False
    return True


__all__ = [
    "REFERENCE_EDGES",
    "REFERENCE_TERMINAL",
    "ReferenceShot",
    "reachable_states",
    "ref_is_allowed",
    "terminal_is_reachable_from_every_state",
]
