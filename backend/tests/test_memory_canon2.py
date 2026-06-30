"""Deterministic tests for the canon2 deepened canon memory (kinora.md §8, §7.2, §9.5).

Zero infra, zero network, zero credits: a seeded bag-of-words embedder makes cosine
track lexical overlap, the store is in-memory, and every clock is injected. Covers:

* **versioning + time-travel** — append-only revisions, field diffs, "the canon as
  of page N", and belief-time (``as_of_tx``) reads;
* **conflict merge** — the four §7.2 policy branches (evolve / flag user-facing /
  flag both-grounded / honor LWW) and deterministic last-writer-wins;
* **hybrid retrieval** — relevance ranking blends dense+sparse, and near-duplicate
  facts are deduped so k slots aren't wasted;
* **consistency audit** — planted contradictions, unexplained drift, dangling
  references, and unresolved conflicts are all caught;
* **tool dispatch** — the ``canon2.*`` surface routes through the same dispatch
  contract, and ``mount_on`` adds it to an existing dispatcher additively.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.memory.canon2 import (
    Canon2Kind,
    Canon2Store,
    Canon2Tools,
    CanonFact,
    CanonRetriever,
    ConflictPolicy,
    ConsistencyAuditor,
    EntityHistory,
    Proposal,
    Provenance,
    Revision,
    Severity,
    diff_attributes,
    resolve,
    revision_as_of_beat,
    revision_as_of_tx,
)
from app.memory.canon2.tools import CANON2_TOOLS_BY_NAME, mount_on
from app.memory.contracts import BeatSpan, BitemporalFact, TxSpan, WriteStamp

_DIM = 1152


class FakeEmbedder:
    """Seeded bag-of-words embedder: cosine tracks shared tokens (no live model)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._vec(b.decode("utf-8", "ignore")) for b in images]

    @staticmethod
    def _vec(text: str) -> list[float]:
        vec = [0.0] * _DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            axis = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:4], "big") % _DIM
            vec[axis] += 1.0
        return vec


def _bf(
    subject: str, predicate: str, obj: str, *, key: str | None = None, beat: int = 0
) -> BitemporalFact:
    """A minimal active BitemporalFact for the auditor/graph tests."""
    fk = key or f"f_{subject}:{predicate}:{obj}"
    return BitemporalFact(
        id=fk,
        fact_key=fk,
        branch="main",
        subject_entity_key=subject,
        predicate=predicate,
        object_value=obj,
        valid=BeatSpan(valid_from_beat=beat, valid_to_beat=None),
        tx=TxSpan(tx_from=datetime(2026, 1, 1, tzinfo=UTC), tx_to=None),
        stamp=WriteStamp(wall=0, counter=0, actor_id="t"),
        current=True,
    )


# --------------------------------------------------------------------------- #
# Versioning + diffs + time-travel
# --------------------------------------------------------------------------- #


def _rev(seq: int, beat: int, *, appearance: dict, tx: datetime) -> Revision:
    return Revision(
        entity_key="hero",
        book_id="b1",
        branch="main",
        kind=Canon2Kind.CHARACTER,
        seq=seq,
        version=seq,
        valid_from_beat=beat,
        tx_at=tx,
        name="Hero",
        appearance=appearance,
    )


def test_diff_attributes_classifies_added_removed_changed() -> None:
    before = {"name": "Hero", "appearance": {"hair": "black"}, "aliases": ["H"]}
    after = {"name": "Hero", "appearance": {"hair": "white"}, "aliases": []}
    deltas = {d.field: d.change for d in diff_attributes(before, after)}
    assert deltas == {"appearance": "changed", "aliases": "removed"}


def test_diff_genesis_marks_present_fields_added() -> None:
    deltas = diff_attributes(None, {"name": "Hero", "appearance": {"hair": "black"}})
    by_field = {d.field: d.change for d in deltas}
    assert by_field["name"] == "added"
    assert by_field["appearance"] == "added"


def test_revision_as_of_beat_resolves_latest_valid_revision() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    hist = EntityHistory(
        entity_key="hero",
        book_id="b1",
        branch="main",
        kind=Canon2Kind.CHARACTER,
        revisions=[
            _rev(1, 1, appearance={"hair": "black"}, tx=t0),
            _rev(2, 10, appearance={"hair": "white"}, tx=t0 + timedelta(hours=1)),
        ],
    )
    assert revision_as_of_beat(hist, 0) is None  # before the entity existed
    assert revision_as_of_beat(hist, 5).appearance == {"hair": "black"}
    assert revision_as_of_beat(hist, 10).appearance == {"hair": "white"}
    assert revision_as_of_beat(hist, 999).appearance == {"hair": "white"}


