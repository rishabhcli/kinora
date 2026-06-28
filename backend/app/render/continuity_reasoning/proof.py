"""Human-readable PROOF TRACES for continuity reasoning (§7.2, §13).

A continuity verdict is only useful to a director (and a judge, §14) if it can
*show its work*: not "this contradicts the canon" but the exact chain of facts
and temporal relations that forces the conclusion. §7.2 makes conflicts
first-class structured objects so they are "inspectable, loggable, and
arbitrated"; a proof trace is the inspectable *derivation* behind one.

These structures are pure data — the reasoners build them, the conflict layer
renders them into the ``claim``/``canon_fact`` strings of a §7.2
:class:`ConflictObject`, and tests assert on their shape. Each
:class:`ProofStep` is one inference (a premise pair + the rule that fired +
the conclusion); a :class:`ProofTrace` is the ordered chain plus a one-line
``summary`` suitable for an agent-activity feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Rule(StrEnum):
    """The named inference rules the engine can cite in a proof step."""

    #: Two facts on one functional channel overlap in time with different objects.
    FUNCTIONAL_CONFLICT = "functional_conflict"
    #: A proposed depiction asserts a value the active canon fact forbids.
    PROPOSED_VS_ACTIVE = "proposed_vs_active"
    #: A fact's interval was closed (retired) before the queried beat (§8.5).
    RETIRED_BEFORE_BEAT = "retired_before_beat"
    #: A fact had not yet begun at the queried beat (asserted in the future).
    NOT_YET_ACTIVE = "not_yet_active"
    #: Transitive closure over entity-valued facts (multi-hop).
    TRANSITIVE = "transitive"
    #: Co-location: two subjects in the same place at the same beat.
    COLOCATION = "colocation"
    #: A depicted fact is canon-true but reader-unknown at this beat (spoiler).
    EPISTEMIC_SPOILER = "epistemic_spoiler"
    #: A reader belief contradicts canon truth (dramatic irony).
    READER_MISBELIEF = "reader_misbelief"
    #: Allen temporal relation cited as a premise.
    TEMPORAL_RELATION = "temporal_relation"


@dataclass(frozen=True, slots=True)
class ProofStep:
    """One inference: premises + the rule that fired → a conclusion (all prose)."""

    rule: Rule
    premises: tuple[str, ...]
    conclusion: str

    def render(self) -> str:
        """Render the step as ``premises ⟹[rule] conclusion``."""
        prem = " ∧ ".join(self.premises) if self.premises else "∅"
        return f"{prem}  ⟹[{self.rule.value}]  {self.conclusion}"


@dataclass(frozen=True, slots=True)
class ProofTrace:
    """An ordered chain of :class:`ProofStep`s with a one-line summary.

    ``contradiction`` flags whether the chain *establishes* a contradiction (vs.
    a derivation that merely infers a new fact). ``cited_fact_ids`` lets the
    conflict layer point the §7.2 ``contradicting_state_id`` at the right canon
    fact, and lets the UI highlight the offending node.
    """

    summary: str
    steps: tuple[ProofStep, ...] = field(default_factory=tuple)
    contradiction: bool = False
    cited_fact_ids: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """A multi-line rendering: summary header then numbered steps."""
        header = f"{'CONTRADICTION' if self.contradiction else 'DERIVATION'}: {self.summary}"
        lines = [header]
        for i, step in enumerate(self.steps, start=1):
            lines.append(f"  {i}. {step.render()}")
        return "\n".join(lines)

    def with_step(self, step: ProofStep) -> ProofTrace:
        """Return a copy with ``step`` appended (immutable builder helper)."""
        return ProofTrace(
            summary=self.summary,
            steps=(*self.steps, step),
            contradiction=self.contradiction,
            cited_fact_ids=self.cited_fact_ids,
        )

    def __str__(self) -> str:
        return self.render()


__all__ = ["ProofStep", "ProofTrace", "Rule"]
