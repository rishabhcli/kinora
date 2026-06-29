"""Merkle reconciliation: find divergent keys without shipping the whole keyspace.

Async log shipping closes most of the gap between replicas, but logs get
truncated, messages get dropped, and a region can be partitioned for long enough
that replaying the full delta is wasteful. Anti-entropy fixes the residue: two
replicas compare compact **Merkle trees** over their keyspace and exchange only
the subtrees whose hashes differ, converging in ``O(d · log n)`` work for ``d``
divergent keys out of ``n`` — never ``O(n)``.

* :class:`MerkleLeaf` / :class:`MerkleNode` — the tree. A leaf hashes one
  *bucket* of keys (keys are bucketed by a stable hash so the tree shape is
  independent of which keys exist); an internal node hashes its children. The
  root hash equals iff the two keyspaces are byte-identical.
* :func:`build_merkle` — build a fixed-arity tree of a fixed depth from a
  ``{key: value_fingerprint}`` map. Fixed shape means two replicas' trees are
  positionally comparable.
* :func:`diff_buckets` — walk two trees in lockstep, descending only where
  hashes differ, and return the set of leaf-bucket indices that disagree — the
  buckets whose keys must be exchanged and merged.

Pure and deterministic: the same map always yields the same tree, and the diff
is symmetric. The value fingerprint is supplied by the caller (typically the
applied :class:`HybridTimestamp` of the cell) so equal values hash equal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Default branching factor and depth of the comparison tree. ``arity**depth``
#: leaf buckets; 16**4 = 65 536 buckets comfortably spreads a large keyspace.
DEFAULT_ARITY = 16
DEFAULT_DEPTH = 4

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _h(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def bucket_of(key: str, arity: int, depth: int) -> int:
    """Map ``key`` to a stable leaf-bucket index in ``[0, arity**depth)``.

    Uses a hash so the assignment is uniform and independent of key insertion
    order, which is what makes two replicas' trees positionally comparable.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big")
    return raw % (arity**depth)


@dataclass(frozen=True, slots=True)
class MerkleLeaf:
    """A leaf: the hash of one bucket's ``(key, fingerprint)`` pairs."""

    index: int
    hash: str


@dataclass(frozen=True, slots=True)
class MerkleNode:
    """An internal node: hash of its children's hashes (left-to-right)."""

    hash: str
    children: Sequence[MerkleNode | MerkleLeaf]


@dataclass(frozen=True, slots=True)
class MerkleTree:
    """A fixed-shape Merkle tree over a keyspace."""

    root: MerkleNode | MerkleLeaf
    arity: int
    depth: int

    @property
    def root_hash(self) -> str:
        return self.root.hash


def _leaf_hash(pairs: Sequence[tuple[str, str]]) -> str:
    if not pairs:
        return _EMPTY_HASH
    # Sort so the bucket hash is independent of map iteration order.
    ordered = sorted(pairs)
    digest = hashlib.sha256()
    for key, fp in ordered:
        digest.update(_h(key, fp).encode("ascii"))
    return digest.hexdigest()


def build_merkle(
    fingerprints: Mapping[str, str],
    *,
    arity: int = DEFAULT_ARITY,
    depth: int = DEFAULT_DEPTH,
) -> MerkleTree:
    """Build a fixed ``arity``/``depth`` tree from ``{key: fingerprint}``.

    The fingerprint is any string that is equal iff the cell value is equal
    (e.g. the converged write's encoded :class:`HybridTimestamp`). Empty buckets
    hash to a constant so two trees over disjoint-but-equal regions still match.
    """
    n_buckets = arity**depth
    buckets: list[list[tuple[str, str]]] = [[] for _ in range(n_buckets)]
    for key, fp in fingerprints.items():
        buckets[bucket_of(key, arity, depth)].append((key, fp))
    leaves: list[MerkleNode | MerkleLeaf] = [
        MerkleLeaf(i, _leaf_hash(b)) for i, b in enumerate(buckets)
    ]
    level: list[MerkleNode | MerkleLeaf] = leaves
    while len(level) > 1:
        parents: list[MerkleNode | MerkleLeaf] = []
        for i in range(0, len(level), arity):
            group = level[i : i + arity]
            parents.append(MerkleNode(_h(*(c.hash for c in group)), tuple(group)))
        level = parents
    return MerkleTree(level[0], arity, depth)


def diff_buckets(left: MerkleTree, right: MerkleTree) -> frozenset[int]:
    """Return the leaf-bucket indices whose hashes disagree between the trees.

    Walks both trees in lockstep, pruning any subtree whose root hashes match
    (the log(n) saving). Trees must share ``arity`` and ``depth``.
    """
    if (left.arity, left.depth) != (right.arity, right.depth):
        raise ValueError("Merkle trees must share arity and depth to diff")
    if left.root_hash == right.root_hash:
        return frozenset()
    diffs: set[int] = set()
    _diff(left.root, right.root, diffs)
    return frozenset(diffs)


def _diff(
    a: MerkleNode | MerkleLeaf,
    b: MerkleNode | MerkleLeaf,
    out: set[int],
) -> None:
    if a.hash == b.hash:
        return
    if isinstance(a, MerkleLeaf) or isinstance(b, MerkleLeaf):
        # At a leaf level: record the divergent bucket index.
        if isinstance(a, MerkleLeaf):
            out.add(a.index)
        if isinstance(b, MerkleLeaf):
            out.add(b.index)
        return
    for child_a, child_b in zip(a.children, b.children, strict=True):
        _diff(child_a, child_b, out)
