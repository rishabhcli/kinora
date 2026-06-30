"""Deterministic seed lineage — a stable seed tree rooted at the book/scene.

Visual consistency across a long adaptation is the whole product, and seeds are
one lever on that consistency. Two requirements pull in opposite directions:

1. **Re-render reproducibility.** The *same* logical shot, asked for again, must
   get the *same* seed — otherwise a re-read or a surgical re-render (§8.7) rolls
   a fresh variation and the footage subtly changes for no reason.
2. **Sibling coherence.** Shots within one scene should not collide on a single
   seed (which over-constrains and can make a model repeat a pose) nor be wildly
   unrelated; they should be *deterministically spread* from a common root so the
   scene reads as one continuous take.

A naïve ``hash(shot_id) % N`` satisfies (1) but ignores the *tree* structure
(book → scene → shot → repair-attempt) and gives no coherent relationship
between siblings. This module instead derives every seed from a single
**root seed** by hashing the path through the lineage tree — so the whole book's
seed space is reconstructible from one number, every node's seed is stable under
re-derivation, and sibling seeds are spread by a high-avalanche hash (no
clustering, collision-resistant).

The derived integers live in the **31-bit non-negative** space that the rest of
the stack already uses (``app.db.hashing.rotate_seed`` masks to ``0x7FFFFFFF``,
and :class:`app.providers.types.WanSpec.seed` is an ``int``), so a lineage seed
drops straight into a :class:`WanSpec` and into the §8.7 ``shot_hash``.

Repair attempts (the §9.5 retry loop re-rolls the seed on a failed QA) are a
*child* axis of the tree: ``rotate_seed`` advances within an attempt chain, but
the lineage also models attempts directly so the n-th re-roll of a shot is itself
reproducible from the root.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

#: The seed space shared with the rest of the render stack (31-bit, non-negative).
SEED_MASK = 0x7FFFFFFF

#: Unit separator for boundary-safe path joins (mirrors the cache-key discipline).
_SEP = "\x1f"

#: Stable namespace tag mixed into every derivation so a Kinora lineage seed can
#: never collide with some other system that happens to hash the same strings.
_NAMESPACE = "kinora.video.repro.seed.v1"


def _derive(*path: str) -> int:
    """Derive a 31-bit non-negative seed from an ordered, namespaced path.

    SHA-256 over the unit-separated, namespaced path gives a high-avalanche,
    collision-resistant value; we fold its leading 8 bytes into the 31-bit seed
    space. Pure and deterministic across processes/platforms.
    """
    payload = _SEP.join((_NAMESPACE, *path)).encode("utf-8")
    raw = hashlib.sha256(payload).digest()
    return int.from_bytes(raw[:8], "big") & SEED_MASK


def root_seed(book_id: str, *, salt: str = "") -> int:
    """Derive a book's deterministic **root seed** from its id (+ optional salt).

    The salt lets a deployment intentionally re-roll an entire book's look (a
    "give me a different visual take on the whole book" knob) while keeping every
    derived seed reproducible from ``(book_id, salt)``. Default salt ``""`` is the
    canonical lineage every cache hit relies on.
    """
    return _derive("root", book_id, salt)


class SeedNode(BaseModel):
    """One derived node in the seed tree, with its full reconstructible path.

    The ``path`` is the audit trail: given the root inputs and this path, anyone
    can re-derive ``seed`` exactly. ``seed`` is what goes into the ``WanSpec``.
    """

    model_config = ConfigDict(frozen=True)

    seed: int
    path: tuple[str, ...]

    @property
    def label(self) -> str:
        """A human-readable ``a/b/c`` rendering of the path for logs/manifests."""
        return "/".join(self.path)


class SeedLineage(BaseModel):
    """A deterministic seed tree for one book.

    Construct from a ``book_id`` (and optional ``salt``) and ask it for the seed
    of any ``(scene_id, shot_id, attempt)`` coordinate. The same coordinate always
    yields the same seed; different coordinates are spread across the seed space
    by the avalanche hash. Nothing here touches I/O — it is a pure derivation.
    """

    model_config = ConfigDict(frozen=True)

    book_id: str
    salt: str = ""
    root: int = Field(default=0)

    @classmethod
    def for_book(cls, book_id: str, *, salt: str = "") -> SeedLineage:
        """Build a lineage rooted at a book's deterministic root seed."""
        return cls(book_id=book_id, salt=salt, root=root_seed(book_id, salt=salt))

    # -- node derivations -------------------------------------------------- #

    def scene_node(self, scene_id: str) -> SeedNode:
        """The deterministic seed for a *scene* (the sibling root of its shots)."""
        path = (self.book_id, self.salt, "scene", scene_id)
        return SeedNode(seed=_derive(*path), path=path)

    def shot_node(self, scene_id: str, shot_id: str, *, attempt: int = 0) -> SeedNode:
        """The deterministic seed for one *shot*, optionally at a repair *attempt*.

        ``attempt=0`` is the first render; ``attempt=n`` is the n-th §9.5 re-roll.
        Each attempt is a distinct, reproducible node — so "the 2nd repair of this
        shot" re-derives to the same seed every time, which makes even the *repair
        path* replayable, not just the happy path.
        """
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        path = (
            self.book_id,
            self.salt,
            "scene",
            scene_id,
            "shot",
            shot_id,
            "attempt",
            str(attempt),
        )
        return SeedNode(seed=_derive(*path), path=path)

    def shot_seed(self, scene_id: str, shot_id: str, *, attempt: int = 0) -> int:
        """Shorthand: just the integer seed for a shot coordinate."""
        return self.shot_node(scene_id, shot_id, attempt=attempt).seed

    def custom_node(self, *components: str) -> SeedNode:
        """Derive an arbitrary deterministic node (e.g. a per-character identity
        keyframe seed). The path is namespaced under the book so it cannot collide
        with the scene/shot axes."""
        path = (self.book_id, self.salt, "custom", *components)
        return SeedNode(seed=_derive(*path), path=path)

    # -- analysis helpers (used by tests / tooling) ------------------------ #

    def scene_shot_seeds(
        self, scene_id: str, shot_ids: list[str]
    ) -> dict[str, int]:
        """Map each shot id in a scene to its first-attempt seed (sibling spread)."""
        return {sid: self.shot_seed(scene_id, sid) for sid in shot_ids}


__all__ = [
    "SEED_MASK",
    "SeedLineage",
    "SeedNode",
    "root_seed",
]
