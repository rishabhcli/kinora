"""The canonical vector value type and its *space* metadata.

The single most important invariant in a multi-backend embedding store is that
**vectors from different embedders are never silently compared**. A cosine of
0.9 between a DashScope ``tongyi-vision`` vector and an OpenAI vector is
meaningless: they live in unrelated geometries. So every vector carries a
:class:`VectorSpace` — ``(provider, model, dimension, version)`` — and the
arithmetic helpers refuse to operate across spaces.

``version`` is *our* monotonic space version, bumped when the producing model is
swapped or re-tuned (even if provider+model+dim are unchanged), so a re-embed
migration can distinguish "old vectors" from "new vectors" purely by space
identity. See :mod:`app.embeddings.cache` for the migration path.

Vectors are stored L2-normalized (``cosine == dot``), matching the round-1
:mod:`app.providers.embeddings` convention so canon text and image/shot vectors
remain directly comparable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class SpaceMismatch(ValueError):  # noqa: N818 - domain-vocabulary name
    """Raised when an operation mixes vectors from two different spaces."""


class DimensionMismatch(ValueError):  # noqa: N818 - domain-vocabulary name
    """Raised when a raw vector's length does not match its declared space."""


@dataclass(frozen=True, slots=True)
class VectorSpace:
    """Identity of an embedding geometry: provider + model + dimension + version.

    Two vectors are comparable **iff** their spaces are equal. ``version`` is a
    local, monotonic integer that lets us invalidate/re-embed vectors when the
    producing model changes in a way the provider/model string would not capture
    (e.g. a silent server-side model update, or a deliberate re-tune).
    """

    provider: str
    model: str
    dimension: int
    version: int = 1

    def __post_init__(self) -> None:
        if self.dimension <= 0:
            raise ValueError(f"dimension must be positive, got {self.dimension}")
        if self.version <= 0:
            raise ValueError(f"version must be positive, got {self.version}")

    @property
    def key(self) -> str:
        """A short, stable, filesystem/redis-safe identifier for this space."""
        return f"{self.provider}:{self.model}:d{self.dimension}:v{self.version}"

    def bumped(self) -> VectorSpace:
        """Return the same space with ``version`` incremented (re-embed trigger)."""
        return VectorSpace(self.provider, self.model, self.dimension, self.version + 1)

    def matches(self, other: VectorSpace) -> bool:
        return self == other


def _l2_norm(values: Sequence[float]) -> float:
    return math.sqrt(math.fsum(x * x for x in values))


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """An immutable, space-stamped, (optionally) unit-normalized vector.

    Construct via :meth:`create` so the length is validated against the space and
    the vector is normalized once. The raw component list is exposed read-only
    via :attr:`values`.
    """

    space: VectorSpace
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.values) != self.space.dimension:
            raise DimensionMismatch(
                f"vector has {len(self.values)} dims but space {self.space.key} "
                f"declares {self.space.dimension}"
            )

    @classmethod
    def create(
        cls,
        space: VectorSpace,
        values: Sequence[float],
        *,
        normalize: bool = True,
    ) -> EmbeddingVector:
        """Build a vector, validating dimension and (by default) L2-normalizing.

        Normalizing here means downstream cosine reduces to a dot product and all
        stored vectors share one scale.
        """
        if len(values) != space.dimension:
            raise DimensionMismatch(
                f"got {len(values)} dims but space {space.key} declares {space.dimension}"
            )
        floats = [float(x) for x in values]
        if normalize:
            norm = _l2_norm(floats)
            if norm > 0.0:
                floats = [x / norm for x in floats]
        return cls(space=space, values=tuple(floats))

    @property
    def dimension(self) -> int:
        return self.space.dimension

    def is_unit(self, *, tol: float = 1e-6) -> bool:
        """True if the vector is (approximately) unit length, or is the zero vector."""
        norm = _l2_norm(self.values)
        return norm == 0.0 or abs(norm - 1.0) <= tol

    def require_same_space(self, other: EmbeddingVector) -> None:
        if not self.space.matches(other.space):
            raise SpaceMismatch(
                f"cannot operate across spaces: {self.space.key} vs {other.space.key}"
            )

    def dot(self, other: EmbeddingVector) -> float:
        """Dot product; requires the same space."""
        self.require_same_space(other)
        return math.fsum(a * b for a, b in zip(self.values, other.values, strict=True))

    def cosine(self, other: EmbeddingVector) -> float:
        """Cosine similarity; requires the same space.

        For unit vectors this equals :meth:`dot`, but we divide by the norms so
        the result is correct even if a caller stored un-normalized vectors.
        """
        self.require_same_space(other)
        return cosine(self.values, other.values)

    def to_payload(self) -> dict[str, Any]:
        """Serialize to a plain dict (for cache / index persistence)."""
        return {
            "space": {
                "provider": self.space.provider,
                "model": self.space.model,
                "dimension": self.space.dimension,
                "version": self.space.version,
            },
            "values": list(self.values),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EmbeddingVector:
        sp = payload["space"]
        space = VectorSpace(
            provider=str(sp["provider"]),
            model=str(sp["model"]),
            dimension=int(sp["dimension"]),
            version=int(sp["version"]),
        )
        # Already-stored vectors are assumed normalized; do not re-normalize.
        return cls(space=space, values=tuple(float(x) for x in payload["values"]))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Raw cosine similarity between two equal-length sequences.

    Mirrors :func:`app.providers.embeddings.cosine` so the two layers agree.
    """
    if len(a) != len(b):
        raise DimensionMismatch(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = _l2_norm(a)
    nb = _l2_norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# Re-exported for callers that build their own zero/identity vectors in tests.
def zero_vector(space: VectorSpace) -> EmbeddingVector:
    """A zero vector in ``space`` (cosine with anything is 0.0)."""
    return EmbeddingVector(space=space, values=tuple(0.0 for _ in range(space.dimension)))


__all__ = [
    "DimensionMismatch",
    "EmbeddingVector",
    "SpaceMismatch",
    "VectorSpace",
    "cosine",
    "zero_vector",
]
