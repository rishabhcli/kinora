"""The end-to-end dataset build pipeline, wired from the stages.

This is the orchestration layer: it reads a :class:`TraceSource`, runs the
stages in the right order, commits each meaningful intermediate as an immutable
version (so the lineage DAG records *exactly* how the final training set was
built), and returns a :class:`BuildResult` carrying every stage's report.

The canonical order (each stage's output is the next's input):

1. **ingest** — :class:`RawTrace` → :class:`TraceExample` (drop policy applied).
2. **scrub** — PII / secret redaction (so no PII ever reaches a frozen version).
3. **dedup** — exact + near-duplicate collapse, best-representative survival.
4. **label** — weak-supervision consensus labels.
5. **split** — leak-free, stratified train/val/test assignment.

Scrubbing runs *before* dedup on purpose: redaction is idempotent and makes two
PII-differing-but-otherwise-identical traces collapse to one. Labelling runs
before splitting so the split can stratify on the label. Each stage is
individually toggleable via :class:`BuildConfig`, and each commits a version
under the same dataset name, so ``registry.history(name)`` reads back as the
build's audit trail.

Pure orchestration over injected pure stages + a registry; no I/O of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

from app.mlplatform.datasets.contracts import Dataset, TraceSource
from app.mlplatform.datasets.dedup import DedupReport, NearDedupConfig, dedup
from app.mlplatform.datasets.ingest import IngestConfig, IngestStats, ingest_all
from app.mlplatform.datasets.labeling import LabelModel, LabelReport
from app.mlplatform.datasets.scrub import Scrubber, ScrubReport, scrub_examples
from app.mlplatform.datasets.splitting import SplitConfig, SplitReport, split_dataset
from app.mlplatform.datasets.versioning import (
    DatasetRegistry,
    DatasetVersion,
    Operation,
)


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Which stages run and how (every stage independently toggleable)."""

    ingest: IngestConfig = field(default_factory=IngestConfig)
    do_scrub: bool = True
    scrubber: Scrubber | None = None
    do_dedup: bool = True
    near_dedup: bool = True
    near_dedup_config: NearDedupConfig | None = None
    do_label: bool = True
    label_model: LabelModel | None = None
    do_split: bool = True
    split: SplitConfig = field(default_factory=SplitConfig)
    #: When set, only ingest traces created at/after this timestamp.
    since: datetime | None = None
    limit: int | None = None
    tags: tuple[str, ...] = ()


@dataclass
class BuildResult:
    """Everything a build produced: the final version + every stage's report."""

    name: str
    final_version: DatasetVersion
    versions: list[DatasetVersion] = field(default_factory=list)
    ingest_stats: IngestStats | None = None
    scrub_report: ScrubReport | None = None
    dedup_report: DedupReport | None = None
    label_report: LabelReport | None = None
    split_report: SplitReport | None = None

    @property
    def dataset(self) -> Dataset:
        return self.final_version.dataset

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "final_version_id": self.final_version.version_id,
            "n": self.final_version.n,
            "lineage": [v.version_id for v in self.versions],
            "ingest": self.ingest_stats.to_dict() if self.ingest_stats else None,
            "scrub": self.scrub_report.to_dict() if self.scrub_report else None,
            "dedup": self.dedup_report.to_dict() if self.dedup_report else None,
            "label": self.label_report.to_dict() if self.label_report else None,
            "split": self.split_report.to_dict() if self.split_report else None,
            "stats": self.final_version.stats.to_dict(),
        }


class DatasetPipeline:
    """Build a versioned dataset from a trace source through the staged flow."""

    def __init__(self, registry: DatasetRegistry | None = None) -> None:
        self.registry = registry or DatasetRegistry()

    def build(
        self,
        name: str,
        source: TraceSource,
        *,
        config: BuildConfig | None = None,
        description: str = "",
    ) -> BuildResult:
        cfg = config or BuildConfig()
        versions: list[DatasetVersion] = []

        # 1) Ingest.
        raws = source.iter_raw(since=cfg.since, limit=cfg.limit)
        examples, ingest_stats = ingest_all(raws, config=cfg.ingest)
        ds = Dataset.from_examples(name, examples, description=description)
        version = self.registry.commit(
            ds,
            operation=Operation.INGEST,
            op_params={"seen": ingest_stats.seen, "kept": ingest_stats.kept},
            note="ingested from trace source",
        )
        versions.append(version)

        scrub_report: ScrubReport | None = None
        dedup_report: DedupReport | None = None
        label_report: LabelReport | None = None
        split_report: SplitReport | None = None

        # 2) Scrub.
        if cfg.do_scrub:
            scrubbed, scrub_report = scrub_examples(ds.examples, scrubber=cfg.scrubber)
            ds = Dataset.from_examples(name, scrubbed, description=description)
            version = self.registry.commit(
                ds,
                operation=Operation.SCRUB,
                parents=[version.version_id],
                op_params=scrub_report.to_dict(),
            )
            versions.append(version)

        # 3) Dedup.
        if cfg.do_dedup:
            deduped, dedup_report = dedup(
                ds.examples, near=cfg.near_dedup, config=cfg.near_dedup_config
            )
            ds = Dataset.from_examples(name, deduped, description=description)
            version = self.registry.commit(
                ds,
                operation=Operation.DEDUP,
                parents=[version.version_id],
                op_params=dedup_report.to_dict(),
            )
            versions.append(version)

        # 4) Label.
        if cfg.do_label:
            labeled, label_report = (cfg.label_model or LabelModel()).fit_predict(ds.examples)
            ds = Dataset.from_examples(name, labeled, description=description)
            version = self.registry.commit(
                ds,
                operation=Operation.LABEL,
                parents=[version.version_id],
                op_params={"coverage": round(label_report.coverage, 6)},
            )
            versions.append(version)

        # 5) Split.
        if cfg.do_split and ds.examples:
            ds, split_report = split_dataset(ds, config=cfg.split)
            version = self.registry.commit(
                ds,
                operation=Operation.SPLIT,
                parents=[version.version_id],
                op_params=split_report.to_dict(),
                tags=cfg.tags,
            )
            versions.append(version)
        elif cfg.tags:
            # Tag the final version even when split is skipped.
            for tag in cfg.tags:
                self.registry.tag(version.version_id, tag)

        return BuildResult(
            name=name,
            final_version=version,
            versions=versions,
            ingest_stats=ingest_stats,
            scrub_report=scrub_report,
            dedup_report=dedup_report,
            label_report=label_report,
            split_report=split_report,
        )


__all__ = ["BuildConfig", "BuildResult", "DatasetPipeline"]
