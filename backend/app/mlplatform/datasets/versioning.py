"""Versioned, immutable, content-addressed dataset store with lineage.

A dataset version is a *frozen* snapshot: once committed it can never be mutated
or overwritten (an attempt raises :class:`ImmutabilityError`). Each version is
identified by a content hash of its examples, carries the parent it was derived
from and the operation that produced it (ingest / scrub / dedup / split / label /
filter / merge), and records a stats snapshot — so the full provenance of any
training set is reconstructable as a DAG.

Why content addressing: re-committing the identical dataset is a no-op that
returns the existing version, so the pipeline is idempotent and storage is
deduplicated. Why a DAG (not a line): a version can have *two* parents (a merge),
and a lineage walk answers "what raw traces, scrub rules, and split seed produced
the set we trained model M on?".

* :class:`DatasetVersion` — the immutable record (id, dataset, parents, op,
  stats, created_at, tags).
* :class:`VersionStore` (protocol) + :class:`InMemoryVersionStore` — the default
  in-process store; :mod:`app.mlplatform.datasets.store` adds the DB-backed mirror.
* :class:`DatasetRegistry` — the high-level façade: commit a dataset under a
  *name* (a moving pointer to its latest version), walk lineage, diff two
  versions, tag, and resolve ``name`` → latest or ``name@version``.

Pure + deterministic; no I/O beyond the injected store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.mlplatform.datasets.contracts import Dataset, stable_hash
from app.mlplatform.datasets.diff import DatasetDiff, diff_datasets
from app.mlplatform.datasets.errors import ImmutabilityError, VersionError
from app.mlplatform.datasets.stats import DatasetStats, compute_stats


def _now() -> datetime:
    return datetime.now(UTC)


class Operation(StrEnum):
    """The pipeline step that produced a version (the lineage edge label)."""

    INGEST = "ingest"
    SCRUB = "scrub"
    DEDUP = "dedup"
    SPLIT = "split"
    LABEL = "label"
    FILTER = "filter"
    MERGE = "merge"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """An immutable, content-addressed snapshot of a dataset + its provenance."""

    version_id: str
    name: str
    content_hash: str
    dataset: Dataset
    operation: Operation
    parents: tuple[str, ...]
    stats: DatasetStats
    created_at: datetime = field(default_factory=_now)
    op_params: Mapping[str, object] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    note: str = ""

    @property
    def n(self) -> int:
        return len(self.dataset)

    def to_dict(self, *, include_examples: bool = False) -> dict[str, object]:
        out: dict[str, object] = {
            "version_id": self.version_id,
            "name": self.name,
            "content_hash": self.content_hash,
            "operation": self.operation.value,
            "parents": list(self.parents),
            "n": self.n,
            "stats": self.stats.to_dict(),
            "created_at": self.created_at.isoformat(),
            "op_params": dict(self.op_params),
            "tags": list(self.tags),
            "note": self.note,
        }
        if include_examples:
            out["examples"] = [e.to_record() for e in self.dataset.examples]
        return out


def make_version_id(name: str, content_hash: str, parents: Sequence[str]) -> str:
    """A deterministic version id from the name + content + parents.

    Including the parents means two versions with identical content but different
    lineage (e.g. arrived via different ops) get distinct ids, so the DAG stays
    unambiguous; re-committing the *same* content with the *same* parents is the
    idempotent no-op the registry relies on.
    """
    digest = stable_hash({"name": name, "content": content_hash, "parents": sorted(parents)})
    return f"dsv_{digest[:24]}"


class VersionStore(Protocol):
    """Where committed :class:`DatasetVersion` records live."""

    def put(self, version: DatasetVersion) -> None: ...

    def get(self, version_id: str) -> DatasetVersion | None: ...

    def all_versions(self) -> list[DatasetVersion]: ...


@dataclass
class InMemoryVersionStore:
    """A dict-backed, immutable version store (the default in-process store)."""

    _versions: dict[str, DatasetVersion] = field(default_factory=dict)

    def put(self, version: DatasetVersion) -> None:
        existing = self._versions.get(version.version_id)
        if existing is not None:
            # Idempotent re-put is fine iff identical content; otherwise it is an
            # attempt to overwrite a frozen version.
            if existing.content_hash != version.content_hash:
                raise ImmutabilityError(
                    f"version {version.version_id!r} already exists with different content"
                )
            return
        self._versions[version.version_id] = version

    def get(self, version_id: str) -> DatasetVersion | None:
        return self._versions.get(version_id)

    def all_versions(self) -> list[DatasetVersion]:
        return list(self._versions.values())


@dataclass(frozen=True, slots=True)
class LineageNode:
    """One node in a rendered lineage walk (id + op + parents + size)."""

    version_id: str
    operation: str
    parents: tuple[str, ...]
    n: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "operation": self.operation,
            "parents": list(self.parents),
            "n": self.n,
            "created_at": self.created_at,
        }


@dataclass
class DatasetRegistry:
    """The façade over a :class:`VersionStore`: commit, tag, resolve, lineage, diff.

    A *name* is a moving pointer to its latest committed version (the "branch
    head"); ``name@version_id`` pins an exact snapshot. Commits are
    content-addressed and idempotent.
    """

    store: VersionStore = field(default_factory=InMemoryVersionStore)
    #: name → ordered list of version ids (newest last).
    _heads: dict[str, list[str]] = field(default_factory=dict)
    _tags: dict[str, str] = field(default_factory=dict)

    def commit(
        self,
        dataset: Dataset,
        *,
        operation: Operation,
        parents: Sequence[str] = (),
        op_params: Mapping[str, object] | None = None,
        tags: Sequence[str] = (),
        note: str = "",
    ) -> DatasetVersion:
        """Freeze ``dataset`` as a new version (idempotent on identical content)."""
        for parent in parents:
            if self.store.get(parent) is None:
                raise VersionError(f"unknown parent version {parent!r}")
        content_hash = dataset.content_hash
        version_id = make_version_id(dataset.name, content_hash, parents)

        existing = self.store.get(version_id)
        if existing is not None:
            return existing  # idempotent no-op

        version = DatasetVersion(
            version_id=version_id,
            name=dataset.name,
            content_hash=content_hash,
            dataset=dataset,
            operation=operation,
            parents=tuple(parents),
            stats=compute_stats(dataset),
            op_params=dict(op_params or {}),
            tags=tuple(tags),
            note=note,
        )
        self.store.put(version)
        self._heads.setdefault(dataset.name, []).append(version_id)
        for tag in tags:
            self._tags[tag] = version_id
        return version

    def tag(self, version_id: str, tag: str) -> None:
        """Pin a human-readable tag (e.g. ``rm_v1``) to a version."""
        if self.store.get(version_id) is None:
            raise VersionError(f"cannot tag unknown version {version_id!r}")
        self._tags[tag] = version_id

    def get(self, version_id: str) -> DatasetVersion:
        v = self.store.get(version_id)
        if v is None:
            raise VersionError(f"unknown version {version_id!r}")
        return v

    def resolve(self, ref: str) -> DatasetVersion:
        """Resolve a ref: a version id, a ``tag``, a ``name``, or ``name@version``."""
        if self.store.get(ref) is not None:
            return self.get(ref)
        if ref in self._tags:
            return self.get(self._tags[ref])
        if "@" in ref:
            _name, _, vid = ref.partition("@")
            return self.get(vid)
        return self.latest(ref)

    def latest(self, name: str) -> DatasetVersion:
        """The newest committed version of a named dataset."""
        ids = self._heads.get(name)
        if not ids:
            raise VersionError(f"no versions committed for {name!r}")
        return self.get(ids[-1])

    def history(self, name: str) -> list[DatasetVersion]:
        """All versions committed under a name, oldest → newest."""
        return [self.get(vid) for vid in self._heads.get(name, [])]

    def names(self) -> list[str]:
        return sorted(self._heads)

    def lineage(self, version_id: str) -> list[LineageNode]:
        """The full ancestry DAG of a version, topologically (ancestors first)."""
        order: list[str] = []
        seen: set[str] = set()

        def visit(vid: str, stack: frozenset[str]) -> None:
            if vid in stack:
                raise VersionError(f"lineage cycle detected at {vid!r}")
            if vid in seen:
                return
            v = self.get(vid)
            for p in v.parents:
                visit(p, stack | {vid})
            seen.add(vid)
            order.append(vid)

        visit(version_id, frozenset())
        return [
            LineageNode(
                version_id=v.version_id,
                operation=v.operation.value,
                parents=v.parents,
                n=v.n,
                created_at=v.created_at.isoformat(),
            )
            for v in (self.get(vid) for vid in order)
        ]

    def diff(self, base_ref: str, target_ref: str) -> DatasetDiff:
        """Structural diff between two versions (by ref)."""
        return diff_datasets(self.resolve(base_ref).dataset, self.resolve(target_ref).dataset)


__all__ = [
    "DatasetRegistry",
    "DatasetVersion",
    "InMemoryVersionStore",
    "LineageNode",
    "Operation",
    "VersionStore",
    "make_version_id",
]
