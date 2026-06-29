"""Local, package-scoped configuration for the vector-search service.

Deliberately a plain dataclass with defaults aligned to the Kinora embedding
contract (1152-dim, cosine — see ``app.providers.embeddings.EMBED_DIM``). It is
*not* wired into ``app.core.config.Settings`` so this package stays strictly
additive; a composition root that wants to override defaults constructs a
:class:`VectorSearchConfig` and passes it to the service. :func:`from_mapping`
lets a caller hydrate it from any settings-like object without a hard import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import Metric

#: Mirror of ``app.providers.embeddings.EMBED_DIM`` without importing the
#: provider (keeps this package import-light and dependency-free). Kept in sync
#: intentionally; the service validates the actual dim it is given.
DEFAULT_EMBED_DIM = 1152


@dataclass(slots=True)
class VectorSearchConfig:
    """Tunables for a :class:`~app.datascale.vectorsearch.service.VectorSearchService`."""

    dim: int = DEFAULT_EMBED_DIM
    metric: Metric = Metric.COSINE
    #: Backend index kind: "hnsw" | "sharded" | "pq" | "sq" | "brute".
    backend: str = "hnsw"
    n_shards: int = 4
    # HNSW knobs
    m: int = 16
    ef_construction: int = 200
    ef_search: int = 64
    seed: int = 0
    # Quantization knobs (used when backend in {"pq","sq"})
    pq_m: int = 8
    pq_nbits: int = 8
    sq_bits: int = 8
    keep_originals: bool = True
    # Hybrid-search defaults
    default_alpha: float = 0.7  # weight on dense vs lexical
    keyword_field: str = "text"  # metadata field tokenised for keyword fusion

    def __post_init__(self) -> None:
        if self.dim <= 0:
            raise ValueError("dim must be positive")
        if self.backend not in ("hnsw", "sharded", "pq", "sq", "brute"):
            raise ValueError(f"unknown backend: {self.backend}")
        if not 0.0 <= self.default_alpha <= 1.0:
            raise ValueError("default_alpha must be in [0, 1]")

    @classmethod
    def from_mapping(cls, data: Any) -> VectorSearchConfig:
        """Build from any mapping / settings-ish object (best-effort, partial)."""

        def pick(key: str, default: Any) -> Any:
            if isinstance(data, dict):
                return data.get(key, default)
            return getattr(data, key, default)

        metric = pick("metric", Metric.COSINE)
        if isinstance(metric, str):
            metric = Metric(metric.lower())
        return cls(
            dim=int(pick("dim", pick("embed_dim", DEFAULT_EMBED_DIM))),
            metric=metric,
            backend=str(pick("backend", "hnsw")),
            n_shards=int(pick("n_shards", 4)),
            m=int(pick("m", 16)),
            ef_construction=int(pick("ef_construction", 200)),
            ef_search=int(pick("ef_search", 64)),
            seed=int(pick("seed", 0)),
            pq_m=int(pick("pq_m", 8)),
            pq_nbits=int(pick("pq_nbits", 8)),
            sq_bits=int(pick("sq_bits", 8)),
            keep_originals=bool(pick("keep_originals", True)),
            default_alpha=float(pick("default_alpha", 0.7)),
            keyword_field=str(pick("keyword_field", "text")),
        )


__all__ = ["DEFAULT_EMBED_DIM", "VectorSearchConfig"]
