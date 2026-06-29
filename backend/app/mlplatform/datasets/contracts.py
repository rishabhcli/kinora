"""The contracts the whole ML-data plane (and its sibling facets) is built on.

This module defines the *shapes* every other module in
:mod:`app.mlplatform.datasets` produces or consumes, and the shapes the sibling
facets (alignment / reward modelling, serving / routing) import. It is pure data
+ protocols — no I/O, no model calls, no app-wide imports beyond the package's
own errors.

The three load-bearing contracts:

* :class:`TraceExample` — one immutable, normalized training example distilled
  from a single agent run-trace: the prompt identity, the rendered input, the
  produced output, an optional QA verdict (from the Critic, §9.5) and an optional
  set of director edits (§5.4). Carries provenance (the originating trace id,
  book/session) and a content hash for dedup + content addressing.
* :class:`Dataset` — an ordered, immutable collection of :class:`TraceExample`
  with a name, a split assignment, and label metadata. The unit the export
  adapters serialize and the sibling facets train on.
* :class:`TraceSource` (Protocol) — the **read-only seam** the pipeline reads
  through. The production implementation (:mod:`app.mlplatform.datasets.sources`)
  adapts :class:`app.llmops.tracing.RunTrace` + the Critic's
  :class:`app.agents.contracts.QARecord` *without importing or mutating* them; a
  fake source drives the tests with zero infra.

Design notes:

* **Immutability is structural.** ``TraceExample`` and ``Dataset`` are frozen
  dataclasses; "editing" a dataset returns a new object (and a new version id).
  This is what makes the version store's content addressing honest.
* **The content hash is canonical.** ``TraceExample.content_hash`` is a stable
  SHA-256 over the *semantic* payload (prompt key+version, input, output, label),
  computed via canonical JSON — so two examples that mean the same thing collide
  for dedup regardless of dict ordering or provenance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.mlplatform.datasets.errors import DatasetError


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, compact, stable for None)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def stable_hash(value: Any) -> str:
    """A stable SHA-256 hex digest of any JSON-able value (the content address)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Enumerations — the crew roles and the learning task each example serves
# --------------------------------------------------------------------------- #


class AgentRole(StrEnum):
    """Which of the six crew agents (kinora.md §7) produced a trace.

    Derived from the trace's ``prompt_key`` by :mod:`app.mlplatform.datasets.sources`
    so the pipeline can stratify and split per role without importing the agents.
    ``UNKNOWN`` covers traces whose prompt key the mapping does not recognise.
    """

    ADAPTER = "adapter"
    CINEMATOGRAPHER = "cinematographer"
    CONTINUITY = "continuity"
    CRITIC = "critic"
    SHOWRUNNER = "showrunner"
    GENERATOR = "generator"
    UNKNOWN = "unknown"


class TaskType(StrEnum):
    """The learning task an example serves for the downstream facets.

    * ``SFT`` — supervised fine-tuning: ``(input → output)`` pairs, optionally
      filtered to QA-passed outputs (the "golden" behaviours to imitate).
    * ``PREFERENCE`` — a preference / reward signal: an output plus a scalar
      reward derived from the QA verdict and director edits (the alignment facet's
      reward-model fuel).
    * ``EVAL`` — held-out evaluation cases (never trained on).
    * ``REWARD`` — a dense reward-regression target (CCS / learned-reward axes).
    """

    SFT = "sft"
    PREFERENCE = "preference"
    EVAL = "eval"
    REWARD = "reward"


class Split(StrEnum):
    """The train / validation / test partition an example is assigned to."""

    TRAIN = "train"
    VAL = "val"
    TEST = "test"
    UNASSIGNED = "unassigned"


# --------------------------------------------------------------------------- #
# QA verdict + director-edit value objects (mirrors, not imports)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QAVerdict:
    """The QA signal distilled from the Critic's :class:`QARecord` (§9.5).

    A *mirror* of the relevant fields, not the model itself: the source adapter
    reads the Critic's record and projects it here, so the dataset plane never
    imports the agents contracts at runtime and stays decoupled from their churn.
    ``passed`` is the pre-registered four-check verdict; ``score`` / ``ccs`` /
    ``reward`` are the dense signals the reward facet regresses on.
    """

    passed: bool
    score: float = 0.0
    ccs: float | None = None
    style_drift: float | None = None
    motion_artifact: float | None = None
    reward: float | None = None
    repair_action: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "ccs": self.ccs,
            "style_drift": self.style_drift,
            "motion_artifact": self.motion_artifact,
            "reward": self.reward,
            "repair_action": self.repair_action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DirectorEdit:
    """A director's correction of a shot (§5.4) — a strong supervision signal.

    A director edit is the highest-value label the system gets: a human said the
    machine's output was wrong *and how*. ``instruction`` is the natural-language
    correction; ``region`` optionally scopes it to part of the frame; ``before`` /
    ``after`` capture the changed payload when known. Carried as a list on a
    :class:`TraceExample` so a single output can accrue several edits over time.
    """

    instruction: str
    region: str | None = None
    before: str | None = None
    after: str | None = None
    edited_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "region": self.region,
            "before": self.before,
            "after": self.after,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
        }


