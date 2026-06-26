# Agent 10 — Book-Open Film Experience: verification

How the reading-room shell was verified against the Definition of Done, with
`KINORA_LIVE_VIDEO` OFF (the default). Screenshots in this folder.

## How to reproduce
1. `pnpm --filter @kinora/desktop dev:web` (Vite renderer; here it ran on :5174).
2. `node coordination/artifacts/agent-12/verify-driver.mjs` (drives headless
   Chrome-for-Testing — the cached Playwright build — through four scenarios).

The driver simulates the backend in-page (fetch + EventSource stubs) so the
live/mid-ingest paths are exercised without a running stack; the no-backend
scenario stubs `/api/*` as offline.

## Pure-logic unit tests (TDD, Node 26 `node --test`)
`machine` · `fallback` · `crossfade` · `warmupModel` — **44/44 green**.
`pnpm --filter @kinora/desktop typecheck && build` — **green**.

## Scenario results (all PASS)

| Scenario | Screenshot(s) | Result |
|---|---|---|
| **Ready book open** (live path, real clips) | `ready-01-open.png`, `ready-02-scrubbed.png` | Film plays (H.264 720×1280, `readyState 4`, time advancing); "Buffered 12s ahead" live pill; rail shows shot ticks + buffer glow. |
| **Mid-ingest open** (ANALYZING, shots delayed) | `midingest-01-warmup.png`, `midingest-02-revealed.png` | Honest warm-up progress (monotonic step checklist + crew feed) during the load, then resolves seamlessly into a playing film. |
| **No-backend fallback** | `nobackend-01-opening.png`, `nobackend-02-playing.png`, `nobackend-03-scrolled.png` | Cover swings open → dissolves into a playing bundled film; text reads + scrubs; never a black/empty frame. |
| **Close animation** | `close-01-closing.png` | Cover swings shut + room dissolves back to the shelf; reader's place preserved. |
| **Teardown leak check** (open/close ×10) | — | `EventSource` opened **10** == closed **10**; net document keydown listeners **0**; `0` dialogs / `0` videos left mounted. |

## DoD checklist
- [x] `typecheck && build` green.
- [x] Seeded/ready book → fully functional, playing, scrubbable film + opening animation.
- [x] Mid-ingest → progress, not an error.
- [x] Non-existent / backend-less book → degrades gracefully to the bundled film.
- [x] Close animation reverses cleanly.
- [x] No reading-room console errors (only environmental dev-backend CORS from login).
- [x] Clean teardown verified open/close ×10 (no leaked SSE / listeners / nodes).
- [x] Slot contract + open-state machine published in `coordination/CONTRACTS.md`.

## Notes
- H.264 plays in Chrome-for-Testing; in the shipping Electron app it plays natively too.
- The one environmental console error is `/api/auth/login` CORS — the dev backend on
  :8000 only allows origin :5173, but Vite fell back to :5174 here. The app tolerates
  this by design (silent demo fallback); it is not a reading-room error.
