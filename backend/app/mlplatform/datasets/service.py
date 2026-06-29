"""``DatasetService`` — the single façade the composition root + API wire to.

Everything below the service is pure stages + an injected registry; the service
is the thin, stable surface the rest of Kinora (and the sibling ML facets) calls:

* :meth:`build` — run the full pipeline from a :class:`TraceSource` to a versioned,
  split dataset (delegates to :class:`DatasetPipeline`).
* :meth:`build_from_llmops` — convenience: wrap an ``app.llmops`` trace store +
  optional QA / edits joins into a source and build. The *only* place the
  llmops seam is touched, and it is a read-only adapter (see
  :mod:`app.mlplatform.datasets.sources`).
* :meth:`export` — serialize a version (or a named latest) to JSONL / columnar in
  a chosen shape, optionally restricted to one split (the train/val/test feed the
  sibling facets pull).
* :meth:`stats` / :meth:`drift` / :meth:`diff` / :meth:`lineage` — the read models
  over committed versions.

The service holds a :class:`DatasetRegistry` (in-memory by default); the DB-backed
:mod:`app.mlplatform.datasets.store` can mirror committed versions for durability.
Pure + offline: no network, no model calls, no credits.
"""

from __future__ import annotations

from typing import Any

from app.mlplatform.datasets.contracts import Dataset, Split, TraceSource
from app.mlplatform.datasets.diff import DatasetDiff
from app.mlplatform.datasets.drift import DriftReport, drift_between
from app.mlplatform.datasets.export import (
    ColumnarExporter,
    ExportShape,
    JSONLExporter,
)
from app.mlplatform.datasets.filtering import Predicate, apply_filter, golden_subset
from app.mlplatform.datasets.pipeline import BuildConfig, BuildResult, DatasetPipeline
from app.mlplatform.datasets.sampling import BalanceMode, ClassKey, balance_by, role_key
from app.mlplatform.datasets.sources import EditsJoin, LLMOpsTraceSource, QAJoin
from app.mlplatform.datasets.stats import DatasetStats
from app.mlplatform.datasets.versioning import (
    DatasetRegistry,
    DatasetVersion,
    LineageNode,
    Operation,
)


