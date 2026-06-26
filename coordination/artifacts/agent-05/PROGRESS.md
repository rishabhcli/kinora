# Agent 05 — live progress checklist (cross-iteration memory for the Ralph loop)

> Read this first each iteration. `git log --oneline agent/05-library` is the other
> source of truth. **My branch is MERGED with overnight/integration** (all 11 agents).

## Environment (verified)
- Worktree `../kinora-a05` on `agent/05-library`. Worktree venv `backend/.venv` (imports worktree app).
- Data plane (Docker singleton): postgres@**55432** (override remaps; NOT 5433), redis@6379, minio@9000.
- backend/.env (gitignored) points host→55432. Run API/scripts from the worktree venv.
- Integration tests: `KINORA_TEST_DATABASE_URL=...@localhost:55432/kinora_conflict_test`,
  `KINORA_TEST_REDIS_URL=redis://localhost:6379/15`, `KINORA_TEST_S3_ENDPOINT_URL=http://localhost:9000`.
  (Reset test schema if a column is added: `DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION vector;`)
- Alembic head: `e843aa7682b2` (my cover_key, merged, single head). DB at head.
- An **Agent 12 captain** session continuously merges agents → overnight/integration.

## DONE ✅
- WS2a cover_key (model+repo+migration e843aa7682b2, round-trips) — merged.
- WS2b/c cover_url on BookResponse + GET /books/{id}/cover (routes/library.py, registered) — merged.
- WS1 catalog: app/library/catalog.py + assets/books/catalog.json (130 books, 12 genres) — merged.
- WS2d covers: app/library/covers.py (OpenLibrary→Google HD + typographic fallback + upscaler).
- WS1 seeder: scripts/seed_library_100.py + seed_public_domain_direct.py (engine) — zero-spend,
  idempotent, bounded shots, sets cover_key. Downloader improved (cache-mirror first, 30s, retry).
- WS3 UploadBook.tsx (drag-drop + validation + polling + announce()).
- WS4 LibraryPage rebuilt (real backend, search/genre/sort/shelves, <main>), BookCard genre tag + kbd.
- lib/api/library.ts + data/catalog.ts. data/books.ts +genre?/era?.
- Merged overnight/integration (all agents). a11y audit from A6 addressed.
- 22 backend tests pass (test_api_library/covers/catalog). My lane ruff+mypy CLEAN.
- Frontend `typecheck && build` GREEN (post-merge). DoD2 ✅.

## REMAINING
- [ ] **Seed ≥100 books** into live DB (DoD3). Run: `backend/.venv/bin/python backend/scripts/seed_library_100.py`
      (idempotent; ~6 seeded already; downloader now faster). Verify `select count(*) from books`.
- [ ] **Screenshots**: populated library + EPUB upload → coordination/artifacts/agent-05/.
      Need API (run from venv: `uvicorn app.main:app`) + the desktop renderer / Playwright.
- [ ] **make lint green**: BLOCKED by 3 pre-existing sibling mypy errors (A7 test_optim_cache,
      A1 test_render_continuity_qa) — flagged to captain (requests/agent-12-from-05.md). NOT my lane.
- [ ] **make test green**: run full suite once seed/infra stable (my 22 pass).
- [ ] Update STATUS.md + CONTRACTS finalize. Re-merge integration before final.

## Promise gate
Output `<promise>AGENT 05 COMPLETE</promise>` ONLY when: ≥100 books seeded + screenshots captured
+ gates green (or sibling blockers cleared by captain). Honest — do not false-promise.
