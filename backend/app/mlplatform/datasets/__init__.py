"""ML dataset + trace pipeline (facet A of the self-improvement platform).

The data foundation for Kinora's self-improvement loop: it ingests agent
run-traces (prompt + input + output + QA verdict + director edits) through a
**read-only** :class:`TraceSource` seam, then turns them into a versioned,
immutable dataset store with lineage, dedup, PII scrubbing, leak-free stratified
splitting, weak-supervision labeling, dataset diffing + stats + drift checks, and
export adapters (JSONL / columnar) the alignment + serving facets consume.

Public contracts the sibling facets import:

* :class:`TraceSource` / :class:`RawTrace` — the read-only ingest seam.
* :class:`TraceExample` / :class:`Dataset` — the immutable training currency.
* :class:`DatasetService` — the façade (build / export / stats / drift / lineage).

See ``DESIGN.md`` for the architecture, the stage order, and the cross-facet
contract surface.
"""

from __future__ import annotations

from app.mlplatform.datasets.contracts import (
    AgentRole,
    Dataset,
    DirectorEdit,
    QAVerdict,
    RawTrace,
    Split,
    TaskType,
    TraceExample,
    TraceSource,
)
from app.mlplatform.datasets.dedup import DedupReport, NearDedupConfig, dedup
from app.mlplatform.datasets.diff import DatasetDiff, diff_datasets
from app.mlplatform.datasets.drift import DriftReport, DriftSeverity, drift_between
from app.mlplatform.datasets.errors import (
    DatasetError,
    ExportError,
    ImmutabilityError,
    IngestError,
    LabelError,
    MLDataError,
    ScrubError,
    SourceError,
    SplitError,
    VersionError,
)
from app.mlplatform.datasets.export import (
    ColumnarExporter,
    ExportShape,
    JSONLExporter,
    export_csv,
    export_jsonl,
)
from app.mlplatform.datasets.filtering import (
    FilterReport,
    QualityTier,
    apply_filter,
    golden_subset,
    order_by_difficulty,
    quality_tiers,
)
from app.mlplatform.datasets.ingest import IngestConfig, ingest_all, normalize
from app.mlplatform.datasets.labeling import LabelModel, apply_labeling, default_lfs
from app.mlplatform.datasets.pipeline import BuildConfig, BuildResult, DatasetPipeline
from app.mlplatform.datasets.sampling import (
    BalanceMode,
    SampleReport,
    balance_by,
    stratified_subsample,
    subsample,
    weighted_sample,
)
from app.mlplatform.datasets.scrub import Scrubber, scrub_examples
from app.mlplatform.datasets.service import DatasetService
from app.mlplatform.datasets.sources import InMemoryTraceSource, LLMOpsTraceSource
from app.mlplatform.datasets.splitting import (
    SplitConfig,
    SplitRatios,
    split_dataset,
)
from app.mlplatform.datasets.stats import DatasetStats, compute_stats
from app.mlplatform.datasets.versioning import (
    DatasetRegistry,
    DatasetVersion,
    InMemoryVersionStore,
    Operation,
    VersionStore,
)

__all__ = [
    "AgentRole",
    "BalanceMode",
    "BuildConfig",
    "BuildResult",
    "ColumnarExporter",
    "Dataset",
    "DatasetDiff",
    "DatasetError",
    "DatasetPipeline",
    "DatasetRegistry",
    "DatasetService",
    "DatasetStats",
    "DatasetVersion",
    "DedupReport",
    "DirectorEdit",
    "DriftReport",
    "DriftSeverity",
    "ExportError",
    "ExportShape",
    "FilterReport",
    "ImmutabilityError",
    "InMemoryTraceSource",
    "InMemoryVersionStore",
    "IngestConfig",
    "IngestError",
    "JSONLExporter",
    "LLMOpsTraceSource",
    "LabelError",
    "LabelModel",
    "MLDataError",
    "NearDedupConfig",
    "Operation",
    "QAVerdict",
    "QualityTier",
    "RawTrace",
    "SampleReport",
    "ScrubError",
    "Scrubber",
    "SourceError",
    "Split",
    "SplitConfig",
    "SplitError",
    "SplitRatios",
    "TaskType",
    "TraceExample",
    "TraceSource",
    "VersionError",
    "VersionStore",
    "apply_filter",
    "apply_labeling",
    "balance_by",
    "compute_stats",
    "dedup",
    "default_lfs",
    "diff_datasets",
    "drift_between",
    "export_csv",
    "export_jsonl",
    "golden_subset",
    "ingest_all",
    "normalize",
    "order_by_difficulty",
    "quality_tiers",
    "scrub_examples",
    "split_dataset",
    "stratified_subsample",
    "subsample",
    "weighted_sample",
]
