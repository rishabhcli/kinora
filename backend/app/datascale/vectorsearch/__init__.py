"""``app.datascale.vectorsearch`` — a scalable approximate-nearest-neighbour service.

A from-scratch, NumPy-only ANN stack that *scales out* the coarse-recall step the
core backend does today inside pgvector (``app.memory.retrieval`` is the fine
re-rank over those candidates; this is the candidate generator that can grow past
a single Postgres). Nothing here edits the existing memory / search lanes — it is
a parallel, composable building block.

Components
----------
* :class:`HnswIndex` / :class:`HnswParams` — from-scratch HNSW graph
  (insert/search/delete with tombstones + repair, tunable ``M`` /
  ``ef_construction`` / ``ef``, deterministic, mmap-friendly layout).
* :class:`BruteForceIndex` — exact kNN, the recall ground truth.
* :class:`ProductQuantizer` / :class:`ScalarQuantizer` — PQ (ADC) + SQ
  compression; :class:`QuantizedFlatIndex` is the compressed flat index that
  uses them with optional exact re-rank.
* :class:`ShardedIndex` + :class:`Router` family — horizontal partitioning with
  a router and a k-way result merge.
* :class:`IncrementalBuilder` + ``build_*`` — incremental and batch build.
* :func:`benchmark` / :class:`BenchmarkReport` — recall@k / latency vs exact.
* :class:`VectorSearchService` + :class:`VectorSearchConfig` — the clean,
  backend-agnostic hybrid (vector + metadata + keyword) query API.
"""

from __future__ import annotations

from .benchmark import (
    BenchmarkReport,
    average_precision,
    benchmark,
    build_brute_force,
    compare,
    exact_ground_truth,
    ndcg,
    recall_at_k,
    run_demo,
)
from .brute_force import BruteForceIndex
from .builder import (
    IncrementalBuilder,
    auto_params,
    build_hnsw,
    build_quantized,
    build_sharded,
)
from .config import DEFAULT_EMBED_DIM, VectorSearchConfig
from .distance import metric_of, normalize, random_unit_vectors
from .filtering import Bm25KeywordIndex, Predicate, fuse_scores, tokenize
from .hnsw import HnswIndex, HnswParams, HnswStats
from .merge import merge_dedup_keep_closest, merge_results
from .pq_index import QuantizedFlatIndex
from .quantization import ProductQuantizer, ScalarQuantizer, kmeans
from .service import VectorSearchService
from .shard import (
    AttributeRouter,
    HashRouter,
    ModuloRouter,
    Router,
    ShardedIndex,
    rebalance_plan,
)
from .storage import (
    deserialize_index,
    load_index,
    save_index,
    serialize_index,
)
from .types import (
    Metadata,
    Metric,
    Query,
    SearchResult,
    VectorId,
    VectorLike,
    as_matrix,
    as_vector,
)

__all__ = [
    "DEFAULT_EMBED_DIM",
    "AttributeRouter",
    "BenchmarkReport",
    "Bm25KeywordIndex",
    "BruteForceIndex",
    "HashRouter",
    "HnswIndex",
    "HnswParams",
    "HnswStats",
    "IncrementalBuilder",
    "Metadata",
    "Metric",
    "ModuloRouter",
    "Predicate",
    "ProductQuantizer",
    "QuantizedFlatIndex",
    "Query",
    "Router",
    "ScalarQuantizer",
    "SearchResult",
    "ShardedIndex",
    "VectorId",
    "VectorLike",
    "VectorSearchConfig",
    "VectorSearchService",
    "as_matrix",
    "as_vector",
    "auto_params",
    "average_precision",
    "benchmark",
    "build_brute_force",
    "build_hnsw",
    "build_quantized",
    "build_sharded",
    "compare",
    "deserialize_index",
    "exact_ground_truth",
    "fuse_scores",
    "kmeans",
    "load_index",
    "merge_dedup_keep_closest",
    "merge_results",
    "metric_of",
    "ndcg",
    "normalize",
    "random_unit_vectors",
    "rebalance_plan",
    "recall_at_k",
    "run_demo",
    "save_index",
    "serialize_index",
    "tokenize",
]
