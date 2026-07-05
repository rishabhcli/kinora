"""Bitemporal canon export — the inspectable bible for the 4-D canon (kinora.md §8.1).

The existing :class:`app.memory.canon_vault.CanonVault` renders the uni-temporal entity graph
to readable markdown. This is its bitemporal counterpart: it renders the **two-axis**
canon — every branch, every fact's transaction-time history (what the system believed and
when), and the tamper-evident audit trail — so the "as of any past write" and FORK/MERGE
story is *visible*, not just queryable.

Pure rendering over already-fetched rows (the services do the reads); the markdown is
returned for the frontend's canon inspector and can be persisted by the caller via the
shared object store. It does not modify the existing vault.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.memory.contracts import AuditChain, BitemporalFact, BranchInfo, FactHistory


def _cell(text: str | None) -> str:
    """Make a string safe for a markdown table cell."""
    return (text or "").replace("\n", " ").replace("|", r"\|")


def _beat_to(fact: BitemporalFact) -> str:
    return "—" if fact.valid.valid_to_beat is None else str(fact.valid.valid_to_beat)


def _tx_to(fact: BitemporalFact) -> str:
    return "current" if fact.tx.tx_to is None else fact.tx.tx_to.isoformat()


class BitemporalVaultDoc(BaseModel):
    """The rendered bitemporal canon document (markdown sections + the whole join)."""

    book_id: str
    branch: str
    sections: dict[str, str] = Field(default_factory=dict)

    @property
    def markdown(self) -> str:
        return "\n\n".join(self.sections.values())


class BitemporalVault:
    """Render the bitemporal canon to inspectable markdown (branches, history, audit)."""

    def render(
        self,
        *,
        book_id: str,
        branch: str,
        facts: list[BitemporalFact],
        branches: list[BranchInfo],
        histories: list[FactHistory],
        audit: AuditChain,
    ) -> BitemporalVaultDoc:
        sections = {
            "active": self._render_active(branch, facts),
            "branches": self._render_branches(branches),
            "history": self._render_histories(histories),
            "audit": self._render_audit(audit),
        }
        return BitemporalVaultDoc(book_id=book_id, branch=branch, sections=sections)

    def _render_active(self, branch: str, facts: list[BitemporalFact]) -> str:
        lines = [f"# Active canon facts — branch `{branch}`", ""]
        lines.append("| subject | predicate | object | from_beat | to_beat | believed_since |")
        lines.append("|---|---|---|---|---|---|")
        for f in sorted(facts, key=lambda x: (x.subject_entity_key, x.predicate)):
            lines.append(
                f"| {_cell(f.subject_entity_key)} | {_cell(f.predicate)} "
                f"| {_cell(f.object_value)} | {f.valid.valid_from_beat} | {_beat_to(f)} "
                f"| {f.tx.tx_from.isoformat()} |"
            )
        lines.append("")
        return "\n".join(lines)

    def _render_branches(self, branches: list[BranchInfo]) -> str:
        lines = ["# Branches", "", "| name | parent | status | base_beat |", "|---|---|---|---|"]
        for b in branches:
            base = "—" if b.base_beat is None else str(b.base_beat)
            lines.append(
                f"| {_cell(b.name)} | {_cell(b.parent)} | {_cell(b.status)} | {base} |"
            )
        if not branches:
            lines.append("| main | — | open | — |")
        lines.append("")
        return "\n".join(lines)

    def _render_histories(self, histories: list[FactHistory]) -> str:
        lines = ["# Fact transaction-time histories", ""]
        for h in histories:
            if not h.beliefs:
                continue
            lines.append(f"## `{h.fact_key}`")
            lines.append("")
            lines.append("| object | from_beat | to_beat | believed_from | believed_to |")
            lines.append("|---|---|---|---|---|")
            for b in h.beliefs:
                lines.append(
                    f"| {_cell(b.object_value)} | {b.valid.valid_from_beat} | {_beat_to(b)} "
                    f"| {b.tx.tx_from.isoformat()} | {_tx_to(b)} |"
                )
            lines.append("")
        return "\n".join(lines)

    def _render_audit(self, audit: AuditChain) -> str:
        status = "intact ✓" if audit.intact else f"BROKEN at seq {audit.broken_at_seq} ✗"
        lines = [
            f"# Audit log ({status})",
            "",
            "| seq | action | actor | target |",
            "|---|---|---|---|",
        ]
        for e in audit.entries:
            lines.append(
                f"| {e.seq} | {_cell(e.action)} | {_cell(e.actor_id)} | {_cell(e.target_key)} |"
            )
        lines.append("")
        return "\n".join(lines)


__all__ = ["BitemporalVault", "BitemporalVaultDoc"]
