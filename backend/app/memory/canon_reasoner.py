"""Query-time reasoning façade over the bitemporal canon (kinora.md §8, §9.5).

Binds the three pure layers — bitemporal reads (:mod:`temporal_state_service`), graph
reasoning (:mod:`graph_reasoning`), and semantic retrieval (:mod:`retrieval`) — into a small
set of high-level answers the agents actually ask for:

* ``neighborhood_as_of`` — the relationship sub-graph around an entity, resolved at a
  bitemporal coordinate (a structural ``canon.query``).
* ``contradictions_as_of`` — the active facts that cannot co-hold at a coordinate (the
  Critic's §9.5 timeline-check candidates), so a contradiction is caught *before* a render.
* ``rerank_episodic`` — MMR + hybrid re-rank of episodic candidates so "what worked before"
  recall is relevant *and* diverse under the context budget (§8.2, §8.4).

The façade is thin: it does the read, hands the snapshot to the pure functions, and returns
typed results. It holds no DB query logic of its own beyond delegating to the temporal
service, so it stays cheap to test.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from app.memory.bitemporal import MAIN_BRANCH
from app.memory.contracts import BitemporalFact
from app.memory.graph_reasoning import (
    CanonGraph,
    Contradiction,
    Edge,
    find_contradictions,
)
from app.memory.retrieval import Scored, hybrid_score, mmr_rerank
from app.memory.temporal_state_service import TemporalStateService


class CanonReasoner:
    """High-level graph + retrieval reasoning over a bitemporal canon snapshot."""

    def __init__(self, temporal: TemporalStateService) -> None:
        self._temporal = temporal

    async def snapshot(
        self,
        *,
        book_id: str,
        beat: int,
        as_of_tx: datetime | None = None,
        branch: str = MAIN_BRANCH,
    ) -> list[BitemporalFact]:
        """The active fact set at a 4-D coordinate (the input to all reasoning below)."""
        return await self._temporal.as_of(
            book_id=book_id, beat=beat, as_of_tx=as_of_tx, branch=branch
        )

    async def graph_as_of(
        self,
        *,
        book_id: str,
        beat: int,
        as_of_tx: datetime | None = None,
        branch: str = MAIN_BRANCH,
    ) -> CanonGraph:
        """Build the relationship graph from the active snapshot."""
        facts = await self.snapshot(
            book_id=book_id, beat=beat, as_of_tx=as_of_tx, branch=branch
        )
        return CanonGraph.from_facts(facts)

    async def neighborhood_as_of(
        self,
        *,
        book_id: str,
        entity_key: str,
        beat: int,
        hops: int = 1,
        as_of_tx: datetime | None = None,
        branch: str = MAIN_BRANCH,
    ) -> list[Edge]:
        """The edges within ``hops`` of an entity at a coordinate (a structural slice)."""
        graph = await self.graph_as_of(
            book_id=book_id, beat=beat, as_of_tx=as_of_tx, branch=branch
        )
        return list(graph.neighborhood(entity_key, hops=hops).edges)

    async def contradictions_as_of(
        self,
        *,
        book_id: str,
        beat: int,
        as_of_tx: datetime | None = None,
        branch: str = MAIN_BRANCH,
        mutually_exclusive: Iterable[tuple[str, str]] = (),
    ) -> list[Contradiction]:
        """Active facts that cannot co-hold at the coordinate (§9.5 timeline candidates)."""
        facts = await self.snapshot(
            book_id=book_id, beat=beat, as_of_tx=as_of_tx, branch=branch
        )
        return find_contradictions(facts, mutually_exclusive=mutually_exclusive)

    @staticmethod
    def rerank_episodic(
        query_vec: Sequence[float],
        query_text: str,
        candidates: Sequence[tuple[str, Sequence[float], str]],
        *,
        k: int = 5,
        alpha: float = 0.7,
        lambda_: float = 0.6,
    ) -> list[str]:
        """Hybrid-score + MMR re-rank episodic candidates; return ids in final order.

        ``candidates`` are ``(id, embedding, text)`` triples (e.g. prior shots). The dense+
        sparse ``hybrid_score`` ranks relevance; MMR then diversifies the top-k so the recall
        the agent sees isn't k near-identical clips.
        """
        scored = [
            Scored(
                item=cid,
                score=hybrid_score(query_vec, vec, query_text, text, alpha=alpha),
                vector=list(vec),
            )
            for (cid, vec, text) in candidates
        ]
        reranked = mmr_rerank(query_vec, scored, k=k, lambda_=lambda_)
        return [s.item for s in reranked]


__all__ = ["CanonReasoner"]
