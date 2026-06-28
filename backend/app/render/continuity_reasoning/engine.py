"""The ContinuityEngine façade — one entry point over the pure reasoners (§7.2).

The Continuity Supervisor agent (``app.agents.continuity``) wants a single,
pure call: "given the active canon at this beat and what the shot implies, is it
continuity-clean, and if not, why (a proof trace) and which canon fact is to
blame?" This façade composes the temporal core, the contradiction detector, the
epistemic spoiler check, the spatial check, and (optionally) multi-hop closure
into that one call, and rolls the findings up into a single
:class:`ContinuityVerdict`.

It stays **pure**: it builds a :class:`~.timeline.CanonTimeline` from the
already-fetched canon slice and reasons offline. The model's job is upstream —
to turn shot prose into the :class:`~.facts.FactQuery` claims this engine then
*proves* against the canon. The agent renders the verdict into a §7.2
:class:`ConflictObject`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .composition import RelationSet
from .constraints import AllenNetwork
from .contradiction import Contradiction, check_proposed_fact, detect_canon_contradictions
from .epistemic import SpoilerRisk, check_spoiler
from .facts import Fact, FactQuery, StateLike
from .inference import multi_hop_closure
from .proof import ProofTrace
from .spatial import detect_spatial_conflicts
from .timeline import CanonTimeline


class FindingKind(StrEnum):
    """The category of a continuity finding (drives the §7.2 conflict type)."""

    CONTRADICTION = "contradiction"
    SPATIAL = "spatial"
    SPOILER = "spoiler"
    #: An *implied* temporal contradiction — a cycle/over-constraint found only
    #: by composing pairwise relations (the Allen constraint network).
    TEMPORAL = "temporal"


@dataclass(frozen=True, slots=True)
class ContinuityFinding:
    """One problem the engine derived, with its proof trace + cited canon fact."""

    kind: FindingKind
    summary: str
    trace: ProofTrace
    cited_fact_id: str | None = None
    at_beat: int | None = None


@dataclass(frozen=True, slots=True)
class ContinuityVerdict:
    """The engine's roll-up: clean or a list of findings (worst-first)."""

    ok: bool
    findings: tuple[ContinuityFinding, ...] = field(default_factory=tuple)

    @property
    def primary(self) -> ContinuityFinding | None:
        """The first (highest-priority) finding, or ``None`` if clean."""
        return self.findings[0] if self.findings else None

    def proof_text(self) -> str:
        """All findings' proof traces concatenated (for logs / the agent feed)."""
        return "\n\n".join(f.trace.render() for f in self.findings)


