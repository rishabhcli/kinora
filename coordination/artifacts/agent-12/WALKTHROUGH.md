# Agent 12 — end-to-end walkthrough (DoD #2)

Captured 2026-06-26 against `overnight/integration` HEAD by driving the **built renderer**
(`pnpm --filter @kinora/desktop dev:web`, vite :5173) with headless Chromium (Playwright).
**Zero console errors and zero page errors** across the whole flow — the integrated app
(all 11 agents' surfaces composing together) runs clean.

Run in the app's **offline demo mode** ("Explore the demo library" — the supported
no-backend path; DoD says backend is optional). `KINORA_LIVE_VIDEO` OFF → fallback
Ken-Burns films. The backend path (`make stack-up` + seed) is available (docker is up) but
not required to prove the integration; agents' own backend-state proofs are alongside
(`ready-*`, `midingest-*`, `nobackend-*`, `close-*`).

## Screens (this Captain run)
| Shot | Proves | Agents composed |
|---|---|---|
| `01-login.png` | Login + moving BookWall + sign-in card | **A11** login, **A8** tokens |
| `02-library.png` | Home: featured hero + "Read Live · Public Domain" shelf (Frankenstein, A Christmas Carol, Jekyll & Hyde, Metamorphosis, Yellow Wallpaper, Alice…) | **A5** library, **A4** navbar/motion, **A9** icons, **A8** tokens |
| `03-reading-room.png` | Open book (Frankenstein) → **A6 ReadingControls** (font incl. dyslexia, text size, line spacing/width, brightness, scroll/paged, **read-aloud voice + speed**, reduce-motion/high-contrast/transparency) + A10 "Generating your film" open sequence + A2 "Generating ahead…" film status | **A10** shell, **A2** scroll-film engine (real, via producer swap), **A6** controls, **A8** tokens |
| `05-profile-menu.png` (`05-settings.png`) | Profile dropdown (User · Profile · Settings · Pricing) | **A9**, **A4** navbar |
| `06-settings-page.png` | Full SettingsPage: sidebar (General/Appearance/Reading/Playback/Notifications/Privacy/Account/About) + General panel | **A9** settings + **A8** |

## Verified
- Login → demo library → open book → reading room → settings, all reachable, no errors.
- The **producer swap is live**: the reading room mounts A2's real `ScrollFilmEngine`
  and A6's real `ReadingControls` (not A10's built-in stand-ins) — visible in `03`.
- Reading prefs + read-aloud controls render and are operable (A6, CONTRACTS §3).
- Fallback film path engages (no backend, no live video) — film "generating ahead".

## Notes / honest caveats
- Demo library shows curated public-domain books, not a seeded 100-title set (that needs
  the backend `seed_library_100`). The integration of the library UI is fully exercised.
- Scroll-scrub of the film: the A2 engine is mounted and the agents' `*-scrubbed.png`
  artifacts demonstrate scrubbing; this Captain run captured the open + controls states.
- Reproduce: `pnpm --filter @kinora/desktop dev:web`, then a headless-chromium Playwright
  script clicking "Explore the demo library" → first book → profile → Settings.
