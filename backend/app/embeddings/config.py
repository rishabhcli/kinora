"""Additive settings for the embeddings / identity vector store (v2).

These are *additive*: the subsystem reads its own thresholds from this model and
inherits the canonical model id + dimension from the existing
:class:`app.core.config.Settings` (``embed_model_image`` / ``embed_dim``) via
:meth:`from_settings`. Nothing here changes the round-1 embedding path.

The defaults are deliberately conservative so that, off the shelf, the identity
verdict matches the round-1 Critic intent (high-similarity == same character)
without any tuning.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EmbeddingStoreSettings(BaseModel):
    """Tunables for the vector store, identity matching, and the cache."""

    # --- Canonical space identity (defaults mirror app.core.config) ---
    provider: str = "dashscope"
    model: str = "tongyi-embedding-vision-plus"
    dimension: int = 1152
    #: Local monotonic space version; bump to force a re-embed migration.
    space_version: int = 1

    # --- Identity match verdict thresholds (cosine, on unit vectors) ---
    #: At/above this the new frame is confidently the SAME entity.
    match_threshold: float = Field(default=0.82, ge=-1.0, le=1.0)
    #: Below this it is confidently a DIFFERENT entity.
    reject_threshold: float = Field(default=0.62, ge=-1.0, le=1.0)
    #: A new reference is only auto-admitted into an identity when at least this
    #: similar to an existing reference (guards against drift / wrong uploads).
    admit_min_similarity: float = Field(default=0.55, ge=-1.0, le=1.0)

    # --- k-NN defaults ---
    default_top_k: int = Field(default=8, ge=1)

    # --- Cache ---
    cache_enabled: bool = True
    #: 0 disables eviction (unbounded); otherwise a simple LRU cap.
    cache_max_entries: int = Field(default=10_000, ge=0)

    def model_post_init(self, __context: Any) -> None:  # noqa: D401, N807
        if self.reject_threshold > self.match_threshold:
            raise ValueError(
                "reject_threshold must be <= match_threshold "
                f"({self.reject_threshold} > {self.match_threshold})"
            )

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> EmbeddingStoreSettings:
        """Build from the app :class:`Settings`, inheriting model id + dimension.

        ``settings`` is duck-typed (anything exposing ``embed_model_image`` and
        ``embed_dim``) so tests can pass a tiny stub without importing the full
        Settings object.
        """
        base: dict[str, Any] = {}
        model = getattr(settings, "embed_model_image", None)
        if model:
            base["model"] = str(model)
        dim = getattr(settings, "embed_dim", None)
        if dim:
            base["dimension"] = int(dim)
        base.update(overrides)
        return cls(**base)


__all__ = ["EmbeddingStoreSettings"]
