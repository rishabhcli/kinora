# Agent 05 — artifacts (library / covers / EPUB upload)

## Screenshots
- `01-library.png` / `01-library-full.png` — the rebuilt **My Library**: real backend
  books, **HD covers** (Open Library, up-scaled) + designed typographic fallbacks,
  genre shelves, search, genre filter chips, sort, Upload drop-zone. Captured @ 2×
  (Retina). _Preliminary while the seed climbs to 100+; refreshed once the seed
  completes (see `seed-report.json` / `git log`)._
- `02-upload.png` — a **successful EPUB upload** (The Yellow Wallpaper, 30pp): drag →
  optimistic "Importing" placeholder + ingest-status chip. (Oversized books, e.g.
  Pride & Prejudice >300pp, are rejected inline with a friendly "Over the 300-page
  limit" — validation works.)

## How they were captured (reproducible)
Data plane (postgres@55432 / redis / minio) up; then from the worktree:
1. `python backend/scripts/seed_library_100.py` — seed the library (zero model spend).
2. API from the worktree venv: `cd backend && .venv/bin/uvicorn app.main:app --port 8010`
   (8000 is taken by the captain's pre-merge `api` container, which lacks `cover_url`).
3. Renderer: `apps/desktop/.env.local` → `VITE_KINORA_API_URL=http://127.0.0.1:8010`,
   then `node_modules/.bin/vite --port 5173`.
4. `node capture.mjs` (run from `apps/desktop`, resolves `@playwright/test` chromium).

`capture.mjs` here is the script (logs in via the demo entry, opens Library, shoots
the shelf + an upload). `capture-note.txt` records the last run's card/img counts.

## Note on the API container
The fleet `kinora-api-1` container serves **pre-merge** code (no `cover_url`) on
:8000. Once the Captain rebuilds it from `overnight/integration`, covers work on
:8000 directly and the `.env.local` override is unnecessary.
