# Data portability & data export/import — design + roadmap

Owner package: `backend/app/dataportability/` (+ `backend/app/api/routes/portability.py`).

Kinora's durable state for a book is split across **Postgres** (the canon graph,
shots, scenes, beats, sync maps, budget ledger, prefs, defects) and **object
storage** (the source PDF/EPUB, page images, keyframes, reference assets,
rendered clips/last-frames, narration audio, the markdown canon vault). This
package makes all of that **portable**: a single, versioned, checksummed archive
that round-trips losslessly through export → import on a different deployment,
with id remapping so an imported book never collides with an existing one.

It also covers the operational siblings of portability — **account/GDPR export +
right-to-erasure**, and **backup + point-in-time restore orchestration** — and
the long-lived concern that any archive format eventually needs: a
**migration/transform layer** that upgrades an old archive to the current schema
on import.

References: kinora.md §8 (the canon: versioned entities, continuity states,
caching) and §9 (the pipeline: shots, sync maps, stitched films).

## The archive format — `.kinora` (a.k.a. KAR, Kinora ARchive)

A `.kinora` archive is a **ZIP container** (stdlib `zipfile`, deflate) with a
fixed internal layout:

```
manifest.json            # ArchiveManifest: format_version, kind, created_at, counts, checksum index
data/<table>.jsonl       # one JSON object per row, per logical table (streaming-friendly)
blobs/<sha256>           # content-addressed blob payloads (dedup by hash)
blobs/index.jsonl        # blob_sha256 -> {original_key, content_type, size}
```

- **Versioned.** `manifest.format_version` is an integer (current = `1`). The
  migration layer upgrades older archives to the current version on import.
- **Checksummed.** Every `data/*.jsonl` and every blob carries a SHA-256 in the
  manifest's `checksums` map; `verify()` recomputes them and reports tampering or
  truncation. The manifest itself carries a `manifest_digest` over the checksum
  index so a single value attests the whole archive.
- **Content-addressed blobs.** Object-store payloads are stored once per distinct
  content hash; the row JSON references blobs by `sha256`, never by raw bytes, so
  a 200-shot book that reuses one keyframe stores it once.
- **Streaming, bounded memory.** Rows are written/read a line at a time (JSONL);
  blobs stream through fixed-size chunks. Export of a huge book never holds more
  than one row + one blob chunk in memory beyond the zip's own buffering.

## Modules (each is a milestone)

