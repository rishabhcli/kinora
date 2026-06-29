"""Exact + near-duplicate dedup for training examples.

Re-reads, replays, and repeated agent calls flood the trace store with examples
that mean the same thing; training on them double-counts a behaviour and biases
the model. This module collapses duplicates deterministically and keeps the
*best* representative of each cluster.

Two passes:

* **Exact dedup** (:func:`dedup_exact`) — collapse on
  :attr:`TraceExample.content_hash` (the canonical semantic hash that ignores
  provenance). O(n), order-preserving, the cheap default.
* **Near-dedup** (:func:`dedup_near`) — collapse examples whose *text* (input
  rendered + output) is near-identical, using MinHash over character k-shingles
  banded into an LSH index, then a Jaccard confirm. Catches "the cat sat" vs
  "the  cat sat" / trivially reworded outputs that exact dedup misses.

"Best representative" is chosen by a pluggable scorer (default: a QA-passed,
higher-reward, more-edited example wins — the most informative survivor), so the
survivor of a cluster is the one most worth training on. Pure + deterministic
(MinHash seeded from the shingle hash, no randomness).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from app.mlplatform.datasets.contracts import TraceExample, canonical_json

#: A scorer ranks an example's value as a cluster survivor (higher = keep).
ExampleScorer = Callable[[TraceExample], float]


def default_scorer(ex: TraceExample) -> float:
    """Rank survivors: QA-passed > higher reward > more director edits > newer.

    Director edits and a QA verdict are the rarest, most-informative signals, so
    an example carrying them beats a bare one even at equal reward.
    """
    score = 0.0
    if ex.qa is not None:
        score += 10.0 + (5.0 if ex.qa.passed else 0.0) + ex.qa.score
    if ex.reward is not None:
        score += ex.reward
    score += 2.0 * len(ex.director_edits)
    score += ex.created_at.timestamp() / 1e12  # tiebreak toward the newest
    return score


# --------------------------------------------------------------------------- #
# Exact dedup
# --------------------------------------------------------------------------- #


@dataclass
class DedupReport:
    """A tally of what a dedup pass removed."""

    seen: int = 0
    kept: int = 0
    exact_removed: int = 0
    near_removed: int = 0

    @property
    def removed(self) -> int:
        return self.exact_removed + self.near_removed

    def to_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "kept": self.kept,
            "exact_removed": self.exact_removed,
            "near_removed": self.near_removed,
            "removed": self.removed,
        }


def dedup_exact(
    examples: Sequence[TraceExample], *, scorer: ExampleScorer | None = None
) -> tuple[list[TraceExample], DedupReport]:
    """Collapse examples sharing a content hash, keeping the best per hash."""
    score = scorer or default_scorer
    best: dict[str, TraceExample] = {}
    order: list[str] = []
    report = DedupReport(seen=len(examples))
    for ex in examples:
        h = ex.content_hash
        if h not in best:
            best[h] = ex
            order.append(h)
        else:
            report.exact_removed += 1
            if score(ex) > score(best[h]):
                best[h] = ex
    kept = [best[h] for h in order]
    report.kept = len(kept)
    return kept, report


# --------------------------------------------------------------------------- #
# Near-duplicate dedup (MinHash + LSH)
# --------------------------------------------------------------------------- #


def _text_of(ex: TraceExample) -> str:
    """The text payload near-dedup compares (input rendered + output)."""
    return f"{canonical_json(dict(ex.input))}\n{ex.output}".lower()


def _shingles(text: str, k: int) -> set[str]:
    """Character k-shingles of a normalized string (whitespace collapsed)."""
    norm = " ".join(text.split())
    if len(norm) <= k:
        return {norm} if norm else set()
    return {norm[i : i + k] for i in range(len(norm) - k + 1)}


def _hash_shingle(shingle: str, seed: int) -> int:
    h = hashlib.blake2b(f"{seed}:{shingle}".encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def _minhash(shingles: set[str], num_perm: int) -> tuple[int, ...]:
    """A MinHash signature: the per-permutation minimum shingle hash."""
    if not shingles:
        return tuple(0 for _ in range(num_perm))
    return tuple(min(_hash_shingle(s, seed) for s in shingles) for seed in range(num_perm))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass(frozen=True, slots=True)
class NearDedupConfig:
    """Knobs for near-duplicate detection."""

    threshold: float = 0.85  #: Jaccard similarity at/above which two are dupes.
    shingle_k: int = 5  #: Character shingle width.
    num_perm: int = 32  #: MinHash permutations (signature length).
    bands: int = 8  #: LSH bands (must divide num_perm).

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if self.num_perm % self.bands != 0:
            raise ValueError("bands must divide num_perm")


def dedup_near(
    examples: Sequence[TraceExample],
    *,
    config: NearDedupConfig | None = None,
    scorer: ExampleScorer | None = None,
) -> tuple[list[TraceExample], DedupReport]:
    """Collapse near-duplicate examples via MinHash-LSH + a Jaccard confirm.

    Runs an exact pass first (cheap), then bands the MinHash signatures into an
    LSH index; only examples colliding in at least one band are compared by exact
    Jaccard, so the pass is near-linear. The highest-scored example in each
    near-duplicate cluster survives.
    """
    cfg = config or NearDedupConfig()
    score = scorer or default_scorer

    exact_kept, report = dedup_exact(examples, scorer=score)

    shingle_sets = [_shingles(_text_of(ex), cfg.shingle_k) for ex in exact_kept]
    signatures = [_minhash(s, cfg.num_perm) for s in shingle_sets]
    rows = cfg.num_perm // cfg.bands

    # LSH: bucket by each band's slice of the signature.
    buckets: dict[tuple[int, tuple[int, ...]], list[int]] = {}
    for idx, sig in enumerate(signatures):
        for b in range(cfg.bands):
            band = tuple(sig[b * rows : (b + 1) * rows])
            buckets.setdefault((b, band), []).append(idx)

    # Union-find over candidate pairs confirmed by Jaccard.
    parent = list(range(len(exact_kept)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if find(a) == find(b):
                    continue
                if _jaccard(shingle_sets[a], shingle_sets[b]) >= cfg.threshold:
                    union(a, b)

    # Keep the best per cluster, preserving the first-seen order of survivors.
    cluster_best: dict[int, int] = {}
    for idx in range(len(exact_kept)):
        root = find(idx)
        cur = cluster_best.get(root)
        if cur is None or score(exact_kept[idx]) > score(exact_kept[cur]):
            cluster_best[root] = idx

    survivors = set(cluster_best.values())
    kept = [ex for i, ex in enumerate(exact_kept) if i in survivors]
    report.near_removed = len(exact_kept) - len(kept)
    report.kept = len(kept)
    return kept, report


def dedup(
    examples: Iterable[TraceExample],
    *,
    near: bool = True,
    config: NearDedupConfig | None = None,
    scorer: ExampleScorer | None = None,
) -> tuple[list[TraceExample], DedupReport]:
    """Top-level dedup: exact always, near-duplicate when ``near`` (the default)."""
    seq = list(examples)
    if near:
        return dedup_near(seq, config=config, scorer=scorer)
    return dedup_exact(seq, scorer=scorer)


__all__ = [
    "DedupReport",
    "ExampleScorer",
    "NearDedupConfig",
    "dedup",
    "dedup_exact",
    "dedup_near",
    "default_scorer",
]
