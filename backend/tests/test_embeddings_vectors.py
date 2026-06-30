"""Vector value type: dimension/version/space guarding + cosine arithmetic."""

from __future__ import annotations

import math

import pytest

from app.embeddings.vectors import (
    DimensionMismatch,
    EmbeddingVector,
    SpaceMismatch,
    VectorSpace,
    cosine,
    zero_vector,
)

SPACE_A = VectorSpace(provider="dashscope", model="tongyi", dimension=4, version=1)
SPACE_B = VectorSpace(provider="openai", model="te-3", dimension=4, version=1)
SPACE_A_V2 = VectorSpace(provider="dashscope", model="tongyi", dimension=4, version=2)


def test_space_key_is_stable_and_distinct() -> None:
    assert SPACE_A.key == "dashscope:tongyi:d4:v1"
    assert SPACE_A.key != SPACE_B.key
    assert SPACE_A.key != SPACE_A_V2.key
    assert SPACE_A.bumped() == SPACE_A_V2


def test_space_rejects_bad_dimension_and_version() -> None:
    with pytest.raises(ValueError):
        VectorSpace(provider="p", model="m", dimension=0)
    with pytest.raises(ValueError):
        VectorSpace(provider="p", model="m", dimension=4, version=0)


def test_create_normalizes_by_default() -> None:
    v = EmbeddingVector.create(SPACE_A, [3.0, 0.0, 4.0, 0.0])
    assert v.is_unit()
    assert math.isclose(math.sqrt(sum(x * x for x in v.values)), 1.0, abs_tol=1e-9)
    # Direction preserved: cosine with the raw direction is ~1.
    assert math.isclose(cosine([3.0, 0.0, 4.0, 0.0], list(v.values)), 1.0, abs_tol=1e-9)


def test_create_can_skip_normalization() -> None:
    v = EmbeddingVector.create(SPACE_A, [3.0, 0.0, 4.0, 0.0], normalize=False)
    assert not v.is_unit()


def test_dimension_mismatch_on_construction() -> None:
    with pytest.raises(DimensionMismatch):
        EmbeddingVector.create(SPACE_A, [1.0, 2.0])
    with pytest.raises(DimensionMismatch):
        EmbeddingVector(space=SPACE_A, values=(1.0, 2.0, 3.0))  # wrong arity


def test_cosine_requires_same_space() -> None:
    a = EmbeddingVector.create(SPACE_A, [1.0, 0.0, 0.0, 0.0])
    b = EmbeddingVector.create(SPACE_B, [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(SpaceMismatch):
        a.cosine(b)
    with pytest.raises(SpaceMismatch):
        a.dot(b)


def test_cosine_version_guard() -> None:
    a = EmbeddingVector.create(SPACE_A, [1.0, 0.0, 0.0, 0.0])
    a2 = EmbeddingVector.create(SPACE_A_V2, [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(SpaceMismatch):
        a.cosine(a2)


def test_cosine_values() -> None:
    a = EmbeddingVector.create(SPACE_A, [1.0, 0.0, 0.0, 0.0])
    same = EmbeddingVector.create(SPACE_A, [2.0, 0.0, 0.0, 0.0])
    orth = EmbeddingVector.create(SPACE_A, [0.0, 1.0, 0.0, 0.0])
    opp = EmbeddingVector.create(SPACE_A, [-1.0, 0.0, 0.0, 0.0])
    assert math.isclose(a.cosine(same), 1.0, abs_tol=1e-9)
    assert math.isclose(a.cosine(orth), 0.0, abs_tol=1e-9)
    assert math.isclose(a.cosine(opp), -1.0, abs_tol=1e-9)


def test_zero_vector_cosine_is_zero() -> None:
    z = zero_vector(SPACE_A)
    a = EmbeddingVector.create(SPACE_A, [1.0, 0.0, 0.0, 0.0])
    assert z.cosine(a) == 0.0
    assert z.is_unit()  # the zero vector is treated as a valid unit edge-case


def test_payload_roundtrip_preserves_space_and_values() -> None:
    v = EmbeddingVector.create(SPACE_A_V2, [0.1, 0.2, 0.3, 0.4])
    back = EmbeddingVector.from_payload(v.to_payload())
    assert back.space == v.space
    assert back.values == v.values


def test_raw_cosine_length_guard() -> None:
    with pytest.raises(DimensionMismatch):
        cosine([1.0, 2.0], [1.0, 2.0, 3.0])
