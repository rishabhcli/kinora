"""The formal continuity-reasoning engine (§7.2, §8.5, §10).

A pure, network-free temporal reasoner over the versioned canon. The Continuity
Supervisor (``app.agents.continuity``) uses it to *derive* — not merely judge —
continuity contradictions, with human-readable proof traces, and the §7.2
conflict layer (``app.render.conflict``) renders those traces into structured
conflicts.

Subsystems (each a pure, exhaustively-tested module):

* :mod:`.intervals` — Allen interval algebra on the beat axis.
* :mod:`.facts` — the versioned-fact model + epistemic visibility.
* :mod:`.timeline` — an indexed, queryable temporal model of the canon.
* :mod:`.proof` — human-readable proof traces.
* :mod:`.contradiction` — automatic contradiction detection.
* :mod:`.propagation` — state-change propagation across dependent facts.
* :mod:`.epistemic` — reader-knowledge tracking (known vs. canon-true).
* :mod:`.spatial` — spatial continuity + prop/wardrobe persistence.
* :mod:`.inference` — multi-hop transitive inference.
* :mod:`.engine` — the façade the agent calls.
"""

from __future__ import annotations

from .belief import BeliefRevision, BeliefState, ReaderBelief
from .composition import ALL_RELATIONS, RelationSet, compose, converse
from .constraints import AllenNetwork, ConsistencyResult
from .contradiction import (
    Contradiction,
    check_proposed_fact,
    detect_canon_contradictions,
)
from .engine import ContinuityEngine, ContinuityFinding, ContinuityVerdict
from .epistemic import EpistemicReport, SpoilerRisk, reader_knowledge_at, spoiler_risks
from .facts import (
    FUNCTIONAL_PREDICATES,
    Fact,
    FactQuery,
    Visibility,
    fact_from_state_slice,
    fact_slot,
)
from .inference import InferredFact, multi_hop_closure, transitive_location
from .intervals import Allen, BeatInterval, inverse
from .proof import ProofStep, ProofTrace, Rule
from .propagation import PropagationEffect, propagate_retirement
from .spatial import (
    SpatialConflict,
    WardrobeContinuity,
    detect_spatial_conflicts,
    prop_persistence_gaps,
)
from .timeline import CanonTimeline

__all__ = [
    "ALL_RELATIONS",
    "FUNCTIONAL_PREDICATES",
    "Allen",
    "AllenNetwork",
    "BeatInterval",
    "BeliefRevision",
    "BeliefState",
    "CanonTimeline",
    "ConsistencyResult",
    "Contradiction",
    "ContinuityEngine",
    "ContinuityFinding",
    "ContinuityVerdict",
    "EpistemicReport",
    "Fact",
    "FactQuery",
    "InferredFact",
    "PropagationEffect",
    "ProofStep",
    "ProofTrace",
    "ReaderBelief",
    "RelationSet",
    "Rule",
    "SpatialConflict",
    "SpoilerRisk",
    "Visibility",
    "WardrobeContinuity",
    "check_proposed_fact",
    "compose",
    "converse",
    "detect_canon_contradictions",
    "detect_spatial_conflicts",
    "fact_from_state_slice",
    "fact_slot",
    "inverse",
    "multi_hop_closure",
    "prop_persistence_gaps",
    "propagate_retirement",
    "reader_knowledge_at",
    "spoiler_risks",
    "transitive_location",
]
