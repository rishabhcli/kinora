"""The identity store: versioned reference images + the same-character verdict.

This is the durable, queryable store beneath Round-1's identity-lock (kinora.md
§8). For each canon entity (a character, a location/setting, a prop, a style) it
holds a set of **reference images** — each with its own embedding (in a known
space), an appearance descriptor, optional pose/shot tags, and a *version* — and
answers the two questions the render/Critic loop actually asks:

1. **"Is this new frame the same character?"** — :meth:`verify` embeds the frame,
   scores it against the entity's references, and returns a :class:`MatchVerdict`
   with a :class:`Verdict` (``MATCH`` / ``UNCERTAIN`` / ``REJECT``) plus the best
   matching reference and its score. This is the storeful form of the round-1
   character-consistency score (CCS).
2. **"Fetch the best reference for this pose/shot."** — :meth:`best_reference`
   returns the most appropriate reference, optionally constrained to references
   tagged for a pose/shot, ranked by similarity to a query (e.g. a rough
   storyboard frame) or by recency/version when no query is given.

References live in the injected :class:`~app.embeddings.index.VectorIndex` under a
per-entity namespace (``{book_id}:{entity_key}``), so two entities — and two
books — never bleed into each other's matches. Descriptors and version metadata
ride along on each :class:`~app.embeddings.index.VectorRecord`.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.embeddings.config import EmbeddingStoreSettings
from app.embeddings.embedder import Embedder
from app.embeddings.index import (
    MetadataFilter,
    SearchResult,
    VectorIndex,
    VectorRecord,
)
from app.embeddings.models import EntityKind
from app.embeddings.vectors import EmbeddingVector, SpaceMismatch


class Verdict(enum.StrEnum):
    """The same-entity decision."""

    MATCH = "match"  # confidently the same entity
    UNCERTAIN = "uncertain"  # between thresholds — needs a human/Critic call
    REJECT = "reject"  # confidently a different entity


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """One versioned reference for an entity, with its embedding + descriptor."""

    ref_id: str
    entity_key: str
    book_id: str
    kind: EntityKind
    vector: EmbeddingVector
    version: int = 1
    #: Free-text appearance descriptor ("auburn braid, ice-blue dress, ...").
    descriptor: str | None = None
    #: Pose / shot tags this reference is good for ("front", "profile", "wide").
    pose_tags: tuple[str, ...] = ()
    #: Object-storage key (or any locator) for the source image bytes.
    source_key: str | None = None
    #: Whether this entity's appearance is LOCKED (canon, §8 identity-lock).
    locked: bool = False
    created_at: float = field(default_factory=time.time)

    def to_metadata(self) -> dict[str, Any]:
        """Project to index metadata (filterable scalars/lists)."""
        return {
            "entity_key": self.entity_key,
            "book_id": self.book_id,
            "kind": self.kind.value,
            "version": self.version,
            "descriptor": self.descriptor,
            "pose_tags": list(self.pose_tags),
            "source_key": self.source_key,
            "locked": self.locked,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, rec: VectorRecord) -> ReferenceImage:
        md = rec.metadata
        return cls(
            ref_id=rec.id,
            entity_key=str(md["entity_key"]),
            book_id=str(md["book_id"]),
            kind=EntityKind(md.get("kind", EntityKind.OTHER.value)),
            vector=rec.vector,
            version=int(md.get("version", 1)),
            descriptor=md.get("descriptor"),
            pose_tags=tuple(md.get("pose_tags", []) or ()),
            source_key=md.get("source_key"),
            locked=bool(md.get("locked", False)),
            created_at=float(md.get("created_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class MatchVerdict:
    """The result of :meth:`IdentityStore.verify`."""

    verdict: Verdict
    score: float
    #: The closest reference, if any references existed.
    best_reference: ReferenceImage | None
    #: All scored references (closest first), for inspection / logging.
    candidates: tuple[tuple[ReferenceImage, float], ...] = ()

    @property
    def is_match(self) -> bool:
        return self.verdict is Verdict.MATCH


def _namespace(book_id: str, entity_key: str) -> str:
    return f"{book_id}:{entity_key}"


class IdentityStore:
    """Versioned reference store + same-entity verdict over a :class:`VectorIndex`."""

    def __init__(
        self,
        index: VectorIndex,
        embedder: Embedder,
        settings: EmbeddingStoreSettings,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._settings = settings

    # -- registration ------------------------------------------------------- #
    async def add_reference(
        self,
        *,
        ref_id: str,
        entity_key: str,
        book_id: str,
        kind: EntityKind,
        image_bytes: bytes | None = None,
        vector: EmbeddingVector | None = None,
        version: int = 1,
        descriptor: str | None = None,
        pose_tags: Sequence[str] = (),
        source_key: str | None = None,
        locked: bool = False,
        enforce_admission: bool = True,
    ) -> ReferenceImage:
        """Register a reference. Supply either ``image_bytes`` or a ``vector``.

        When ``enforce_admission`` and the entity already has references, the new
        reference must be at least ``admit_min_similarity`` to an existing one —
        this rejects accidental wrong-character uploads / drift. Set it ``False``
        to seed the very first canonical reference unconditionally (the lock).
        """
        if vector is None:
            if image_bytes is None:
                raise ValueError("add_reference requires image_bytes or a precomputed vector")
            vector = (await self._embedder.embed_images([image_bytes]))[0]
        self._guard_space(vector)

        existing = await self._references(book_id, entity_key)
        if enforce_admission and existing:
            best = max((vector.cosine(r.vector) for r in existing), default=0.0)
            if best < self._settings.admit_min_similarity:
                raise ValueError(
                    f"reference for {entity_key} rejected: best similarity {best:.3f} "
                    f"< admit_min_similarity {self._settings.admit_min_similarity}"
                )

        ref = ReferenceImage(
            ref_id=ref_id,
            entity_key=entity_key,
            book_id=book_id,
            kind=kind,
            vector=vector,
            version=version,
            descriptor=descriptor,
            pose_tags=tuple(pose_tags),
            source_key=source_key,
            locked=locked,
        )
        await self._index.upsert(
            [
                VectorRecord(
                    id=ref_id,
                    vector=vector,
                    namespace=_namespace(book_id, entity_key),
                    metadata=ref.to_metadata(),
                )
            ]
        )
        return ref

    async def remove_reference(self, *, book_id: str, entity_key: str, ref_id: str) -> bool:
        removed = await self._index.delete(
            [ref_id], namespace=_namespace(book_id, entity_key)
        )
        return removed > 0

    async def list_references(
        self, *, book_id: str, entity_key: str, version: int | None = None
    ) -> list[ReferenceImage]:
        refs = await self._references(book_id, entity_key)
        if version is not None:
            refs = [r for r in refs if r.version == version]
        # Newest version first, then newest created.
        refs.sort(key=lambda r: (r.version, r.created_at), reverse=True)
        return refs

    # -- the two questions -------------------------------------------------- #
    async def verify(
        self,
        *,
        book_id: str,
        entity_key: str,
        frame_bytes: bytes | None = None,
        frame_vector: EmbeddingVector | None = None,
        top_k: int | None = None,
    ) -> MatchVerdict:
        """Is this new frame the same entity? Returns a scored verdict.

        Scores the frame against every reference for the entity and decides on
        the *best* reference's cosine relative to the configured thresholds.
        """
        if frame_vector is None:
            if frame_bytes is None:
                raise ValueError("verify requires frame_bytes or a frame_vector")
            frame_vector = (await self._embedder.embed_images([frame_bytes]))[0]
        self._guard_space(frame_vector)

        k = top_k or self._settings.default_top_k
        results = await self._index.search(
            frame_vector,
            top_k=k,
            namespace=_namespace(book_id, entity_key),
        )
        candidates = tuple(
            (ReferenceImage.from_record(r.record), r.score) for r in results
        )
        if not candidates:
            return MatchVerdict(
                verdict=Verdict.UNCERTAIN, score=0.0, best_reference=None, candidates=()
            )
        best_ref, best_score = candidates[0]
        verdict = self._classify(best_score)
        return MatchVerdict(
            verdict=verdict,
            score=best_score,
            best_reference=best_ref,
            candidates=candidates,
        )

    async def best_reference(
        self,
        *,
        book_id: str,
        entity_key: str,
        query_vector: EmbeddingVector | None = None,
        pose: str | None = None,
        version: int | None = None,
    ) -> ReferenceImage | None:
        """Fetch the best reference for a pose/shot.

        * ``pose`` restricts to references tagged for that pose (falling back to
          all references if none are tagged for it);
        * ``query_vector`` (e.g. a rough storyboard frame) ranks references by
          similarity to it;
        * with no query, the newest (highest version, then most recent) reference
          wins.
        """
        ns = _namespace(book_id, entity_key)
        filt: MetadataFilter | None = None
        if version is not None:
            filt = MetadataFilter().eq("version", version)
        if pose is not None:
            pose_filt = (filt or MetadataFilter()).contains("pose_tags", pose)
            posed = await self._search_or_all(ns, query_vector, pose_filt)
            if posed:
                return self._pick(posed, query_vector)
            # Fall through to unposed references if nothing matched the pose.

        candidates = await self._search_or_all(ns, query_vector, filt)
        return self._pick(candidates, query_vector) if candidates else None

    # -- internals ---------------------------------------------------------- #
    async def _search_or_all(
        self,
        namespace: str,
        query_vector: EmbeddingVector | None,
        filt: MetadataFilter | None,
    ) -> list[SearchResult]:
        if query_vector is not None:
            self._guard_space(query_vector)
            return await self._index.search(
                query_vector,
                top_k=self._settings.default_top_k,
                namespace=namespace,
                filter=filt,
            )
        # No query: synthesize results (score 0) from all records so callers get
        # a uniform shape; ranking falls back to version/recency in :meth:`_pick`.
        recs = await self._index.iter_records(namespace=namespace)
        if filt is not None:
            recs = [r for r in recs if filt.matches(r.metadata)]
        return [SearchResult(record=r, score=0.0) for r in recs]

    def _pick(
        self, results: Sequence[SearchResult], query_vector: EmbeddingVector | None
    ) -> ReferenceImage:
        if query_vector is not None:
            # Index already sorted by score desc; take the top.
            return ReferenceImage.from_record(results[0].record)
        # No query: newest version, then most recent.
        refs = [ReferenceImage.from_record(r.record) for r in results]
        refs.sort(key=lambda r: (r.version, r.created_at), reverse=True)
        return refs[0]

    async def _references(self, book_id: str, entity_key: str) -> list[ReferenceImage]:
        recs = await self._index.iter_records(namespace=_namespace(book_id, entity_key))
        return [ReferenceImage.from_record(r) for r in recs]

    def _classify(self, score: float) -> Verdict:
        if score >= self._settings.match_threshold:
            return Verdict.MATCH
        if score < self._settings.reject_threshold:
            return Verdict.REJECT
        return Verdict.UNCERTAIN

    def _guard_space(self, vector: EmbeddingVector) -> None:
        expected = self._embedder.space
        if vector.space != expected:
            raise SpaceMismatch(
                f"identity store expects {expected.key} but vector is {vector.space.key}"
            )


__all__ = [
    "IdentityStore",
    "MatchVerdict",
    "ReferenceImage",
    "Verdict",
]
