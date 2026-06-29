"""Export adapters — the serialization the sibling facets train on.

A dataset is the in-repo currency; an *export* is the on-the-wire shape a
training job consumes. This module turns a :class:`Dataset` (or one split of it)
into the two formats the alignment + serving facets asked for, shaped per task:

* **JSONL** (:class:`JSONLExporter`) — one JSON object per line, the universal
  fine-tuning interchange. Three shapes selectable by :class:`ExportShape`:
  - ``RECORD`` — the full :meth:`TraceExample.to_record` (lossless; the audit shape).
  - ``SFT`` — ``{"messages": [...], "completion": ...}`` style ``(prompt → output)``
    chat pairs for supervised fine-tuning; filters to QA-passed / good-labelled
    examples by default (imitate only the good behaviours).
  - ``PREFERENCE`` — ``{"prompt", "chosen", "rejected"}`` pairs built per group by
    pairing a high-reward output against a low-reward one (the reward facet's RM
    fuel). Falls back to ``{"prompt","output","reward"}`` point shape when a group
    has no contrasting pair.
* **Columnar** (:class:`ColumnarExporter`) — a column-oriented dict-of-arrays
  (the shape pandas/pyarrow/polars ingest with zero glue) plus a CSV rendering,
  for the analytics + feature-store consumers. Nested fields are JSON-encoded
  into their column so the frame stays rectangular.

All exporters are pure: they take a dataset and return ``str`` / ``bytes`` /
in-memory structures — *no filesystem writes* (the caller / API decides where
bytes land), which keeps the unit suite hermetic.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.mlplatform.datasets.contracts import Dataset, TaskType, TraceExample, canonical_json
from app.mlplatform.datasets.errors import ExportError


class ExportShape(StrEnum):
    """How an example is projected into an export row."""

    RECORD = "record"  #: lossless full record
    SFT = "sft"  #: supervised (prompt → completion) pairs
    PREFERENCE = "preference"  #: (prompt, chosen, rejected) preference pairs


# --------------------------------------------------------------------------- #
# Row shaping
# --------------------------------------------------------------------------- #


def _render_prompt(ex: TraceExample) -> str:
    """A stable textual prompt for an example (canonical JSON of its input)."""
    return canonical_json(dict(ex.input))


def _is_good(ex: TraceExample) -> bool:
    """Whether an example is a positive training target (QA pass / good label)."""
    if ex.labels.get("quality") == "good":
        return True
    if ex.qa is not None:
        return ex.qa.passed
    if ex.reward is not None:
        return ex.reward >= 0.7
    return not ex.director_edits


def sft_row(ex: TraceExample) -> dict[str, Any]:
    """A chat-style SFT row: a system-tagged prompt turn + the completion."""
    return {
        "id": ex.id,
        "role": ex.role.value,
        "messages": [
            {"role": "system", "tag": f"{ex.prompt_key}@{ex.prompt_version}"},
            {"role": "user", "content": _render_prompt(ex)},
        ],
        "completion": ex.output,
        "reward": ex.reward,
        "split": ex.split.value,
    }


def _preference_pairs(examples: Sequence[TraceExample]) -> list[dict[str, Any]]:
    """Build (prompt, chosen, rejected) pairs by contrasting reward within a group.

    Examples are grouped by ``(prompt_key, group_key)`` — same task on the same
    book — then the best (highest reward / QA-passed) is paired against the worst
    when they differ. A group with no contrast falls back to a point row.
    """
    groups: dict[tuple[str, str], list[TraceExample]] = {}
    for ex in examples:
        groups.setdefault((ex.prompt_key, ex.group_key), []).append(ex)

    def _r(ex: TraceExample) -> float:
        if ex.reward is not None:
            return ex.reward
        if ex.qa is not None:
            return 1.0 if ex.qa.passed else 0.0
        return 0.5

    rows: list[dict[str, Any]] = []
    for (prompt_key, group), members in groups.items():
        ranked = sorted(members, key=_r)
        worst, best = ranked[0], ranked[-1]
        if best is not worst and _r(best) > _r(worst):
            rows.append(
                {
                    "prompt_key": prompt_key,
                    "group": group,
                    "prompt": _render_prompt(best),
                    "chosen": best.output,
                    "rejected": worst.output,
                    "margin": round(_r(best) - _r(worst), 6),
                }
            )
        else:
            ex = best
            rows.append(
                {
                    "prompt_key": prompt_key,
                    "group": group,
                    "prompt": _render_prompt(ex),
                    "output": ex.output,
                    "reward": _r(ex),
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# JSONL
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JSONLExporter:
    """Serialize a dataset to JSON-lines in a chosen shape."""

    shape: ExportShape = ExportShape.RECORD
    #: For SFT: keep only positive training targets (the default).
    sft_good_only: bool = True

    def rows(self, dataset: Dataset) -> list[dict[str, Any]]:
        if self.shape is ExportShape.RECORD:
            return [ex.to_record() for ex in dataset.examples]
        if self.shape is ExportShape.SFT:
            src = (
                [e for e in dataset.examples if _is_good(e)]
                if self.sft_good_only
                else list(dataset.examples)
            )
            return [sft_row(e) for e in src]
        if self.shape is ExportShape.PREFERENCE:
            return _preference_pairs(dataset.examples)
        raise ExportError(f"unknown export shape {self.shape!r}")

    def to_jsonl(self, dataset: Dataset) -> str:
        lines = (json.dumps(row, ensure_ascii=False, sort_keys=True) for row in self.rows(dataset))
        return "\n".join(lines) + ("\n" if dataset.examples else "")

    def to_bytes(self, dataset: Dataset) -> bytes:
        return self.to_jsonl(dataset).encode("utf-8")


def export_jsonl(
    dataset: Dataset,
    *,
    shape: ExportShape = ExportShape.RECORD,
    sft_good_only: bool = True,
) -> str:
    """Convenience wrapper around :class:`JSONLExporter`."""
    return JSONLExporter(shape=shape, sft_good_only=sft_good_only).to_jsonl(dataset)


# --------------------------------------------------------------------------- #
# Columnar
# --------------------------------------------------------------------------- #

#: The rectangular columns the columnar export emits (nested → JSON string).
_COLUMNS: tuple[str, ...] = (
    "id",
    "role",
    "task",
    "prompt_key",
    "prompt_version",
    "model",
    "input",
    "output",
    "qa",
    "director_edits",
    "reward",
    "labels",
    "weak_labels",
    "split",
    "book_id",
    "session_id",
    "group_key",
    "scrubbed",
    "content_hash",
    "created_at",
)

#: Columns whose values are nested structures, JSON-encoded into the cell.
_NESTED_COLUMNS = frozenset({"input", "qa", "director_edits", "labels", "weak_labels"})


def _cell(record: dict[str, Any], column: str) -> Any:
    value = record.get(column)
    if column in _NESTED_COLUMNS:
        return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else ""
    return value


@dataclass(frozen=True, slots=True)
class ColumnarExporter:
    """Serialize a dataset to a column-oriented frame + a CSV rendering."""

    columns: tuple[str, ...] = _COLUMNS

    def to_columns(self, dataset: Dataset) -> dict[str, list[Any]]:
        """A dict-of-arrays (pandas/pyarrow ``from_dict`` shape)."""
        out: dict[str, list[Any]] = {c: [] for c in self.columns}
        for ex in dataset.examples:
            rec = ex.to_record()
            for c in self.columns:
                out[c].append(_cell(rec, c))
        return out

    def to_records(self, dataset: Dataset) -> list[dict[str, Any]]:
        """A row-oriented list-of-dicts with nested cells JSON-encoded."""
        return [{c: _cell(ex.to_record(), c) for c in self.columns} for ex in dataset.examples]

    def to_csv(self, dataset: Dataset) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(self.columns), extrasaction="ignore")
        writer.writeheader()
        for row in self.to_records(dataset):
            writer.writerow(row)
        return buf.getvalue()


def export_columns(dataset: Dataset) -> dict[str, list[Any]]:
    return ColumnarExporter().to_columns(dataset)


def export_csv(dataset: Dataset) -> str:
    return ColumnarExporter().to_csv(dataset)


# --------------------------------------------------------------------------- #
# Round-trip read-back (so an export can be re-loaded for verification)
# --------------------------------------------------------------------------- #


def read_jsonl_records(text: str) -> Iterable[dict[str, Any]]:
    """Parse a RECORD-shape JSONL export back into dict rows (verification path)."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExportError(f"malformed JSONL line: {exc}") from exc


def shape_for_task(task: TaskType) -> ExportShape:
    """The default export shape a downstream facet wants for a given task."""
    if task is TaskType.SFT:
        return ExportShape.SFT
    if task is TaskType.PREFERENCE:
        return ExportShape.PREFERENCE
    return ExportShape.RECORD


__all__ = [
    "ColumnarExporter",
    "ExportShape",
    "JSONLExporter",
    "export_columns",
    "export_csv",
    "export_jsonl",
    "read_jsonl_records",
    "sft_row",
    "shape_for_task",
]
