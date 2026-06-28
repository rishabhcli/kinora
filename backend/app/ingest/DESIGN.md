# Phase-A Ingest — industrial-grade pipeline (DESIGN / roadmap)

Owner: ingest-domain agent. Scope = `backend/app/ingest/**` only. Shared files
(`core/config.py`, `db/models`, `composition.py`) are **additive-only** and
recorded at the bottom of this file.

Phase A (kinora.md §9.1) is the cheap, global, token-only import that spends
**zero video-seconds**:

    importing → extract → analyse(+canon) → shot list + source-span index
              → identity lock → ready

This document is the living roadmap. Each numbered milestone is an
independently-landable unit of work; each keeps `make lint` + `make test` green.

---

## Design pillars

1. **Bounded memory at any book size.** A 1 GB PDF / 2000-page novel must ingest
   without buffering every page PNG at once. Extraction streams page-by-page,
   uploading + persisting each page before loading the next, and the in-RAM
   `PdfExtractResult` keeps only lightweight per-page metadata (no PNG bytes).

2. **Format funnel → one PDF path.** Every supported input (PDF, EPUB, TXT,
   Markdown, DOCX, MOBI/AZW3, HTML) is normalised to PDF bytes by a single
   `formats.py` funnel, so the §9.1 extract/analyse/shot-plan stages stay
   byte-for-byte identical. EPUB already did this; we generalise it.

3. **Resumable, checkpointed milestones.** Each heavy stage is a unit-of-work
   that records a durable `IngestCheckpoint` (book_id, milestone). A crashed/
   restarted ingest skips already-completed milestones instead of recomputing
   them. Extraction is idempotent at the page grain.

4. **OCR fallback for scanned pages.** A page whose extractable-word count is far
   below what its rendered area predicts is treated as image-only and routed
   through an OCR provider seam (Qwen-VL transcription by default; a pluggable
   `OcrEngine` so a local Tesseract can drop in). OCR words get synthesised
   bounding boxes so the karaoke layer still works.

5. **Layout / column / reading-order analysis.** Multi-column pages (academic
   PDFs, magazines) break naive top-to-bottom word order. A layout pass clusters
   words into columns and blocks and re-threads reading order, correcting the
   global word index before the source-span index is built.

6. **Parallelism with rate control.** Page analysis already uses a semaphore;
   we add a token-bucket rate limiter + retry/backoff so a back-catalogue ingest
   respects DashScope QPS and survives `429 Throttling.RateQuota` without
   failing the whole book.

7. **Incremental re-ingest / diff.** When a source changes, diff the new
   extraction against the persisted pages (per-page text hash) and re-analyse
   only changed pages, preserving canon/shots for untouched spans.

8. **Fault-injection test suite.** Deterministic crash points (after extract,
   mid-analysis, after shot-plan) prove the pipeline resumes correctly and never
   double-inserts.

---

## Milestones

- [x] **M1 — Format funnel (`formats.py`).** Content-magic detection + TXT /
      Markdown / HTML → PDF (PyMuPDF Story), DOCX → text → PDF, MOBI/AZW3 sniff.
      Pure + unit-tested. The books route can later call `normalize_to_pdf`.
- [x] **M2 — Layout / reading-order (`layout.py`).** Column detection + block
      clustering + reading-order rethread over PyMuPDF word tuples. Pure.
- [x] **M3 — OCR fallback (`ocr.py`).** `OcrEngine` protocol + VL-backed default;
      scanned-page heuristic; synthesised word boxes. Provider-seam.
- [x] **M4 — Streaming extraction (`pdf_extract.py`).** Stream page-by-page with
      bounded memory; PNG bytes never retained; OCR + layout hooks wired in.
- [x] **M5 — Rate control (`ratelimit.py`).** Async token bucket + retrying VL
      wrapper; analyse uses it. Deterministic (injectable clock).
- [x] **M6 — Checkpoints (`checkpoints.py` + model).** Durable milestone ledger;
      `IngestCheckpoint` model (additive migration); service skips done stages.
- [x] **M7 — Incremental re-ingest (`diff.py`).** Per-page text-hash diff.
- [x] **M8 — Fault-injection tests.** Crash-point harness across the pipeline.

---

## Status: all milestones landed (M1–M8 green)

`make lint` (ruff + mypy over 389 files) is clean and the full suite passes
(1112 no-DB + 111 ingest DB tests against the isolated `kinora_ingest_test` DB on
:5433). New ingest test files: `test_ingest_formats`, `test_ingest_layout`,
`test_ingest_ocr`, `test_ingest_ratelimit`, `test_ingest_analyze`,
`test_ingest_extract_features`, `test_ingest_checkpoints`, `test_ingest_diff`,
`test_ingest_resume`, `test_ingest_fault_injection`.

## Follow-up (NOT done — out of my file ownership)

- `app/api/routes/books.py` is owned by another agent. Its `_normalize_upload`
  only accepts PDF/EPUB today. To expose the new TXT/Markdown/HTML/DOCX formats
  end-to-end, that route should delegate to `app.ingest.formats.normalize_to_pdf`
  (already exported). I did **not** edit `books.py` (not in my additive
  allowlist). The funnel is import-ready for whoever owns the route.

## Additive shared-file changes (documented per the parallel-work contract)

- `core/config.py` — appended an `# --- Ingest pipeline (Phase A) ---` block of
  new settings (OCR toggle/thresholds, analyse rate limit + retry, checkpoint
  toggle, layout toggle). Additive only; no existing field touched.
- `db/models/ingest_checkpoint.py` — **new** model module + registered in
  `db/models/__init__.py` (two additive lines: an import + two `__all__`
  entries). New table `ingest_checkpoints`.
- `db/repositories/ingest_checkpoint.py` — **new** repository file (no edits to
  any existing repo).
- `migrations/versions/f3a7c9e1b2d4_ingest_checkpoints.py` — **new** Alembic
  migration, `down_revision = a1b2c3d4e5f6` (the head at branch time). Verified
  upgrade+downgrade on a scratch DB. NOTE: if a sibling agent also adds a
  migration off the same head, Alembic will report multiple heads — resolve with
  a merge migration at integration time.
- No edits to `agents/adapter.py` (round-1 owned); only its public
  `Adapter`/`ShotPlanner` API is called.