class DatasetService:
    """The façade over the dataset + trace pipeline."""

    def __init__(self, registry: DatasetRegistry | None = None) -> None:
        self.registry = registry or DatasetRegistry()
        self.pipeline = DatasetPipeline(self.registry)

    # -- build ------------------------------------------------------------- #

    def build(
        self,
        name: str,
        source: TraceSource,
        *,
        config: BuildConfig | None = None,
        description: str = "",
    ) -> BuildResult:
        """Run the full pipeline from a trace source to a versioned dataset."""
        return self.pipeline.build(name, source, config=config, description=description)

    def build_from_llmops(
        self,
        name: str,
        store: Any,
        *,
        qa_join: QAJoin | None = None,
        edits_join: EditsJoin | None = None,
        query_factory: Any = None,
        config: BuildConfig | None = None,
        description: str = "",
    ) -> BuildResult:
        """Build directly from an ``app.llmops`` trace store (read-only adapter)."""
        source = LLMOpsTraceSource(
            store=store,
            qa_join=qa_join,
            edits_join=edits_join,
            query_factory=query_factory,
        )
        return self.build(name, source, config=config, description=description)

    # -- read models ------------------------------------------------------- #

    def resolve(self, ref: str) -> DatasetVersion:
        return self.registry.resolve(ref)

    def latest(self, name: str) -> DatasetVersion:
        return self.registry.latest(name)

    def history(self, name: str) -> list[DatasetVersion]:
        return self.registry.history(name)

    def names(self) -> list[str]:
        return self.registry.names()

    def stats(self, ref: str) -> DatasetStats:
        return self.registry.resolve(ref).stats

    def lineage(self, ref: str) -> list[LineageNode]:
        return self.registry.lineage(self.registry.resolve(ref).version_id)

    def diff(self, base_ref: str, target_ref: str) -> DatasetDiff:
        return self.registry.diff(base_ref, target_ref)

    def drift(self, reference_ref: str, candidate_ref: str) -> DriftReport:
        ref = self.registry.resolve(reference_ref).dataset
        cand = self.registry.resolve(candidate_ref).dataset
        return drift_between(ref, cand)

    def tag(self, ref: str, tag: str) -> None:
        self.registry.tag(self.registry.resolve(ref).version_id, tag)

    # -- derivations (commit a new version with lineage to the source) ----- #

    def derive_filtered(
        self,
        ref: str,
        predicate: Predicate,
        *,
        name: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> DatasetVersion:
        """Commit a FILTER child of ``ref`` keeping only ``predicate``-matching rows."""
        parent = self.registry.resolve(ref)
        filtered, report = apply_filter(parent.dataset, predicate)
        ds = Dataset(
            name=name or parent.name,
            examples=filtered.examples,
            description=parent.dataset.description,
            meta=parent.dataset.meta,
        )
        return self.registry.commit(
            ds,
            operation=Operation.FILTER,
            parents=[parent.version_id],
            op_params=report.to_dict(),
            tags=tags,
        )

    def derive_golden(self, ref: str, *, name: str | None = None) -> DatasetVersion:
        """Commit the QA-passed, high-reward, unedited 'golden' subset of ``ref``."""
        parent = self.registry.resolve(ref)
        golden, report = golden_subset(parent.dataset)
        ds = Dataset(
            name=name or f"{parent.name}-golden",
            examples=golden.examples,
            description=parent.dataset.description,
            meta=parent.dataset.meta,
        )
        return self.registry.commit(
            ds,
            operation=Operation.FILTER,
            parents=[parent.version_id],
            op_params=report.to_dict(),
        )

    def derive_balanced(
        self,
        ref: str,
        key: ClassKey = role_key,
        *,
        mode: BalanceMode = BalanceMode.UNDERSAMPLE,
        target: int | None = None,
        seed: int = 1729,
        name: str | None = None,
    ) -> DatasetVersion:
        """Commit a class-balanced FILTER child of ``ref`` (balanced on ``key``)."""
        parent = self.registry.resolve(ref)
        balanced, report = balance_by(
            parent.dataset, key, mode=mode, target=target, seed=seed
        )
        ds = Dataset(
            name=name or f"{parent.name}-balanced",
            examples=balanced.examples,
            description=parent.dataset.description,
            meta=parent.dataset.meta,
        )
        return self.registry.commit(
            ds,
            operation=Operation.FILTER,
            parents=[parent.version_id],
            op_params=report.to_dict(),
        )

    # -- export ------------------------------------------------------------ #

    def _dataset_for_export(self, ref: str, split: Split | None) -> Dataset:
        ds = self.registry.resolve(ref).dataset
        return ds.by_split(split) if split is not None else ds

    def export_jsonl(
        self,
        ref: str,
        *,
        shape: ExportShape = ExportShape.RECORD,
        split: Split | None = None,
        sft_good_only: bool = True,
    ) -> str:
        ds = self._dataset_for_export(ref, split)
        return JSONLExporter(shape=shape, sft_good_only=sft_good_only).to_jsonl(ds)

    def export_columns(self, ref: str, *, split: Split | None = None) -> dict[str, list[Any]]:
        return ColumnarExporter().to_columns(self._dataset_for_export(ref, split))

    def export_csv(self, ref: str, *, split: Split | None = None) -> str:
        return ColumnarExporter().to_csv(self._dataset_for_export(ref, split))

    # -- convenience for the sibling facets -------------------------------- #

    def training_feed(
        self,
        ref: str,
        *,
        shape: ExportShape = ExportShape.SFT,
        split: Split = Split.TRAIN,
    ) -> str:
        """The JSONL feed a sibling facet pulls for a given split + task shape."""
        return self.export_jsonl(ref, shape=shape, split=split)

    def build_summary(self, name: str) -> dict[str, Any]:
        """A compact summary of a named dataset's latest version (the API card)."""
        version = self.latest(name)
        return {
            "name": name,
            "latest_version": version.version_id,
            "n": version.n,
            "operation": version.operation.value,
            "stats": version.stats.to_dict(),
            "lineage": [node.to_dict() for node in self.lineage(version.version_id)],
        }


__all__ = ["DatasetService"]