| Module | Responsibility |
|---|---|
| `errors.py` | Typed exceptions (`PortabilityError`, `ChecksumMismatch`, `UnsupportedArchiveVersion`, `ReferentialIntegrityError`, `ArchiveFormatError`). |
| `manifest.py` | `ArchiveManifest`, `BlobRef`, format-version constant, digest helpers. |
| `codec.py` | `ArchiveWriter` / `ArchiveReader` — the streaming, checksummed ZIP container. Pure I/O, no domain knowledge. |
| `blobs.py` | `BlobSink` / `BlobSource` — bridge object storage ↔ archive blobs (content-addressed, dedup, streamed). |
| `serialization.py` | ORM-row ↔ portable-dict projection for every exported table; embeddings encoded compactly; deterministic ordering. |
| `idremap.py` | `IdRemapper` — collision-free id remapping on import while preserving all intra-archive references (FKs, `entity_key`, `scene_id`, `beat_id`, `reference_image_ids`, source-span links). |
| `book_export.py` | Full book-bundle export: DB rows + referenced object-store blobs → archive. |
| `book_import.py` | Full book-bundle import: archive → DB rows (remapped) + restored blobs; referential-integrity validated. |
| `canon_export.py` / `canon_import.py` | Canon-graph-only export/import (entities + continuity states + bitemporal + audit + branches + vault), referential integrity + id remapping. |
| `account.py` | GDPR account export (all of a user's books + profile) and right-to-erasure (cascade-aware hard delete + blob purge), with a dry-run plan. |
| `backup.py` | Backup + point-in-time restore orchestration: snapshot a set of books to a backup archive, list/inspect, restore selectively, prune. |
| `migrate.py` | Archive-format migration registry: a chain of `vN -> vN+1` transforms applied on import to bring an old archive to `CURRENT_FORMAT_VERSION`. |
| `service.py` | `PortabilityService` — the façade the route calls; wires repos + object store + codec. |
| `__init__.py` | Public exports. |

## Route — `backend/app/api/routes/portability.py`

Mounted under `/api` (additive entry in `routes/__init__.py`). Owner-scoped:

- `GET  /books/{id}/export`            → stream a `.kinora` book bundle.
- `POST /books/import`                 → upload a `.kinora`, import as a new owned book (id-remapped).
- `GET  /books/{id}/canon/export`      → stream a canon-only archive.
- `POST /books/{id}/canon/import`      → merge a canon archive into a book.
- `GET  /me/export`                    → stream a full GDPR account archive.
- `POST /me/erasure`                   → right-to-erasure (with `?dry_run=true` plan).
- `POST /archives/inspect`             → manifest + verification report without importing (via upload).

## Referential integrity + id remapping (import)

On import every primary key is reassigned via `IdRemapper` (new opaque ids), and
**every reference is rewritten** in lockstep: `book_id`, `scene_id`, `beat_id`,
`shot_id`, `supersedes`, source-span index `shot_id`, budget-ledger
`reservation_id`, etc. `entity_key` is kept stable *within the book* (it is the
canon identity the agents pass around) but is naturally namespaced by the new
`book_id`. Import is atomic per book (one unit of work) and fails closed on any
dangling reference.

## Test plan (round-trip fidelity)

`tests/test_dataportability_*.py`:
- Unit (no infra): manifest digesting, codec round-trip in-memory, checksum
  tamper detection, id-remapper invariants, serialization symmetry, migration
  chain, blob dedup.
- Integration (isolated DB `kinora_portability_test` + redis db 15 + MinIO):
  seed a book with canon + shots + blobs, export → import into a second owned
  book, assert **deep structural equality** of the projected graph and that all
  blobs are byte-identical; GDPR export/erasure; backup/restore.

## Shared-file changes (additive only)
- `backend/app/api/routes/__init__.py`: `+portability` import + `ROUTERS` entry
  (2 lines, additive).
- No new DB tables ⇒ **no Alembic migration** (backup/restore reuse existing
  tables; archive metadata lives in object storage / the archive itself, not
  Postgres). Current Alembic head unchanged at `a1b2c3d4e5f6`.

## Test results
- `make lint` (ruff + mypy, 396 source files): green.
- `make test` (no infra): 1088 passed, 0 failed, 176 skipped.
- Integration (isolated DB `kinora_portability_test` + redis db 15 + MinIO): the
  6 `tests/test_dataportability_*` files all pass (codec, serialization,
  migration, service-unit, round-trip fidelity, feature/route). 16 infra tests +
  ~30 unit tests for this package.
- Pre-existing, unrelated: `test_api_director` (2) is order/isolation-flaky in
  the director domain — reproduces with this package's route reverted; not caused
  by this work.

## Status
- [x] Milestone 1 — errors, manifest, codec (streaming checksummed ZIP)
- [x] Milestone 2 — blobs bridge + serialization + id remapper + key rewriting
- [x] Milestone 3 — book bundle export/import (dbio + topo-ordered self-FK insert)
- [x] Milestone 4 — canon graph export/import (replace / merge)
- [x] Milestone 5 — GDPR account export + import + right-to-erasure (dry-run + execute)
- [x] Milestone 6 — backup + point-in-time restore (catalog-indexed snapshots)
- [x] Milestone 7 — archive-format migration layer (validated chain)
- [x] Milestone 8 — service façade + HTTP route (owner-scoped, streaming)
- [x] Milestone 9 — round-trip property + integration tests

## Implementation notes (post-build)
- **shot_hash re-keying on import** (`scrub.py`): `shots.shot_hash` is globally
  ``UNIQUE`` and derived from the *old* book id, so importing it verbatim into a
  DB that still holds the source collides. On import both `shots.shot_hash` and
  `shot_cache.shot_hash` are re-keyed to ``<new_book_id>:<old_hash>`` — collision-
  free, self-consistent, and stale-by-design (the render pipeline recomputes the
  hash from the new book on the next render).
- **Self-referential FK ordering** (`dbio._topo_order_self_refs`):
  `entities.supersedes` points at another `entities` row; Postgres checks self-FKs
  per row within an ``executemany``, so the batch is topologically ordered
  (Kahn, stable) before insert.
- **Deterministic per-book asset keys**: book/account/backup exports also pull the
  source doc + cover + rendered page stills (`pages/<book>/NNNN.png`) by book id,
  so assets travel even when no row references them by key.
- **Merge-mode canon import** appends rows; a `(book_id, entity_key, version)`
  collision with the target's existing canon is a documented caller
  responsibility (the unique constraint rejects it) — `replace` mode is the safe
  default and clears the target's canon first.
