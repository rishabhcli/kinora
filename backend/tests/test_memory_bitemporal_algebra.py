"""Pure bitemporal algebra + graph reasoning + retrieval tests (no infra, §8).

Exercises the offline cores the bitemporal engine builds on: the half-open interval algebra
and the Allen relations, the canon graph (reachability / shortest path / neighborhood),
contradiction detection (the Critic's §9.5 timeline candidates), and the retrieval math
(cosine / MMR diversity / hybrid scoring / budget packing).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.memory.bitemporal import (
    Allen,
    BeatInterval,
    BitemporalCoord,
    TxInterval,
)
from app.memory.contracts import BeatSpan, BitemporalFact, TxSpan, WriteStamp
from app.memory.graph_reasoning import CanonGraph, find_contradictions
from app.memory.retrieval import (
    Packable,
    Scored,
    cosine,
    estimate_tokens,
    hybrid_score,
    mmr_rerank,
    pack_under_budget,
)

# --- interval algebra -------------------------------------------------------- #


def test_beat_interval_half_open_containment() -> None:
    iv = BeatInterval(10, 20)
    assert iv.contains(10)  # inclusive lower
    assert iv.contains(19)
    assert not iv.contains(20)  # exclusive upper (half-open)
    assert not iv.contains(9)
    open_iv = BeatInterval(10)  # open-ended
    assert open_iv.contains(10) and open_iv.contains(10_000)


def test_beat_interval_rejects_inverted() -> None:
    import pytest

    with pytest.raises(ValueError):
        BeatInterval(20, 10)


def test_allen_relations() -> None:
    # meets: [10,20) then [20,30) — the clean supersession a correction produces.
    assert BeatInterval(10, 20).relation(BeatInterval(20, 30)) is Allen.MEETS
    assert BeatInterval(10, 20).relation(BeatInterval(30, 40)) is Allen.BEFORE
    assert BeatInterval(10, 30).relation(BeatInterval(15, 20)) is Allen.CONTAINS
    assert BeatInterval(15, 20).relation(BeatInterval(10, 30)) is Allen.DURING
    assert BeatInterval(10, 20).relation(BeatInterval(10, 20)) is Allen.EQUALS
    assert BeatInterval(10, 25).relation(BeatInterval(20, 30)) is Allen.OVERLAPS


def test_tx_interval_contains_and_normalizes_tz() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(hours=1)
    iv = TxInterval(t0, t1)
    assert iv.contains(t0)
    assert iv.contains(t0 + timedelta(minutes=30))
    assert not iv.contains(t1)  # half-open
    # Naive datetime is coerced to UTC.
    naive = datetime(2026, 1, 1, 0, 30)
    assert iv.contains(naive)
    # Open interval = still believed.
    assert TxInterval(t0).contains(t0 + timedelta(days=999))


def test_bitemporal_coord_defaults_to_latest_now() -> None:
    coord = BitemporalCoord.now()
    assert coord.branch == "main"
    assert coord.as_of_tx is None
    assert coord.tx_instant().tzinfo is not None


# --- graph reasoning --------------------------------------------------------- #


def _fact(subject: str, predicate: str, obj: str, key: str | None = None) -> BitemporalFact:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    return BitemporalFact(
        id=key or f"{subject}-{predicate}-{obj}",
        fact_key=key or f"fk_{subject}_{predicate}_{obj}",
        subject_entity_key=subject,
        predicate=predicate,
        object_value=obj,
        valid=BeatSpan(valid_from_beat=1),
        tx=TxSpan(tx_from=t0),
        stamp=WriteStamp(wall=1, counter=0, actor_id="system"),
    )


def test_graph_reachability_and_shortest_path() -> None:
    graph = CanonGraph.from_facts(
        [
            _fact("hero", "travels_to", "castle"),
            _fact("castle", "contains", "throne_room"),
            _fact("throne_room", "holds", "crown"),
            _fact("villain", "covets", "crown"),
        ]
    )
    assert "crown" in graph.reachable("hero")
    assert "crown" not in graph.reachable("hero", max_hops=2)  # 3 hops away
    path = graph.shortest_path("hero", "crown")
    assert path is not None
    assert [e.object for e in path] == ["castle", "throne_room", "crown"]
    assert graph.shortest_path("crown", "hero") is None  # directed: no reverse edge


def test_graph_neighborhood_is_a_local_slice() -> None:
    graph = CanonGraph.from_facts(
        [
            _fact("hero", "wields", "sword"),
            _fact("hero", "wears", "cloak"),
            _fact("sword", "forged_in", "volcano"),  # 2 hops from hero
        ]
    )
    one_hop = graph.neighborhood("hero", hops=1)
    objs = {e.object for e in one_hop.edges}
    assert objs == {"sword", "cloak"}  # volcano excluded at 1 hop
    two_hop = graph.neighborhood("hero", hops=2)
    assert "volcano" in {e.object for e in two_hop.edges}


def test_find_functional_contradiction() -> None:
    # A character cannot be located_at two places at once.
    facts = [
        _fact("hero", "located_at", "castle", key="fk1"),
        _fact("hero", "located_at", "forest", key="fk2"),
        _fact("hero", "wields", "sword", key="fk3"),  # not functional → fine
    ]
    contradictions = find_contradictions(facts)
    assert len(contradictions) == 1
    c = contradictions[0]
    assert c.subject == "hero" and c.predicate == "located_at"
    assert {c.object_a, c.object_b} == {"castle", "forest"}


def test_find_mutually_exclusive_contradiction() -> None:
    facts = [
        _fact("king", "alive", "true", key="a"),
        _fact("king", "dead", "true", key="d"),
    ]
    contradictions = find_contradictions(facts, mutually_exclusive=[("alive", "dead")])
    assert any("mutually-exclusive" in c.reason for c in contradictions)


# --- retrieval math ---------------------------------------------------------- #


def test_cosine_basic() -> None:
    assert cosine([1, 0, 0], [1, 0, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0  # zero vector


def test_mmr_prefers_diversity_over_near_duplicates() -> None:
    query = [1.0, 0.0, 0.0]
    # Two near-identical high-relevance items + one slightly-less-relevant but diverse one.
    candidates = [
        Scored(item="dupe_a", score=0.95, vector=[1.0, 0.0, 0.0]),
        Scored(item="dupe_b", score=0.94, vector=[0.99, 0.01, 0.0]),
        Scored(item="diverse", score=0.80, vector=[0.0, 1.0, 0.0]),
    ]
    # Pure relevance top-2 would pick both dupes; MMR with diversity should swap one out.
    picked = [s.item for s in mmr_rerank(query, candidates, k=2, lambda_=0.5)]
    assert picked[0] == "dupe_a"  # most relevant first
    assert "diverse" in picked  # diversity wins the 2nd slot over the near-dupe


def test_mmr_lambda_one_is_pure_relevance() -> None:
    query = [1.0, 0.0]
    candidates = [
        Scored(item="a", score=0.9, vector=[1.0, 0.0]),
        Scored(item="b", score=0.8, vector=[0.99, 0.0]),
        Scored(item="c", score=0.7, vector=[0.0, 1.0]),
    ]
    picked = [s.item for s in mmr_rerank(query, candidates, k=3, lambda_=1.0)]
    assert picked == ["a", "b", "c"]  # exactly relevance order


def test_hybrid_score_blends_dense_and_sparse() -> None:
    qv, cv = [1.0, 0.0], [1.0, 0.0]
    # Identical vectors + identical text → max on both signals.
    high = hybrid_score(qv, cv, "snow queen ice", "snow queen ice", alpha=0.5)
    # Identical vectors but disjoint text → dense max, sparse 0.
    mid = hybrid_score(qv, cv, "snow queen ice", "warm desert sun", alpha=0.5)
    assert high > mid


def test_pack_under_budget_takes_highest_density() -> None:
    items = [
        Packable(item="cheap_good", value=8.0, tokens=2),   # density 4.0
        Packable(item="pricey", value=10.0, tokens=10),     # density 1.0
        Packable(item="filler", value=3.0, tokens=1),       # density 3.0
    ]
    chosen = {p.item for p in pack_under_budget(items, token_budget=3)}
    # Budget 3 → take cheap_good(2) + filler(1) = 11 value > pricey alone.
    assert chosen == {"cheap_good", "filler"}


def test_estimate_tokens_is_positive() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 40) == 10


# --- bitemporal vault rendering (pure) --------------------------------------- #


def test_bitemporal_vault_renders_all_sections() -> None:
    from app.memory.bitemporal_vault import BitemporalVault
    from app.memory.contracts import AuditChain, AuditEntry, BranchInfo, FactHistory

    facts = [_fact("hero", "hair", "silver", key="fk_hair")]
    histories = [
        FactHistory(
            fact_key="fk_hair",
            book_id="b1",
            branch="main",
            beliefs=[
                _fact("hero", "hair", "black", key="fk_hair"),
                _fact("hero", "hair", "silver", key="fk_hair"),
            ],
        )
    ]
    branches = [
        BranchInfo(id="x", book_id="b1", name="director_cut", parent="main", status="open")
    ]
    audit = AuditChain(
        book_id="b1",
        intact=True,
        entries=[
            AuditEntry(
                id="a1",
                seq=1,
                book_id="b1",
                branch="main",
                action="assert_fact",
                actor_id="director",
                target_key="fk_hair",
                entry_hash="deadbeef",
            )
        ],
    )
    doc = BitemporalVault().render(
        book_id="b1",
        branch="main",
        facts=facts,
        branches=branches,
        histories=histories,
        audit=audit,
    )
    md = doc.markdown
    assert "Active canon facts" in md
    assert "director_cut" in md  # branch table
    assert "fk_hair" in md  # history section
    assert "black" in md and "silver" in md  # both beliefs in the tx-history
    assert "intact" in md  # audit status
    assert set(doc.sections) == {"active", "branches", "history", "audit"}


def test_bitemporal_vault_flags_broken_audit() -> None:
    from app.memory.bitemporal_vault import BitemporalVault
    from app.memory.contracts import AuditChain

    doc = BitemporalVault().render(
        book_id="b1",
        branch="main",
        facts=[],
        branches=[],
        histories=[],
        audit=AuditChain(book_id="b1", intact=False, broken_at_seq=3),
    )
    assert "BROKEN at seq 3" in doc.sections["audit"]
