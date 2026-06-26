# Agent 05 — live progress (cross-iteration memory for the Ralph loop)

> Read first. `git log --oneline agent/05-library`. Branch MERGED with overnight/integration.

## Environment
- Worktree `../kinora-a05`, venv `backend/.venv`. Data plane: postgres@**55432**, redis@6379, minio@9000.
- backend/.env → host 55432. Test env: KINORA_TEST_DATABASE_URL=...@55432/kinora_conflict_test,
  _REDIS_URL=redis://localhost:6379/15, _S3_ENDPOINT_URL=http://localhost:9000.
- Alembic head e843aa7682b2 (cover_key). Captain (A12) continuously merges agents.

## Gate status (after re-merging overnight/integration @ 6a39e6c)
- `make lint` → **GREEN** ✅ (ruff + mypy 234 files; captain fixed the sibling errors).
- `make test` → 533 passed pre-merge; re-running post-merge to reconfirm.
- `pnpm --filter @kinora/desktop typecheck && build` → ✅ (incl. a11y fixes).
- a11y: 2 serious axe violations (chip contrast + scrollable shelf) FIXED.

## Seed (the remaining gate for ≥100 books)
- Running in background (nohup): `backend/scripts/seed_library_100.py` → live kinora DB.
- Downloader: mirror fallback (pglaf/aleph/www) + HTTP Range-resume + zip-validate. Gutenberg
  throttles this IP (~12–27KB/s) so it's SLOW (~1 book/min) but reliable. ~36/130 done.
- Check: `docker exec kinora-postgres-1 psql -U kinora -d kinora -tc "select count(*) from books where id like 'pubdom%'"`
- If the seed died, relaunch (idempotent, cached EPUBs skip): `cd backend && nohup .venv/bin/python scripts/seed_library_100.py &`
- Covers: mostly `openlibrary` (HD), some `generated` (typographic fallback). seed-report.json at end.

## Screenshots / app (for re-shoot at ≥100)
- A pre-merge `kinora-api-1` container holds :8000 (NO cover_url). So run my API on **8010**:
  `cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010`
- Renderer: `apps/desktop/.env.local` has `VITE_KINORA_API_URL=http://127.0.0.1:8010`; run
  `cd apps/desktop && node_modules/.bin/vite --port 5173`.
- Capture: `cd apps/desktop && node _a05_capture.mjs` (or coordination/artifacts/agent-05/capture.mjs).
  Outputs 01-library.png + 02-upload.png. Temps (_a05_capture.mjs, .env.local) are UNTRACKED — don't commit.

## TO FINISH (then output <promise>AGENT 05 COMPLETE</promise>)
1. [ ] Seed ≥100 books (wait for background seed). Verify count.
2. [ ] Re-shoot 01-library.png at ≥100 books; commit final artifacts.
3. [ ] Re-merge overnight/integration (pick up captain's sibling-lint fix → make lint green).
4. [ ] Confirm make lint + make test green; typecheck+build green.
5. [ ] Output the promise ONLY when all true. Do NOT false-promise.
