"""Identity store: same-character verdicts, best-reference, versioning, isolation."""

from __future__ import annotations

import pytest

from app.embeddings.config import EmbeddingStoreSettings
from app.embeddings.embedder import FakeEmbedder, perturb
from app.embeddings.identity import IdentityStore, Verdict
from app.embeddings.index import InMemoryVectorIndex
from app.embeddings.models import EntityKind
from app.embeddings.vectors import EmbeddingVector, VectorSpace

SPACE = VectorSpace(provider="p", model="m", dimension=32, version=1)


def make_store() -> IdentityStore:
    cfg = EmbeddingStoreSettings(
        model="m",
        dimension=32,
        match_threshold=0.82,
        reject_threshold=0.62,
        admit_min_similarity=0.55,
    )
    return IdentityStore(InMemoryVectorIndex(), FakeEmbedder(SPACE, seed=1), cfg)


def unit(values: list[float]) -> EmbeddingVector:
    return EmbeddingVector.create(SPACE, values)


def axis(i: int, dim: int = 32) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


async def seed_elsa(store: IdentityStore) -> EmbeddingVector:
    base = unit(axis(0))
    await store.add_reference(
        ref_id="elsa_ref1",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=base,
        descriptor="auburn braid, ice-blue dress",
        pose_tags=["front"],
        locked=True,
        enforce_admission=False,
    )
    return base


async def test_verify_match_uncertain_reject() -> None:
    store = make_store()
    base = await seed_elsa(store)

    # Near-identical frame -> MATCH.
    near = perturb(base, amount=0.05)
    m = await store.verify(book_id="book1", entity_key="char_elsa", frame_vector=near)
    assert m.verdict is Verdict.MATCH
    assert m.is_match
    assert m.best_reference is not None and m.best_reference.ref_id == "elsa_ref1"
    assert m.score >= 0.82

    # Mid-similarity -> UNCERTAIN (between thresholds).
    mid = unit([0.75, 0.66] + [0.0] * 30)  # cosine to axis(0) ~ 0.75
    u = await store.verify(book_id="book1", entity_key="char_elsa", frame_vector=mid)
    assert u.verdict is Verdict.UNCERTAIN
    assert 0.62 <= u.score < 0.82

    # Orthogonal frame -> REJECT.
    far = unit(axis(5))
    r = await store.verify(book_id="book1", entity_key="char_elsa", frame_vector=far)
    assert r.verdict is Verdict.REJECT
    assert r.score < 0.62


async def test_verify_no_references_is_uncertain() -> None:
    store = make_store()
    v = await store.verify(book_id="book1", entity_key="unknown", frame_vector=unit(axis(0)))
    assert v.verdict is Verdict.UNCERTAIN
    assert v.best_reference is None
    assert v.candidates == ()


async def test_namespace_isolation_between_entities_and_books() -> None:
    store = make_store()
    await seed_elsa(store)
    # A different character whose only reference is axis(7).
    await store.add_reference(
        ref_id="anna_ref1",
        entity_key="char_anna",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=unit(axis(7)),
        enforce_admission=False,
    )
    # Verifying an elsa-like frame against anna must NOT see the elsa reference.
    res = await store.verify(
        book_id="book1", entity_key="char_anna", frame_vector=perturb(unit(axis(0)), amount=0.05)
    )
    assert res.verdict is Verdict.REJECT  # only anna's axis(7) ref is in scope

    # Same entity_key in another book is also isolated.
    res2 = await store.verify(
        book_id="book2", entity_key="char_elsa", frame_vector=unit(axis(0))
    )
    assert res2.best_reference is None


async def test_best_reference_by_pose_then_query() -> None:
    store = make_store()
    await seed_elsa(store)  # ref1: pose front, axis(0)
    await store.add_reference(
        ref_id="elsa_profile",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=perturb(unit(axis(0)), amount=0.1),
        pose_tags=["profile"],
    )
    # Ask for a profile reference -> must return the profile-tagged one.
    prof = await store.best_reference(book_id="book1", entity_key="char_elsa", pose="profile")
    assert prof is not None and prof.ref_id == "elsa_profile"

    # Ask with a query close to ref1 -> ranks by similarity.
    best = await store.best_reference(
        book_id="book1", entity_key="char_elsa", query_vector=unit(axis(0))
    )
    assert best is not None and best.ref_id == "elsa_ref1"


async def test_best_reference_no_query_picks_newest_version() -> None:
    store = make_store()
    await seed_elsa(store)  # version defaults to 1
    await store.add_reference(
        ref_id="elsa_v3",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=perturb(unit(axis(0)), amount=0.05),
        version=3,
    )
    best = await store.best_reference(book_id="book1", entity_key="char_elsa")
    assert best is not None and best.ref_id == "elsa_v3"  # highest version wins


async def test_admission_control_rejects_off_character_reference() -> None:
    store = make_store()
    await seed_elsa(store)
    with pytest.raises(ValueError):
        await store.add_reference(
            ref_id="bad",
            entity_key="char_elsa",
            book_id="book1",
            kind=EntityKind.CHARACTER,
            vector=unit(axis(20)),  # orthogonal to the locked reference
            enforce_admission=True,
        )
    # A sufficiently-similar reference is admitted.
    ok = await store.add_reference(
        ref_id="good",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=perturb(unit(axis(0)), amount=0.1),
        enforce_admission=True,
    )
    assert ok.ref_id == "good"


async def test_list_references_and_remove() -> None:
    store = make_store()
    await seed_elsa(store)
    await store.add_reference(
        ref_id="elsa_v2",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=perturb(unit(axis(0)), amount=0.05),
        version=2,
    )
    refs = await store.list_references(book_id="book1", entity_key="char_elsa")
    assert [r.ref_id for r in refs] == ["elsa_v2", "elsa_ref1"]  # newest version first
    only_v2 = await store.list_references(book_id="book1", entity_key="char_elsa", version=2)
    assert [r.ref_id for r in only_v2] == ["elsa_v2"]

    assert await store.remove_reference(book_id="book1", entity_key="char_elsa", ref_id="elsa_v2")
    remaining = await store.list_references(book_id="book1", entity_key="char_elsa")
    assert [r.ref_id for r in remaining] == ["elsa_ref1"]


async def test_verify_from_image_bytes_uses_embedder() -> None:
    store = make_store()
    # Seed by bytes through the embedder, then verify the same bytes -> MATCH.
    embedder = FakeEmbedder(SPACE, seed=1)
    [base] = await embedder.embed_images([b"elsa_frame"])
    await store.add_reference(
        ref_id="ref",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        image_bytes=b"elsa_frame",
        enforce_admission=False,
    )
    m = await store.verify(book_id="book1", entity_key="char_elsa", frame_bytes=b"elsa_frame")
    assert m.verdict is Verdict.MATCH
    assert m.score == pytest.approx(base.cosine(base), abs=1e-9)
