# OWNERSHIP MAP — this is law

**Principle: exactly one owner per file. New files > edits. Cross-cutting concerns ship as owned primitives others consume, never as scatter-edits.**

An edit outside an agent's lane is **reverted on sight and flagged** in that agent's request queue. Shared seams change **only** through the Captain (A12) via `coordination/requests/agent-12.md`.

| Path / area | Owner |
|---|---|
| `backend/app/render/**`, NEW `render/event_director.py`, `agents/cinematographer.py` | **A1** |
| `apps/desktop/src/reading/ScrollFilmEngine.tsx`, `FilmPane.tsx`, `useScrollFilm.ts`, `timeline.ts` | **A2** |
| NEW `backend/app/routes/films.py`; `apps/desktop/src/lib/api/films.ts` | **A3** |
| `apps/desktop/src/motion/**`; `src/styles/motion.css`; `HomePage.tsx`, `Navbar.tsx`, `AnimatedPageSwitch.tsx`, `FloatingDock.tsx` | **A4** |
| `backend/scripts/seed_*`, NEW `seed_library_100.py`, `fetch_hd_covers.py`; `backend/app/ingest/epub_extract.py`; NEW `backend/app/routes/library.py`; book `cover` migration; `assets/books/**` | **A5** |
| `apps/desktop/src/components/LibraryPage.tsx`, `BookShelf.tsx`, `BookCard.tsx`; `data/books.ts`; NEW `UploadBook.tsx`; `src/lib/api/library.ts` | **A5** |
| `apps/desktop/src/a11y/**`; NEW `reading/ReadingControls.tsx`; `src/styles/a11y.css`; bundled dyslexia font | **A6** |
| `backend/app/optim/**`; NEW `backend/app/routes/metrics.py`; index migration; `apps/desktop/vite.config.ts`; `src/lib/perf.ts`; `coordination/PERF.md` | **A7** |
| `apps/desktop/tailwind.config.js`; `src/styles/tokens.css`, `glass.css`, `base.css`; `apps/desktop/index.html`; `src/assets/fonts/**` | **A8** |
| `apps/desktop/src/components/icons/**`; `SettingsPage.tsx` + `settings/**`; `EditProfilePage.tsx`; `src/lib/settings.ts` | **A9** |
| `apps/desktop/src/reading/ReadingRoom.tsx`, `ReadingRoomShell.tsx`, `FilmLoader.tsx`, `OpenSequence.tsx`, `fallback.ts`; `SkeletonShimmer.tsx` | **A10** |
| `apps/desktop/src/components/LoginPage.tsx`, `BookWall.tsx`, `auth/**`; `App.tsx`; `src/styles/login.css` | **A11** |
| **SHARED SEAMS (Captain):** `src/lib/api.ts`, `src/styles/index.css`, `src/main.tsx`, `package.json`, backend router registration (`app/main.py`/`app/api.py`), `composition.py`, `config.py`, alembic ordering, `coordination/**` | **A12** |

## Dead / unused files — Captain arbitrates deletion
`BlobRainAnimation.tsx`, `RainAnimation.tsx`, `BookTicker.tsx` (Agent 7 may propose deletion). `lucide-react` (installed-but-unused) — Captain removes once no importers remain.

## Re-export shims (Captain-maintained during cutovers)
- When **A10** moves `components/ReadingRoom.tsx` → `reading/ReadingRoom.tsx`, the Captain keeps a re-export shim at the old path until all importers migrate.
- When **A6** moves `lib/readingPrefs.ts` → `a11y/readingPrefs.ts`, the Captain keeps a re-export shim at the old path until all importers migrate.

## Merge order (dependency order — Captain integrates in this sequence)
**A8 (tokens) → A6 (a11y) → A4 (motion) → A9 (icons) → A1 (director/stitch) → A3 (film API) → A2 (scroll engine) → A5 (library) → A10 (reading shell) → A11 (login) → A7 (optim, last).**
