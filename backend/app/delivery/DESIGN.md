# `app.delivery` — adaptive-bitrate streaming delivery & packaging (DESIGN)

> **Stream the growing film smoothly, regardless of which model produced each
> clip.** A self-contained, additive subsystem that takes normalized per-shot
> clips and packages them into **HLS** (`.m3u8`) + **MPEG-DASH** (`.mpd`) over
> **fMP4/CMAF** segments, with a multi-rendition ABR ladder, a master playlist,
> and a **growing/live manifest** that appends shots as they finish — so the
> reader can stream *ahead of render* (kinora.md §5.3 dual-watermark buffer).

Reads: kinora.md **§4.2** (scene = stitch boundary), **§4.4 / §12.4** (the
degradation ladder — one of the providers this normalizes), **§5.3** (the
buffer the live manifest grows behind), **§9.6** (offline stitch — the sibling
that concatenates, where this instead *segments* for ABR).

This package is **additive and self-contained**: it adds no runtime dependency
to any existing module, imports side-effect-free with `DASHSCOPE_API_KEY=test`
and no network/DB, and reuses exactly one existing seam — the render layer's
hardened ffmpeg resolution + runner (`app.render.degrade`) — so there is a
single ffmpeg-discovery path in the codebase.

## Why this exists

The render pipeline produces one mp4 **per shot**, and different shots come from
different models — Wan (DashScope) i2v/t2v at ~16fps, MiniMax at ~25fps, a local
Wan host, and the offline Ken-Burns degradation lane at 30fps. Played back
naively, that is a pile of heterogeneous mp4s: different codecs, frame rates,
and keyframe cadences, with no bitrate ladder and no way to stream the part of
the film that exists while the rest is still rendering. This subsystem turns
that pile into a **single adaptive stream**:

1. **Normalize** every clip onto one grid (H.264/yuv420p, the film fps, a
   **closed GOP whose length divides the segment duration** so an IDR lands on
   every segment boundary — the precondition for seamless ABR switching).
2. **Ladder** — derive 720/540/360/240 renditions clamped to the master (never
   upscale past the mastered geometry).
3. **Segment** into CMAF init + `.m4s` fragments.
4. **Manifest** — emit HLS master + media playlists and a DASH MPD, with a
   **discontinuity at every shot boundary** (HLS `EXT-X-DISCONTINUITY` / a new
   DASH `Period`), because clips from different providers don't share a
   continuous timeline.
5. **Grow / live-window** — while the film is still rendering the media
   playlists are `EVENT`/`dynamic` (no `ENDLIST`); finished shots are appended,
   and an optional sliding window drops shots off the front with a correct
   `MEDIA-SEQUENCE` + `DISCONTINUITY-SEQUENCE`.

## Architecture (pure plan layer + thin ffmpeg layer)

```
              ShotClip (per-shot mp4 + provider + geometry)
                              │
         profiles.profile_for(provider) ──► ProviderProfile
                              │                     │
            ladder.build_ladder(geometry)     normalization_spec(fps, seg)
                  │  Rendition[]                    │  NormalizationSpec
                  └──────────────┬──────────────────┘   (GOP = fps*seg, IDR-aligned)
                                 ▼
   PURE PLAN LAYER  segmenter.build_encode_plan / build_hls_segmenting_plan
                    / build_dash_segmenting_plan / plan_byte_ranges
                                 │  exact ffmpeg arg lists (no exec)
   PURE MANIFEST    manifest.GrowingFilm ──► HLS master+media / DASH MPD
                    + LiveWindow (sliding) ── hls.* / dash.* text builders
                                 │
   THIN FFMPEG      packager.AbrPackager ── runs plans with the resolved binary
   (gated)          (reuses app.render.degrade resolution + run_ffmpeg)
                                 │
   AUTH HOOK        signing.UrlSigner (signed segment/playlist URLs)
                    signing.StreamTokenSigner (per-book playback tokens)
```

* **`ladder.py`** — `Rendition` + `build_ladder` (clamped to the master, even
  dims, monotonic bitrate) + `select_rendition` (server-side mirror of the
  player's ABR pick, for transcode prioritisation). Pure.
* **`profiles.py`** — `ProviderProfile` registry + `profile_for` (forgiving
  resolution incl. model-id families) + `normalization_spec` (enforces
  `fps*segment_duration ∈ ℤ` so IDRs align). Pure.
* **`models.py`** — `ShotClip`, `MediaSegment`, `RenditionTrack`, `InitSegment`,
  `ByteRange`. `ShotClip.segment_durations` does the exact per-shot segment math
  (last segment short; sum == shot duration). Pure data.
* **`segmenter.py`** — the packaging **plan** layer: exact ffmpeg/segmenter arg
  lists + byte-range planning. **Fully unit-tested without ffmpeg.**
* **`hls.py` / `dash.py`** — pure text/XML manifest builders.
* **`manifest.py`** — `GrowingFilm` orchestrates clips → segments → HLS/DASH,
  appendable, with `LiveWindow` for the sliding live form.
* **`packager.py`** — the only module that shells out; ffmpeg-gated.
* **`signing.py`** — HMAC signed URLs + compact playback tokens (stdlib only).

## Testing posture

* The pure plan + manifest layers are tested **without ffmpeg** for manifest
  correctness (segment durations, discontinuity placement, live-window
  growth/slide), ladder selection, provider-profile normalization, byte-range
  math, and signing — all deterministic, no infra/network.
* `tests/test_delivery_packager.py` is **ffmpeg-gated**
  (`skipif(not degrade.ffmpeg_available())`): it produces a real source mp4 via
  the degrade lane and packages it into real CMAF segments that decode.

## Additive shared-file changes

None required. The subsystem is import-only and wires nothing into the
composition root; a caller adapts a rendered `Shot` into a `ShotClip` at the
seam and instantiates `GrowingFilm` / `AbrPackager` directly. The signing layer
takes any secret (derive one from `Settings.jwt_secret` via
`signing.derive_signing_secret` if desired).
