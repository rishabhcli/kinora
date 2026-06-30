"""Maintenance / compaction for the vector index.

Over a long adaptation an entity accumulates references — many of them
near-duplicates (consecutive frames of a locked character) or superseded by a
newer canon version. :func:`compact_index` reclaims that space without changing
what the store *answers*:

* **dedup** — within a namespace, references whose cosine to an already-kept
  reference is ``>= dedup_threshold`` are dropped (the higher-version / newer of
  the pair is kept);
* **version pruning** — when ``keep_versions`` is set, only the N newest
  ``version`` values per ``entity_key`` survive;
* **orphan sweep** — empty namespaces are dropped.

It operates only through the :class:`~app.embeddings.index.VectorIndex` protocol,
so it works against any backend, and returns a :class:`CompactionReport`. It is
idempotent: running it twice on a compacted index removes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.embeddings.index import VectorIndex, VectorRecord


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """What a compaction pass changed."""

    namespaces_examined: int = 0
    deduped: int = 0
    version_pruned: int = 0
    orphan_namespaces_dropped: int = 0
    removed_ids: tuple[str, ...] = ()

    @property
    def total_removed(self) -> int:
        return self.deduped + self.version_pruned


def _record_sort_key(rec: VectorRecord) -> tuple[int, float, str]:
    """Higher version, then newer, then id — the record we'd rather keep is last."""
    md = rec.metadata
    return (
        int(md.get("version", 1)),
        float(md.get("created_at", 0.0)),
        rec.id,
    )


async def compact_index(
    index: VectorIndex,
    *,
    namespaces: Sequence[str] | None = None,
    dedup_threshold: float = 0.985,
    keep_versions: int | None = None,
) -> CompactionReport:
    """Compact (dedup + version-prune + orphan-sweep) one or all namespaces."""
    target_namespaces = list(namespaces) if namespaces is not None else await index.namespaces()

    examined = 0
    deduped = 0
    version_pruned = 0
    orphans = 0
    removed_ids: list[str] = []

    for ns in target_namespaces:
        recs = await index.iter_records(namespace=ns)
        if not recs:
            # Orphan namespace: nothing to drop structurally for in-memory, but
            # count it so callers can detect it. (drop_namespace is backend-opt.)
            orphans += 1
            await _maybe_drop_namespace(index, ns)
            continue
        examined += 1

        to_remove: set[str] = set()

        # --- version pruning ---
        if keep_versions is not None and keep_versions > 0:
            by_entity: dict[str, set[int]] = {}
            for rec in recs:
                ek = str(rec.metadata.get("entity_key", ns))
                by_entity.setdefault(ek, set()).add(int(rec.metadata.get("version", 1)))
            keep_set: dict[str, set[int]] = {
                ek: set(sorted(versions, reverse=True)[:keep_versions])
                for ek, versions in by_entity.items()
            }
            for rec in recs:
                ek = str(rec.metadata.get("entity_key", ns))
                if int(rec.metadata.get("version", 1)) not in keep_set[ek]:
                    to_remove.add(rec.id)
            version_pruned += len(to_remove)

        # --- dedup (over the survivors) ---
        survivors = [r for r in recs if r.id not in to_remove]
        # Keep the "best" record of each near-duplicate cluster: visit the
        # preferred record (highest version, newest, id tie-break) FIRST so it
        # becomes the cluster representative; later near-duplicates are dropped.
        survivors.sort(key=_record_sort_key, reverse=True)
        kept: list[VectorRecord] = []
        for rec in survivors:
            dup = False
            for keep in kept:
                if rec.vector.space != keep.vector.space:
                    continue
                if rec.vector.cosine(keep.vector) >= dedup_threshold:
                    # rec is a near-duplicate of a (preferred) kept record.
                    dup = True
                    break
            if dup:
                to_remove.add(rec.id)
                deduped += 1
            else:
                kept.append(rec)

        if to_remove:
            removed_ids.extend(sorted(to_remove))
            await index.delete(list(to_remove), namespace=ns)
            # If we emptied the namespace, sweep it.
            if await index.count(namespace=ns) == 0:
                orphans += 1
                await _maybe_drop_namespace(index, ns)

    return CompactionReport(
        namespaces_examined=examined,
        deduped=deduped,
        version_pruned=version_pruned,
        orphan_namespaces_dropped=orphans,
        removed_ids=tuple(removed_ids),
    )


async def _maybe_drop_namespace(index: VectorIndex, namespace: str) -> None:
    """Drop a namespace if the backend supports it (best-effort, additive)."""
    drop = getattr(index, "drop_namespace", None)
    if drop is not None:
        await drop(namespace)


__all__ = ["CompactionReport", "compact_index"]
