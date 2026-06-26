# Agent 05 — live progress checklist (cross-iteration memory for the Ralph loop)

> Read this first each iteration. Update as work lands. `git log --oneline` on
> `agent/05-library` is the other source of truth.

## Environment (verified iteration 1)
- Worktree: `../kinora-a05` on `agent/05-library` (base `overnight/integration` @ 4863a0c).
- Worktree venv: `backend/.venv` (imports the worktree's `app`). Use it for all backend cmds.
- Data plane (Docker, shared host singleton): postgres@5433, redis@6379, minio@9000 — UP.
- Run the API from the worktree venv (NOT the docker `api` image) so my endpoints are live.
- Alembic head at branch time: `c8f1a2b3d4e5`. Migrations live in `backend/migrations/`.
- Tests build schema via `Base.metadata.create_all` (conftest) — new columns auto-picked-up.
- Network OK (gutendex/gutenberg/openlibrary reachable). KINORA_LIVE_VIDEO stays OFF.
- Reference (untracked in main, NOT in worktree): `seed_public_domain_direct.py` —
  the proven direct-seed pattern (build_book). Copied/adapted into my seeder.

## Workstreams
- [ ] WS2a: `Book.cover_key` model field + alembic migration (down_revision c8f1a2b3d4e5)
- [ ] WS2b: `BookResponse.cover_url` + `_book_response` presign wiring
- [ ] WS2c: `routes/library.py` `GET /books/{id}/cover` + register in main.ROUTERS
- [ ] WS2d: `fetch_hd_covers.py` (Open Library `-L` / Google Books / Wikimedia) + typographic fallback
- [ ] WS1: `seed_library_100.py` — curated 100+ Gutenberg catalog, download EPUBs, catalog.json, direct seed (idempotent, zero-spend)
- [ ] WS1: `assets/books/catalog.json`
- [ ] WS3: `UploadBook.tsx` drag-drop + `lib/api/library.ts` uploadBook + polling
- [ ] WS4: `LibraryPage`/`BookShelf`/`BookCard` rebuild — real backend, search/filter/genre shelves
- [ ] DoD1: `make lint && make test` green; `make migrate` clean
- [ ] DoD2: `pnpm --filter @kinora/desktop typecheck && build` green
- [ ] DoD3: seed ≥100 books w/ HD covers (idempotent, ~zero spend); screenshots → coordination/artifacts/agent-05/
- [ ] DoD4: CONTRACTS.md + STATUS.md updated

## Decisions / notes
- Brainstorming/Plan skills are interactive (need a user) — not viable in the unattended
  loop; following their spirit (design-first, documented) instead. TDD + small green commits.
- Mission text says `artifacts/agent-07/` / `requests/agent-07.md` — treated as a typo for
  agent-05 (my identity is unambiguously Agent 5); using `agent-05` to avoid intruding on Agent 7's lane.
- Cover endpoint = 302 redirect to presigned (cheap, no byte-proxying). Every book gets a
  cover_key (real HD or generated fallback) so nothing is blank.
