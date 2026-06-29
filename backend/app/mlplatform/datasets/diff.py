"""Structural diffing between two dataset versions.

Where :mod:`app.mlplatform.datasets.drift` answers "did the *distribution* shift",
this module answers "which *examples* changed". It is the audit trail behind a
new version: what was added, what was dropped, and which examples kept their id
but changed content (a re-scrub, a new director edit, a re-label).

Examples are matched by ``id`` (stable across re-ingest because the id is a hash
of the trace identity). For matched ids, content equality is decided by
``content_hash`` (semantic, provenance-free), and when they differ the diff
records *which* fields changed — the field-level delta the lineage view shows.

Pure, JSON-able, O(n) on dict lookups.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.mlplatform.datasets.contracts import Dataset, TraceExample, canonical_json


def _comparable_fields(ex: TraceExample) -> dict[str, object]:
    """The semantic fields the diff compares for a changed example."""
    return {
        "role": ex.role.value,
        "task": ex.task.value,
        "prompt_key": ex.prompt_key,
        "prompt_version": ex.prompt_version,
        "model": ex.model,
        "input": canonical_json(dict(ex.input)),
        "output": ex.output,
        "qa": canonical_json(ex.qa.to_dict()) if ex.qa else None,
        "director_edits": canonical_json([e.to_dict() for e in ex.director_edits]),
        "reward": ex.reward,
        "labels": canonical_json(dict(ex.labels)),
        "split": ex.split.value,
        "scrubbed": ex.scrubbed,
    }


@dataclass(frozen=True, slots=True)
class ChangedExample:
    """An example present in both versions whose content changed."""

    id: str
    changed_fields: tuple[str, ...]
    before: Mapping[str, object] = field(default_factory=dict)
    after: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "changed_fields": list(self.changed_fields),
            "before": dict(self.before),
            "after": dict(self.after),
        }


@dataclass(frozen=True, slots=True)
class DatasetDiff:
    """The structural delta between a base and a target dataset."""

    base: str
    target: str
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    changed: tuple[ChangedExample, ...]
    unchanged_count: int

    @property
    def added(self) -> int:
        return len(self.added_ids)

    @property
    def removed(self) -> int:
        return len(self.removed_ids)

    @property
    def changed_count(self) -> int:
        return len(self.changed)

    @property
    def is_identical(self) -> bool:
        return not self.added_ids and not self.removed_ids and not self.changed

    def summary(self) -> dict[str, int | bool]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed_count,
            "unchanged": self.unchanged_count,
            "identical": self.is_identical,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "base": self.base,
            "target": self.target,
            "summary": self.summary(),
            "added_ids": list(self.added_ids),
            "removed_ids": list(self.removed_ids),
            "changed": [c.to_dict() for c in self.changed],
        }


def diff_datasets(base: Dataset, target: Dataset) -> DatasetDiff:
    """Compute the added / removed / changed / unchanged delta base → target."""
    base_by_id = {e.id: e for e in base.examples}
    target_by_id = {e.id: e for e in target.examples}

    base_ids = set(base_by_id)
    target_ids = set(target_by_id)

    added = tuple(sorted(target_ids - base_ids))
    removed = tuple(sorted(base_ids - target_ids))

    changed: list[ChangedExample] = []
    unchanged = 0
    for ex_id in sorted(base_ids & target_ids):
        be, te = base_by_id[ex_id], target_by_id[ex_id]
        bf, tf = _comparable_fields(be), _comparable_fields(te)
        fields = tuple(k for k in bf if bf[k] != tf[k])
        # ``content_hash`` is provenance-/split-free, so it can match while the
        # split assignment changed; the field-level compare is the source of truth.
        if not fields:
            unchanged += 1
            continue
        changed.append(
            ChangedExample(
                id=ex_id,
                changed_fields=fields,
                before={k: bf[k] for k in fields},
                after={k: tf[k] for k in fields},
            )
        )

    return DatasetDiff(
        base=base.name,
        target=target.name,
        added_ids=added,
        removed_ids=removed,
        changed=tuple(changed),
        unchanged_count=unchanged,
    )


__all__ = ["ChangedExample", "DatasetDiff", "diff_datasets"]