# --------------------------------------------------------------------------- #
# TraceExample — one immutable training example
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TraceExample:
    """One normalized, immutable training example distilled from a run-trace.

    Provenance (``trace_id`` / ``book_id`` / ``session_id``) is carried but is
    *not* part of the content hash, so two semantically identical examples from
    different sessions dedup to one. ``labels`` and ``weak_labels`` are filled by
    the labeling stage; ``split`` by the splitter; both default to neutral so a
    freshly-ingested example is valid before those stages run.
    """

    id: str
    role: AgentRole
    task: TaskType
    prompt_key: str
    prompt_version: str
    model: str
    input: Mapping[str, Any]
    output: str
    # -- supervision signals (optional) ------------------------------------- #
    qa: QAVerdict | None = None
    director_edits: tuple[DirectorEdit, ...] = ()
    reward: float | None = None
    # -- labels (filled by the labeling stage) ------------------------------ #
    labels: Mapping[str, Any] = field(default_factory=dict)
    weak_labels: Mapping[str, Any] = field(default_factory=dict)
    # -- partition + provenance --------------------------------------------- #
    split: Split = Split.UNASSIGNED
    trace_id: str | None = None
    book_id: str | None = None
    session_id: str | None = None
    created_at: datetime = field(default_factory=_now)
    # -- bookkeeping -------------------------------------------------------- #
    #: A grouping key the splitter uses to keep related examples on one side of a
    #: split (leak prevention). Defaults to the book id (or the example id when no
    #: book attribution exists), so shots from one book never straddle train/test.
    group_key: str = ""
    scrubbed: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise DatasetError("TraceExample requires a non-empty id")
        if not self.group_key:
            object.__setattr__(self, "group_key", self.book_id or self.id)

    @property
    def content_hash(self) -> str:
        """A stable content address over the *semantic* payload (not provenance).

        Two examples with the same prompt identity, input, output and labels hash
        identically regardless of which trace/session produced them — the key the
        deduper collapses on and the version store content-addresses with.
        """
        return stable_hash(
            {
                "role": self.role.value,
                "task": self.task.value,
                "prompt_key": self.prompt_key,
                "prompt_version": self.prompt_version,
                "model": self.model,
                "input": dict(self.input),
                "output": self.output,
                "qa": self.qa.to_dict() if self.qa else None,
                "director_edits": [e.to_dict() for e in self.director_edits],
                "reward": self.reward,
                "labels": dict(self.labels),
            }
        )

    def with_labels(
        self,
        labels: Mapping[str, Any] | None = None,
        weak_labels: Mapping[str, Any] | None = None,
    ) -> TraceExample:
        """A copy with labels merged in (immutable update)."""
        merged = {**self.labels, **(labels or {})}
        merged_weak = {**self.weak_labels, **(weak_labels or {})}
        return replace(self, labels=merged, weak_labels=merged_weak)

    def with_split(self, split: Split) -> TraceExample:
        """A copy assigned to a split (immutable update)."""
        return replace(self, split=split)

    def to_record(self) -> dict[str, Any]:
        """A flat JSON-able record (the export adapters' row shape)."""
        return {
            "id": self.id,
            "role": self.role.value,
            "task": self.task.value,
            "prompt_key": self.prompt_key,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "input": dict(self.input),
            "output": self.output,
            "qa": self.qa.to_dict() if self.qa else None,
            "director_edits": [e.to_dict() for e in self.director_edits],
            "reward": self.reward,
            "labels": dict(self.labels),
            "weak_labels": dict(self.weak_labels),
            "split": self.split.value,
            "trace_id": self.trace_id,
            "book_id": self.book_id,
            "session_id": self.session_id,
            "group_key": self.group_key,
            "scrubbed": self.scrubbed,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# Dataset — an immutable, ordered collection of examples
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Dataset:
    """An immutable, ordered collection of :class:`TraceExample`.

    The dataset is the unit the export adapters serialize and the sibling facets
    consume. Constructed once and never mutated: ``filter`` / ``map`` / ``concat``
    return new datasets. ``description`` and ``meta`` carry free-form provenance
    the version store records into lineage.
    """

    name: str
    examples: tuple[TraceExample, ...]
    description: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise DatasetError("Dataset requires a non-empty name")
        ids = [e.id for e in self.examples]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise DatasetError(f"dataset {self.name!r} has duplicate example ids: {dupes[:5]}")

    def __len__(self) -> int:
        return len(self.examples)

    def __iter__(self) -> Iterator[TraceExample]:
        return iter(self.examples)

    def __bool__(self) -> bool:
        return bool(self.examples)

    @property
    def content_hash(self) -> str:
        """A stable content address of the dataset (ordered example hashes + name)."""
        return stable_hash(
            {"name": self.name, "examples": [e.content_hash for e in self.examples]}
        )

    def by_split(self, split: Split) -> Dataset:
        """The sub-dataset of examples assigned to ``split``."""
        return self.filter(lambda e: e.split is split, name_suffix=split.value)

    def by_role(self, role: AgentRole) -> Dataset:
        return self.filter(lambda e: e.role is role, name_suffix=role.value)

    def by_task(self, task: TaskType) -> Dataset:
        return self.filter(lambda e: e.task is task, name_suffix=task.value)

    def filter(
        self, predicate: Any, *, name_suffix: str | None = None
    ) -> Dataset:
        """A new dataset of the examples matching ``predicate``."""
        kept = tuple(e for e in self.examples if predicate(e))
        name = f"{self.name}:{name_suffix}" if name_suffix else self.name
        return Dataset(name=name, examples=kept, description=self.description, meta=self.meta)

    def map(self, fn: Any, *, name_suffix: str | None = None) -> Dataset:
        """A new dataset with ``fn`` applied to every example."""
        mapped = tuple(fn(e) for e in self.examples)
        name = f"{self.name}:{name_suffix}" if name_suffix else self.name
        return Dataset(name=name, examples=mapped, description=self.description, meta=self.meta)

    def concat(self, other: Dataset, *, name: str | None = None) -> Dataset:
        """Concatenate two datasets (ids must remain unique)."""
        return Dataset(
            name=name or self.name,
            examples=(*self.examples, *other.examples),
            description=self.description,
            meta=self.meta,
        )

    @classmethod
    def from_examples(
        cls,
        name: str,
        examples: Iterable[TraceExample],
        *,
        description: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> Dataset:
        return cls(
            name=name,
            examples=tuple(examples),
            description=description,
            meta=dict(meta or {}),
        )


# --------------------------------------------------------------------------- #
# The read-only ingest seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RawTrace:
    """A provider-agnostic raw record yielded by a :class:`TraceSource`.

    A flattened, read-only projection of whatever the upstream observability
    layer holds (an ``app.llmops.tracing.RunTrace``, plus an optional Critic QA
    record and director edits joined in by the source). The ingest stage
    normalizes this into a :class:`TraceExample`; keeping it a plain dataclass
    means a fake source can build one with no app imports.
    """

    trace_id: str
    prompt_key: str
    prompt_version: str
    model: str
    inputs: Mapping[str, Any]
    output: str
    created_at: datetime
    book_id: str | None = None
    session_id: str | None = None
    error: str | None = None
    cache_hit: bool = False
    #: A raw QA record dict (Critic §9.5 projection) when one was joined in.
    qa: Mapping[str, Any] | None = None
    #: Raw director-edit dicts (§5.4) joined in for this trace's output.
    director_edits: Sequence[Mapping[str, Any]] = ()


@runtime_checkable
class TraceSource(Protocol):
    """The read-only seam the pipeline ingests through (no mutation of the source).

    Implementations adapt the existing observability planes — the production one
    wraps :class:`app.llmops.tracing.TraceStore` + the Critic's QA records — and
    yield :class:`RawTrace` rows. The contract is deliberately narrow (an iterable
    + a count) so it is trivial to fake in tests and to back by a DB cursor or an
    in-memory store interchangeably.
    """

    def iter_raw(
        self, *, since: datetime | None = None, limit: int | None = None
    ) -> Iterable[RawTrace]:
        """Yield raw traces, optionally only those after ``since``, up to ``limit``."""
        ...

    def count(self, *, since: datetime | None = None) -> int:
        """How many raw traces are available (for progress / sizing)."""
        ...


__all__ = [
    "AgentRole",
    "Dataset",
    "DirectorEdit",
    "QAVerdict",
    "RawTrace",
    "Split",
    "TaskType",
    "TraceExample",
    "TraceSource",
    "canonical_json",
    "stable_hash",
]