class ContinuityEngine:
    """Composes the pure reasoners into one verdict over a canon slice (§7.2)."""

    def __init__(self, timeline: CanonTimeline) -> None:
        self._timeline = timeline

    @classmethod
    def from_state_slices(
        cls,
        states: Sequence[StateLike],
        *,
        hidden_state_ids: Sequence[str] = (),
        revealed_at: dict[str, int] | None = None,
    ) -> ContinuityEngine:
        """Build directly from a memory ``canon.query`` active-state list."""
        return cls(
            CanonTimeline.from_state_slices(
                states, hidden_state_ids=hidden_state_ids, revealed_at=revealed_at
            )
        )

    @property
    def timeline(self) -> CanonTimeline:
        return self._timeline

    # --- the live shot check --------------------------------------------- #

    def check_shot_claims(
        self,
        claims: Sequence[FactQuery],
        *,
        check_spoilers: bool = True,
    ) -> ContinuityVerdict:
        """Prove a shot's implied facts against the canon at their beats.

        ``claims`` is what the model extracted the shot to *assert* (e.g. "the
        hero draws a sword" ⇒ ``possesses[weapon] sword @ beat 39``). Each is
        tested for a hard contradiction; if requested, each is also tested for a
        spoiler (depicting a reader-unknown reveal). Returns a verdict with one
        finding per problem, contradictions before spoilers.
        """
        contradiction_findings: list[ContinuityFinding] = []
        spoiler_findings: list[ContinuityFinding] = []
        for claim in claims:
            contradiction = check_proposed_fact(self._timeline, claim)
            if contradiction is not None:
                contradiction_findings.append(
                    _finding_from_contradiction(contradiction, FindingKind.CONTRADICTION)
                )
                continue  # one problem per claim is enough to route a repair
            if check_spoilers:
                risk = check_spoiler(self._timeline, claim)
                if risk is not None:
                    spoiler_findings.append(_finding_from_spoiler(risk))
        findings = (*contradiction_findings, *spoiler_findings)
        return ContinuityVerdict(ok=not findings, findings=findings)

    # --- whole-canon audits ---------------------------------------------- #

    def audit_canon(self) -> ContinuityVerdict:
        """Self-consistency audit of the whole canon (functional + spatial).

        Used by ingest / canon-edit to catch a mis-asserted canon *before* it
        poisons forward generation — the §8.5 invariant, proven.
        """
        findings: list[ContinuityFinding] = []
        for c in detect_canon_contradictions(self._timeline):
            findings.append(_finding_from_contradiction(c, FindingKind.CONTRADICTION))
        for s in detect_spatial_conflicts(self._timeline):
            findings.append(
                ContinuityFinding(
                    kind=FindingKind.SPATIAL,
                    summary=s.trace.summary,
                    trace=s.trace,
                    cited_fact_id=(s.trace.cited_fact_ids[0] if s.trace.cited_fact_ids else None),
                    at_beat=s.beat,
                )
            )
        findings.sort(key=lambda f: (f.at_beat if f.at_beat is not None else 0, f.kind.value))
        return ContinuityVerdict(ok=not findings, findings=tuple(findings))

    def audit_temporal_consistency(
        self,
        ordering_constraints: list[tuple[str, str, RelationSet]] | None = None,
    ) -> ContinuityVerdict:
        """Detect *implied* temporal contradictions via the Allen network (§8.5).

        Builds a constraint network from the canon's fact intervals (keyed by
        fact id) and any externally-asserted qualitative ``ordering_constraints``
        (``(fact_id_i, fact_id_j, relations)`` — e.g. an author saying "the duel
        is BEFORE the funeral" without exact beats). Path-consistency then
        propagates: if the constraints cannot be jointly satisfied (a cycle /
        over-constraint), the collapsed triangle is returned as a finding with a
        proof trace. A consistent network yields a clean verdict.
        """
        intervals = {
            (f.fact_id or f"{f.subject}.{f.predicate}:{f.object}"): f.interval
            for f in self._timeline.facts
            if f.fact_id
        }
        net = AllenNetwork.from_intervals(intervals)
        for i, j, relations in ordering_constraints or []:
            net.constrain(i, j, relations)
        result = net.path_consistency()
        if result.consistent or result.trace is None:
            return ContinuityVerdict(ok=True)
        finding = ContinuityFinding(
            kind=FindingKind.TEMPORAL,
            summary=result.trace.summary,
            trace=result.trace,
            cited_fact_id=None,
            at_beat=None,
        )
        return ContinuityVerdict(ok=False, findings=(finding,))

    def inferred_facts_at(self, beat: int) -> tuple[Fact, ...]:
        """The multi-hop-closed inferred facts at ``beat`` (carried/accompanied)."""
        return tuple(i.fact for i in multi_hop_closure(self._timeline, beat))

    def with_inference_at(self, beat: int) -> ContinuityEngine:
        """An engine whose timeline includes the inferred facts at ``beat``.

        Lets :meth:`check_shot_claims` catch contradictions that hold only by
        composition (a prop in the wrong place because its carrier is).
        """
        extra = [i.fact for i in multi_hop_closure(self._timeline, beat)]
        return ContinuityEngine(self._timeline.with_facts(extra))


def _finding_from_contradiction(c: Contradiction, kind: FindingKind) -> ContinuityFinding:
    return ContinuityFinding(
        kind=kind,
        summary=c.trace.summary,
        trace=c.trace,
        cited_fact_id=c.cited_fact_id or None,
        at_beat=c.beat,
    )


def _finding_from_spoiler(risk: SpoilerRisk) -> ContinuityFinding:
    return ContinuityFinding(
        kind=FindingKind.SPOILER,
        summary=risk.trace.summary,
        trace=risk.trace,
        cited_fact_id=(risk.fact.fact_id or None),
        at_beat=risk.beat,
    )


__all__ = [
    "ContinuityEngine",
    "ContinuityFinding",
    "ContinuityVerdict",
    "FindingKind",
]
