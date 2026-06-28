# Media / asset service — `backend/app/media/`

> A hardened media-asset subsystem layered **over** the existing
> `app.storage.object_store.ObjectStore` and **complementing** the §9.7 render
> pipeline (which already persists provider videos because task URLs expire).
> Nothing here rewrites the render path; it gives the rest of the system a
> typed, content-addressed, lifecycle-aware media layer.

Authoritative spec: `kinora.md` §9 (generation pipeline), §8.7 (caching &
content-hash dedup), §12 (engineering). Read the cited `§` before changing code.

## Why this exists

The render pipeline writes clips/keyframes/audio at deterministic keys
(`clips/{book}/{shot}.mp4`, …) and signs URLs ad-hoc. That is enough for the
happy path, but a long adaptation needs:

- **Content-addressed dedup** — two assets that share bytes should share one
  stored blob. §8.7 dedups at the *shot-hash* level (don't re-render); this
  dedups at the *byte* level (don't re-store) — identical Ken-Burns cards,
  repeated cover art, etc.
- **Resumable / multipart upload** — large source PDFs and provider videos that
  exceed a single PUT, with restartable parts.
- **Signed / expiring CDN URLs with a stable contract** — one place that knows
  the `minio:9000 → localhost:9000` rewrite, public-base preference, and TTL.
- **Poster / thumbnail / sprite generation** — ffmpeg-derived stills and a
  sprite-sheet + WEBVTT for scrubbing the generated film.
- **HLS / DASH packaging + a master playlist** — segment the stitched scene
  mp4s into an adaptive-bitrate ladder so the reading-room player can stream.
- **Per-asset metadata + checksums** — a durable record (size, content-type,
  sha256, width/height/duration, etag) for every managed blob.
- **Lifecycle / retention GC** — sweep orphaned/expired derived assets.
- **A transcode-orchestration queue seam** — a narrow protocol so a worker can
  drain transcode jobs without this package depending on Redis.

## Layering

```
app.storage.object_store.ObjectStore        (existing — unchanged)
            ▲
            │ used through the MediaStore Protocol
            │
app.media.errors         typed errors
app.media.hashing        streaming sha256 / content-address key derivation
app.media.urls           URL signing contract + minio→localhost rewrite
app.media.metadata       AssetMetadata + content-type/extension helpers (pure)
app.media.store          content-addressed put/get, multipart, signed URLs
app.media.probe          ffmpeg/ffprobe media inspection (reuses degrade)
app.media.images         poster / thumbnail / sprite-sheet generation (ffmpeg)
app.media.vtt            WEBVTT sprite + chapter cue authoring (pure)
app.media.packaging      HLS/DASH segmenting + master playlist (ffmpeg + pure)
app.media.kinds          MediaAssetKind enum (shared by model + service)
app.media.models         MediaAsset ORM model
app.media.repository     MediaAssetRepo over the media_assets table
app.media.lifecycle      retention policy + GC sweep (pure policy + DB sweep)
app.media.transcode      transcode-job queue seam (Protocol + in-proc impl)
app.media.service        MediaService — the orchestration facade
app.media.ranges         HTTP byte-range parsing/slicing for progressive playback
app.media.testing        FakeMediaStore + tiny-asset builders for tests
```

## Data model (additive migration)

New table `media_assets` (migration `b3f7c1d20e94`, chains on current head
`a1b2c3d4e5f6`):

| column | type | note |
|---|---|---|
| `id` | str PK | opaque id |
| `book_id` | str FK→books (nullable, SET NULL) | scoping for GC |
| `kind` | str enum | `clip`/`poster`/`thumbnail`/`sprite`/`hls`/`source`/… |
| `storage_key` | str | object-store key |
| `content_hash` | str (sha256 hex), indexed | the dedup key |
| `content_type` | str | MIME |
| `size_bytes` | bigint | |
| `width`/`height` | int nullable | for visual assets |
| `duration_s` | float nullable | for AV assets |
| `etag` | str nullable | provider etag |
| `meta` | JSONB | free-form (sprite cols/rows, hls variants, …) |
| `ref_count` | int | live references; GC only collects 0 |
| `expires_at` | timestamptz nullable | retention horizon |
| `created_at`/`updated_at` | timestamptz | mixins |

Unique on `content_hash` is intentionally **NOT** enforced at the DB level
(different `kind`/`book` may legitimately reference one blob); dedup is handled
in `store` by checking `exists` on the derived content-address key.

## Shared-file changes (additive only — documented here)

- `core/config.py` — add media TTL / packaging / retention settings (defaults
  keep behaviour identical; no required new env).
- `db/models/__init__.py` — export `MediaAsset`, `MediaAssetKind`.
- `composition.py` — construct `MediaService` lazily on the `Container`
  (additive attribute; nothing else reads it yet).
- `api/routes/__init__.py` + `main.py` — mount a `/api/media` router (asset
  metadata + signed URL minting). Additive include only.
- New migration with a unique revision id chained on `a1b2c3d4e5f6`.

## Test strategy

- Pure modules (hashing, vtt, urls, metadata, lifecycle policy) — full unit
  coverage, no infra.
- ffmpeg modules (probe, images, packaging) — generate **tiny real** mp4s with
  the bundled `imageio-ffmpeg` binary at runtime (skip cleanly if no ffmpeg),
  never network.
- store / repository / service — a `FakeMediaStore` in-memory object store for
  unit tests; the DB-backed repo test uses the isolated
  `kinora_media_test` DB on :5433 + redis db 15 and skips when unset.
- No real network, no real credits, `KINORA_LIVE_VIDEO` stays OFF.

## Milestones

1. **M1 — foundation** ✅: errors, hashing, urls, metadata, store +
   FakeMediaStore, multipart/resumable, content-address dedup.
2. **M2 — ffmpeg derivations** ✅: probe, posters/thumbnails, sprite sheets
   + WEBVTT.
3. **M3 — packaging** ✅: HLS segmenter + master playlist, DASH manifest, ABR
   ladder presets.
4. **M4 — persistence** ✅: `media_assets` model + repo + migration
   `b3f7c1d20e94` (chains on `a1b2c3d4e5f6`; round-trip verified;
   `alembic check` shows no `media_assets` drift).
5. **M5 — orchestration** ✅: MediaService facade, RetentionPolicy + lifecycle
   GC, transcode queue seam, composition `media_service` property, `/api/media`
   router (registered in ROUTERS).
6. **M6 — depth** ✅: checksum integrity sweep (`verify_integrity`), HTTP
   byte-range parsing (`app.media.ranges`), retention policy, migration
   round-trip + autogenerate check.

### Test status
- 131 media tests pass against full isolated infra (kinora_media_test on :5433,
  redis db 15, MinIO bucket kinora-media-test); 19 skip cleanly with no infra.
- Whole backend suite green (1138+ passed) with no regressions; lint (ruff +
  mypy) clean across the app.

## Additive shared-file changes (this domain only)

- `core/config.py` — added `media_url_ttl_s`, `media_segment_s`,
  `media_sprite_count`, `media_derived_retention_days`, `media_gc_batch`
  (all defaulted; no new required env).
- `db/models/__init__.py` — import + export `MediaAsset`, `MediaAssetKind`.
- `composition.py` — `TYPE_CHECKING` import of `MediaService`, a
  `_media_service` cache field, and a lazy `media_service` property
  (`build_media_service`). Nothing else reads it yet.
- `api/routes/__init__.py` — import `media` and append `media.router` to ROUTERS.
- New migration `migrations/versions/b3f7c1d20e94_media_assets_registry.py`.

## Non-goals

- Re-rendering or re-deciding render modes (that is `app.render`).
- Turning on live video or spending credits.
- Owning the Redis queue (only a narrow transcode seam Protocol).
