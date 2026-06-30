"""Canon consistency auditor — detects drift + contradictions across the canon.

A long adaptation stays watchable only if the accumulated canon stays
*self-consistent* (kinora.md §8, §9.5). This auditor sweeps the whole canon —
entity revision logs (:mod:`.versioning`) and the active fact set — and reports:

* **fact contradictions** — two active facts that cannot co-hold (a character in
  two locations, alive *and* dead). Reuses the §9.5 timeline check
  (:func:`app.memory.graph_reasoning.find_contradictions`) so canon2 and the
  Critic flag the same thing.
* **appearance / style drift** — an entity whose ``appearance`` or
  ``style_tokens`` changed across revisions *without* a grounded reason (no
  source span / no provenance). Unexplained look-changes are the AI-slop the
  whole memory thesis exists to prevent, so they are surfaced even though each
  individual revision is internally valid.
* **dangling references** — a fact whose subject is not a known entity (a fact
  about someone the canon never introduced) and orphaned predicates.
* **unresolved conflicts** — anything still sitting in the flagged-conflict queue
  (:mod:`.conflict`) that arbitration has not closed.

Pure: it takes already-materialized snapshots and returns a :class:`AuditReport`.
No DB, no provider — so a planted contradiction is caught deterministically in
tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, Field

from app.memory.canon2.conflict import FlaggedConflict
from app.memory.canon2.versioning import REVISION_FIELDS, EntityHistory
from app.memory.contracts import BitemporalFact
from app.memory.graph_reasoning import find_contradictions


class Severity(StrEnum):
    """How load-bearing a finding is for consistency."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Finding(BaseModel):
    """One consistency issue the auditor surfaced."""

    #: "contradiction" | "drift" | "dangling_reference" | "unresolved_conflict"
    kind: str
    severity: Severity
    subject: str | None = None
    predicate: str | None = None
    message: str
    #: The fact_keys / entity_keys this finding implicates (for the canon editor).
    refs: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    """The full consistency sweep result for a book/branch."""

    book_id: str
    branch: str = "main"
    findings: list[Finding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no ERROR-severity findings (warnings/info are advisory)."""
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.ERROR)

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


#: The entity fields whose unexplained change between revisions is *drift* — the
#: visual canon a shot is conditioned on. A name/description change is prose and
#: not flagged as drift.
DRIFT_FIELDS: tuple[str, ...] = ("appearance", "style_tokens", "voice")


class ConsistencyAuditor:
    """Sweep the accumulated canon for drift + contradictions (§8, §9.5).

    All inputs are materialized snapshots so the audit is pure and deterministic.
    """

    def audit(
        self,
        *,
        book_id: str,
        branch: str = "main",
        facts: Iterable[BitemporalFact] = (),
        histories: Iterable[EntityHistory] = (),
        flagged: Iterable[FlaggedConflict] = (),
        mutually_exclusive: Iterable[tuple[str, str]] = (),
    ) -> AuditReport:
        facts = list(facts)
        histories = list(histories)
        findings: list[Finding] = []

        findings.extend(self._contradictions(facts, mutually_exclusive))
        findings.extend(self._drift(histories))
        findings.extend(self._dangling(facts, histories))
        findings.extend(self._unresolved(flagged))

        return AuditReport(book_id=book_id, branch=branch, findings=findings)

    # --- individual checks -------------------------------------------------- #

    def _contradictions(
        self,
        facts: list[BitemporalFact],
        mutually_exclusive: Iterable[tuple[str, str]],
    ) -> list[Finding]:
        out: list[Finding] = []
        for c in find_contradictions(facts, mutually_exclusive=mutually_exclusive):
            out.append(
                Finding(
                    kind="contradiction",
                    severity=Severity.ERROR,
                    subject=c.subject,
                    predicate=c.predicate,
                    message=(
                        f"{c.subject}.{c.predicate}: '{c.object_a}' vs '{c.object_b}' "
                        f"— {c.reason}"
                    ),
                    refs=[c.fact_key_a, c.fact_key_b],
                )
            )
        return out

    def _drift(self, histories: list[EntityHistory]) -> list[Finding]:
        """Flag unexplained changes to visual canon across an entity's revisions."""
        out: list[Finding] = []
        for hist in histories:
            revs = sorted(hist.revisions, key=lambda r: r.seq)
            for prev, cur in zip(revs, revs[1:], strict=False):
                grounded = bool(
                    cur.provenance.source_span or cur.provenance.reason
                )
                for field in DRIFT_FIELDS:
                    if field not in REVISION_FIELDS:
                        continue
                    old, new = getattr(prev, field), getattr(cur, field)
                    if old == new or (not old and not new):
                        continue
                    if grounded:
                        # An explained change is legitimate canon evolution.
                        continue
                    out.append(
                        Finding(
                            kind="drift",
                            severity=Severity.WARNING,
                            subject=hist.entity_key,
                            predicate=field,
                            message=(
                                f"{hist.entity_key}: '{field}' changed at version "
                                f"{cur.version} (beat {cur.valid_from_beat}) with no "
                                f"grounding (no source span / reason) — possible drift"
                            ),
                            refs=[hist.entity_key],
                        )
                    )
        return out

    def _dangling(
        self, facts: list[BitemporalFact], histories: list[EntityHistory]
    ) -> list[Finding]:
        """Facts about a subject the canon never introduced as an entity."""
        known = {h.entity_key for h in histories}
        if not known:
            # No entity registry supplied → can't judge danglers; skip silently.
            return []
        out: list[Finding] = []
        for f in facts:
            if f.subject_entity_key not in known:
                out.append(
                    Finding(
                        kind="dangling_reference",
                        severity=Severity.WARNING,
                        subject=f.subject_entity_key,
                        predicate=f.predicate,
                        message=(
                            f"fact '{f.fact_key}' references unknown subject "
                            f"'{f.subject_entity_key}' (no canon entity)"
                        ),
                        refs=[f.fact_key],
                    )
                )
        return out

    def _unresolved(self, flagged: Iterable[FlaggedConflict]) -> list[Finding]:
        out: list[Finding] = []
        for c in flagged:
            if c.resolved:
                continue
            out.append(
                Finding(
                    kind="unresolved_conflict",
                    severity=Severity.ERROR,
                    subject=c.subject,
                    predicate=c.predicate,
                    message=(
                        f"unresolved conflict {c.conflict_id}: {c.subject}.{c.predicate} "
                        f"'{c.incoming.object_value}' vs '{c.existing.object_value}' "
                        f"awaiting arbitration"
                    ),
                    refs=[c.conflict_id],
                )
            )
        return out


__all__ = [
    "DRIFT_FIELDS",
    "AuditReport",
    "ConsistencyAuditor",
    "Finding",
    "Severity",
]
