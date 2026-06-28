"""Cross-volume continuity — does a beat contradict a *prior volume*'s canon (§7.2, §8.5).

Per-book continuity (the Continuity Supervisor, §7.2) guards a single volume. At
series scale the danger is a Volume-3 beat that contradicts an established
Volume-1 fact (a character who died returns with no explanation; a destroyed city
stands again). This module detects those cross-volume contradictions against a
simple *prior-fact ledger* and raises a
:class:`~app.agents.contracts.CrossVolumeConflict` citing the offending earlier
volume — the cross-book counterpart of the §7.2 :class:`ConflictObject`.

The ledger is a list of typed prior facts (subject, predicate, object, the volume
they were established in, and whether they were later retired — §8.5 forgetting).
A *retired* fact does not constrain forward generation, mirroring per-book
forgetting; only *active* prior facts can be contradicted. All pure.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.agents.contracts import CrossVolumeConflict


@dataclass(frozen=True, slots=True)
class PriorFact:
    """An established fact from an earlier volume (the cross-volume ledger row).

    ``predicate``/``object_value`` are the canon assertion (e.g. ``status`` =
    ``deceased``); ``established_volume`` is where it became true; ``retired`` marks
    a fact that a later, in-story event superseded (§8.5) — a retired fact no
    longer constrains forward generation.
    """

    subject_entity_key: str
    predicate: str
    object_value: str
    established_volume: int
    retired: bool = False


@dataclass(frozen=True, slots=True)
class ProposedFact:
    """A fact a proposed beat would depict (what we check against the ledger)."""

    subject_entity_key: str
    predicate: str
    object_value: str
    volume_index: int
    beat_index: int = 0


def _conflicts(prior: PriorFact, proposed: ProposedFact) -> bool:
    """True iff the proposed fact contradicts an active prior fact.

    Same subject + same predicate + a *different* object value, where the prior
    fact is active and was established in an *earlier* volume. (A same-volume or
    later prior is not a cross-volume contradiction; an identical object value is
    consistent, not contradictory.)
    """
    if prior.retired:
        return False
    if prior.established_volume >= proposed.volume_index:
        return False
    return (
        prior.subject_entity_key == proposed.subject_entity_key
        and prior.predicate == proposed.predicate
        and prior.object_value != proposed.object_value
    )


def detect_cross_volume_conflict(
    proposed: ProposedFact,
    ledger: Iterable[PriorFact],
    *,
    conflict_id: str,
) -> CrossVolumeConflict | None:
    """Return a :class:`CrossVolumeConflict` if the beat violates a prior volume (§7.2).

    Checks every active prior fact; on the first contradiction returns a structured
    conflict citing the earliest offending volume. Returns ``None`` when clean.
    """
    hits = [pf for pf in ledger if _conflicts(pf, proposed)]
    if not hits:
        return None
    # Cite the earliest-established contradicted fact (the most foundational).
    pf = min(hits, key=lambda f: f.established_volume)
    return CrossVolumeConflict(
        conflict_id=conflict_id,
        subject_entity_key=proposed.subject_entity_key,
        claim=f"{proposed.subject_entity_key} {proposed.predicate}={proposed.object_value}",
        prior_fact=f"{pf.subject_entity_key} {pf.predicate}={pf.object_value} "
        f"(established in volume {pf.established_volume})",
        prior_volume_index=pf.established_volume,
        current_volume_index=proposed.volume_index,
        current_beat_index=proposed.beat_index,
    )


def scan_cross_volume(
    proposed: Iterable[ProposedFact],
    ledger: Iterable[PriorFact],
    *,
    conflict_prefix: str = "xvc",
) -> list[CrossVolumeConflict]:
    """Scan a batch of proposed facts; return every cross-volume conflict found."""
    ledger_list = list(ledger)
    out: list[CrossVolumeConflict] = []
    for i, pf in enumerate(proposed):
        conflict = detect_cross_volume_conflict(
            pf, ledger_list, conflict_id=f"{conflict_prefix}_{i:04d}"
        )
        if conflict is not None:
            out.append(conflict)
    return out


def active_prior_facts(ledger: Iterable[PriorFact], *, before_volume: int) -> list[PriorFact]:
    """The active (non-retired) facts established before a volume (§8.5 scoping)."""
    return [
        pf
        for pf in ledger
        if not pf.retired and pf.established_volume < before_volume
    ]


__all__ = [
    "PriorFact",
    "ProposedFact",
    "active_prior_facts",
    "detect_cross_volume_conflict",
    "scan_cross_volume",
]