def test_revision_as_of_tx_reads_what_the_canon_believed_at_an_instant() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    hist = EntityHistory(
        entity_key="hero",
        book_id="b1",
        branch="main",
        kind=Canon2Kind.CHARACTER,
        revisions=[
            _rev(1, 1, appearance={"hair": "black"}, tx=t0),
            _rev(2, 1, appearance={"hair": "white"}, tx=t1),
        ],
    )
    # Both revisions are valid at beat 1, but the *belief* differs by tx.
    assert revision_as_of_tx(hist, t0 + timedelta(minutes=30)).appearance == {"hair": "black"}
    assert revision_as_of_tx(hist, t1 + timedelta(minutes=30)).appearance == {"hair": "white"}
    assert revision_as_of_tx(hist, t0 - timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_store_upsert_is_append_only_with_diffs_and_time_travel() -> None:
    store = Canon2Store(FakeEmbedder())
    r1 = await store.upsert_entity(
        book_id="b1", entity_key="hero", kind=Canon2Kind.CHARACTER, name="Hero",
        valid_from_beat=1, appearance={"hair": "black"},
    )
    r2 = await store.upsert_entity(
        book_id="b1", entity_key="hero", kind=Canon2Kind.CHARACTER, name="Hero",
        valid_from_beat=10, appearance={"hair": "white"},
        provenance=Provenance(actor_id="cs", reason="aged", source_span={"page": 12}),
    )
    assert (r1.seq, r2.seq) == (1, 2)
    assert r1.is_genesis and not r2.is_genesis
    assert [(d.field, d.change) for d in r2.deltas] == [("appearance", "changed")]
    # Prior revision is preserved (append-only): time-travel still sees it.
    assert store.get_entity(book_id="b1", entity_key="hero", at_beat=5).appearance == {
        "hair": "black"
    }
    assert store.get_entity(book_id="b1", entity_key="hero").appearance == {"hair": "white"}
    hist = store.history(book_id="b1", entity_key="hero")
    assert len(hist.revisions) == 2


# --------------------------------------------------------------------------- #
# Conflict-resolution engine (§7.2)
# --------------------------------------------------------------------------- #


def test_conflict_evolve_when_only_incoming_is_grounded() -> None:
    existing = Proposal(subject="hero", predicate="possesses", object_value="sword")
    incoming = Proposal(
        subject="hero", predicate="possesses", object_value="shield",
        source_span={"page": 20},
    )
    res = resolve(incoming, existing)
    assert res.policy is ConflictPolicy.EVOLVE
    assert res.winner == "incoming" and res.winning_object == "shield"


def test_conflict_evolve_when_only_existing_is_grounded() -> None:
    existing = Proposal(
        subject="hero", predicate="possesses", object_value="sword",
        source_span={"page": 8},
    )
    incoming = Proposal(subject="hero", predicate="possesses", object_value="shield")
    res = resolve(incoming, existing)
    assert res.policy is ConflictPolicy.EVOLVE
    assert res.winner == "existing" and res.winning_object == "sword"


def test_conflict_user_facing_predicate_always_flags_even_if_grounded() -> None:
    existing = Proposal(subject="hero", predicate="status", object_value="alive")
    incoming = Proposal(
        subject="hero", predicate="status", object_value="dead",
        source_span={"page": 99},
    )
    res = resolve(incoming, existing)
    assert res.policy is ConflictPolicy.FLAG
    assert res.winner is None and res.winning_object is None


def test_conflict_both_grounded_flags_as_ambiguous() -> None:
    existing = Proposal(
        subject="hero", predicate="possesses", object_value="sword",
        source_span={"page": 8},
    )
    incoming = Proposal(
        subject="hero", predicate="possesses", object_value="shield",
        source_span={"page": 20},
    )
    res = resolve(incoming, existing)
    assert res.policy is ConflictPolicy.FLAG


def test_conflict_honor_lww_is_deterministic_by_stamp() -> None:
    # Neither grounded, not user-facing → last-writer-wins by CRDT stamp.
    early = Proposal(
        subject="hero", predicate="possesses", object_value="sword",
        actor_id="a", wall_ms=100,
    )
    late = Proposal(
        subject="hero", predicate="possesses", object_value="shield",
        actor_id="b", wall_ms=200,
    )
    forward = resolve(late, early)
    backward = resolve(early, late)
    # Whichever is invoked, the higher stamp ('shield' @200) wins → convergent.
    assert forward.winning_object == "shield"
    assert backward.winning_object == "shield"
    assert forward.policy is backward.policy is ConflictPolicy.HONOR


@pytest.mark.asyncio
async def test_store_propose_flags_then_resolve_applies_choice() -> None:
    store = Canon2Store(FakeEmbedder())
    await store.propose_fact(
        book_id="b1",
        proposal=Proposal(subject="hero", predicate="status", object_value="alive"),
    )
    decision = await store.propose_fact(
        book_id="b1",
        proposal=Proposal(
            subject="hero", predicate="status", object_value="dead",
            source_span={"page": 99},
        ),
        current_beat=39,
    )
    assert decision.policy is ConflictPolicy.FLAG
    queued = store.list_conflicts(book_id="b1")
    assert len(queued) == 1 and queued[0].conflict_id == decision.conflict_id
    assert queued[0].current_beat == 39
    # The active fact is untouched until arbitration.
    facts = {f.predicate: f.object_value for f in store.active_facts(book_id="b1")}
    assert facts["status"] == "alive"
    # Arbitrate → the choice becomes canon and the conflict closes.
    await store.resolve_conflict(
        book_id="b1", conflict_id=decision.conflict_id, chosen_object="dead",
        reasoning="director chose",
    )
    assert store.list_conflicts(book_id="b1") == []
    facts = {f.predicate: f.object_value for f in store.active_facts(book_id="b1")}
    assert facts["status"] == "dead"


# --------------------------------------------------------------------------- #
# Hybrid retrieval (ranking + dedup)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hybrid_retrieval_ranks_lexically_relevant_fact_first() -> None:
    retriever = CanonRetriever(FakeEmbedder())
    cands = [
        CanonFact(fact_key="f1", subject="hero", predicate="wields", object_value="shield"),
        CanonFact(fact_key="f2", subject="villain", predicate="lives_in", object_value="tower"),
        CanonFact(fact_key="f3", subject="hero", predicate="rides", object_value="horse"),
    ]
    out = await retriever.retrieve("hero shield", cands, k=3)
    assert out[0].fact.fact_key == "f1"  # shares 'hero' + 'shield'
    assert out[0].score >= out[-1].score


@pytest.mark.asyncio
async def test_hybrid_retrieval_dedups_near_identical_facts() -> None:
    retriever = CanonRetriever(FakeEmbedder())
    # f1 and f2 are an identical triple (different keys) → one is absorbed.
    cands = [
        CanonFact(fact_key="f1", subject="hero", predicate="wields", object_value="sword"),
        CanonFact(fact_key="f2", subject="hero", predicate="wields", object_value="sword"),
        CanonFact(fact_key="f3", subject="villain", predicate="lives_in", object_value="tower"),
    ]
    out = await retriever.retrieve("hero sword", cands, k=5)
    keys = {r.fact.fact_key for r in out}
    assert "f1" in keys and "f2" not in keys  # f2 deduped into f1
    rep = next(r for r in out if r.fact.fact_key == "f1")
    assert "f2" in rep.deduped


@pytest.mark.asyncio
async def test_store_retrieve_over_active_facts() -> None:
    store = Canon2Store(FakeEmbedder())
    await store.propose_fact(
        book_id="b1",
        proposal=Proposal(subject="hero", predicate="wields", object_value="shield"),
    )
    await store.propose_fact(
        book_id="b1",
        proposal=Proposal(subject="villain", predicate="lives_in", object_value="tower"),
    )
    out = await store.retrieve(book_id="b1", query="hero shield", k=2)
    assert out[0].fact.subject == "hero"


# --------------------------------------------------------------------------- #
# Consistency auditor (§9.5 + drift)
# --------------------------------------------------------------------------- #


def test_audit_catches_planted_functional_contradiction() -> None:
    auditor = ConsistencyAuditor()
    facts = [
        _bf("hero", "located_at", "castle", key="a"),
        _bf("hero", "located_at", "forest", key="b"),
    ]
    report = auditor.audit(book_id="b1", facts=facts)
    contradictions = report.by_kind("contradiction")
    assert not report.ok
    assert len(contradictions) == 1
    assert set(contradictions[0].refs) == {"a", "b"}


def test_audit_catches_mutually_exclusive_predicates() -> None:
    auditor = ConsistencyAuditor()
    facts = [_bf("hero", "alive", "true", key="a"), _bf("hero", "dead", "true", key="b")]
    report = auditor.audit(
        book_id="b1", facts=facts, mutually_exclusive=[("alive", "dead")]
    )
    assert report.by_kind("contradiction")


def test_audit_flags_unexplained_appearance_drift_but_not_grounded_change() -> None:
    auditor = ConsistencyAuditor()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    ungrounded = EntityHistory(
        entity_key="hero", book_id="b1", branch="main", kind=Canon2Kind.CHARACTER,
        revisions=[
            _rev(1, 1, appearance={"hair": "black"}, tx=t0),
            _rev(2, 10, appearance={"hair": "white"}, tx=t0 + timedelta(hours=1)),
        ],
    )
    report = auditor.audit(book_id="b1", histories=[ungrounded])
    drift = report.by_kind("drift")
    assert len(drift) == 1 and drift[0].predicate == "appearance"
    assert drift[0].severity is Severity.WARNING

    # The same change, but grounded with a reason → NOT flagged as drift.
    grounded_rev2 = _rev(2, 10, appearance={"hair": "white"}, tx=t0 + timedelta(hours=1))
    grounded_rev2 = grounded_rev2.model_copy(
        update={"provenance": Provenance(reason="aged in the storm")}
    )
    grounded = ungrounded.model_copy(
        update={"revisions": [ungrounded.revisions[0], grounded_rev2]}
    )
    assert not auditor.audit(book_id="b1", histories=[grounded]).by_kind("drift")


def test_audit_flags_dangling_reference_and_unresolved_conflict() -> None:
    auditor = ConsistencyAuditor()
    hist = EntityHistory(
        entity_key="hero", book_id="b1", branch="main", kind=Canon2Kind.CHARACTER,
        revisions=[_rev(1, 1, appearance={"hair": "black"}, tx=datetime(2026, 1, 1, tzinfo=UTC))],
    )
    facts = [_bf("ghost", "haunts", "castle", key="g")]  # 'ghost' is unknown
    report = auditor.audit(book_id="b1", facts=facts, histories=[hist])
    assert report.by_kind("dangling_reference")


@pytest.mark.asyncio
async def test_store_audit_reports_unresolved_conflict_as_error() -> None:
    store = Canon2Store(FakeEmbedder())
    await store.propose_fact(
        book_id="b1",
        proposal=Proposal(subject="hero", predicate="status", object_value="alive"),
    )
    await store.propose_fact(
        book_id="b1",
        proposal=Proposal(
            subject="hero", predicate="status", object_value="dead",
            source_span={"page": 99},
        ),
    )
    report = store.audit(book_id="b1")
    assert not report.ok
    assert report.by_kind("unresolved_conflict")


# --------------------------------------------------------------------------- #
# Tool dispatch + additive integration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dispatch_routes_validated_args_to_handler() -> None:
    tools = Canon2Tools(embedder=FakeEmbedder())
    rev = await tools.dispatch(
        "canon2.upsert_entity",
        {
            "book_id": "b1", "entity_key": "hero", "type": "character",
            "name": "Hero", "valid_from_beat": 1, "appearance": {"hair": "black"},
        },
    )
    assert isinstance(rev, Revision) and rev.version == 1
    got = await tools.dispatch(
        "canon2.get_entity", {"book_id": "b1", "entity_key": "hero", "at_beat": 5}
    )
    assert got.found and got.revision.name == "Hero"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_raises() -> None:
    tools = Canon2Tools(embedder=FakeEmbedder())
    with pytest.raises(ValueError, match="unknown tool"):
        await tools.dispatch("canon2.nope", {})


@pytest.mark.asyncio
async def test_mount_on_routes_canon2_and_delegates_rest() -> None:
    seen: list[str] = []

    class Base:
        async def dispatch(self, name: str, arguments: dict[str, object]) -> object:
            seen.append(name)
            return ("base", name)

    merged = mount_on(Base(), embedder=FakeEmbedder())
    # Existing tool → delegated to base untouched.
    assert await merged.dispatch("canon.query", {"book_id": "b1"}) == ("base", "canon.query")
    assert seen == ["canon.query"]
    # canon2 tool → handled by canon2, base never consulted.
    rev = await merged.dispatch(
        "canon2.upsert_entity",
        {"book_id": "b1", "entity_key": "h", "type": "character",
         "name": "H", "valid_from_beat": 1},
    )
    assert isinstance(rev, Revision)
    assert seen == ["canon.query"]  # base still only saw the first call


def test_tool_defs_have_unique_namespaced_names() -> None:
    names = [d.name for d in CANON2_TOOLS_BY_NAME.values()]
    assert all(n.startswith("canon2.") for n in names)
    assert len(names) == len(set(names))
