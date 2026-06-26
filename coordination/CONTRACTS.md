# Kinora overnight — cross-agent CONTRACTS

Each agent appends its own clearly-delimited section. Don't edit another agent's
section; stub against these contracts if a producer hasn't merged yet.

---

## Agent 9 — Settings & SF-Symbols icons

### `<Icon>` — the one icon API (`apps/desktop/src/components/icons`)

```tsx
import { Icon } from "@/components/icons";

<Icon
  name={IconName}            // required — SF-Symbols-style, e.g. "book.fill"
  size?={number}             // px, default 18
  weight?="ultralight"|"light"|"regular"|"medium"|"semibold"|"bold"  // default regular
  mode?="monochrome"|"hierarchical"                                  // default monochrome
  className?={string}
  title?={string}            // present → role="img" + <title> (labelled);
                             // absent  → aria-hidden (decorative)
  style?={CSSProperties}
/>
```

- Colour is `currentColor` (inherits text colour / Agent 8 tokens). Crisp at any size.
- **83 names** today (nav, controls, settings, appearance, media, reading, account,
  files, status). Full union: `icons/types.ts`. Live set + copy-picker: `vite` dev →
  `/icon-gallery.html`.
- Icon-only **buttons** still need their own `aria-label` (Agent 6 a11y) — `title`
  is only for a glyph that carries standalone meaning.
- **Adopt it in your own files** using `icons/migration-map.md` (old inline SVG →
  name, per file). Brand logos (Kinora mark, Google/Apple/GitHub) stay as-is.

### Settings store — `apps/desktop/src/lib/settings.ts`

```ts
import { settingsStore, type AppSettings, SETTINGS_DEFAULTS } from "@/lib/settings";
import { useSettings } from "@/lib/useSettings";

settingsStore.get(): AppSettings           // referentially stable until a change
settingsStore.set(patch: Partial<AppSettings>)
settingsStore.reset() / resetKey(key)
settingsStore.subscribe(fn): () => void

const { settings, set, reset, resetKey } = useSettings();  // React hook (useSyncExternalStore)
```

`AppSettings` (persisted at `localStorage["kinora.settings"]` as one JSON blob;
`diffFromDefaults()` is the future backend-sync payload). **Non-reading prefs only:**

| Group | Keys |
|---|---|
| General | `launchView` |
| Appearance | `reduceMotion` `reduceTransparency` `increaseContrast` (each `"system"\|"on"\|"off"`) |
| Playback | `autoplayFilm` `captions` `scrubSensitivity` (0.5–2) |
| Notifications | `notificationsEnabled` `readingReminders` `weeklyDigest` `soundEffects` |
| Privacy | `analytics` `crashReports` |

> **Reading prefs are NOT here.** Theme/font/leading/measure/spacing/auto-night stay
> in **Agent 6's** `useReadingPrefs` (`localStorage["kinora.readingPrefs"]`); the
> Settings ▸ Reading tab composes that hook (verified: changing it writes Agent 6's key).

**Agent 1 / Agent 10 (reading-room player):** read `settingsStore.get().autoplayFilm /
captions / scrubSensitivity` to drive playback.

### Appearance effects — `apps/desktop/src/lib/appearance.ts`
- `startAppearanceSync()` toggles `<html>` classes (`kinora-reduce-motion`,
  `kinora-reduce-transparency`, `kinora-increase-contrast`) + injects a managed
  `<style>` (NOT index.css), staying in sync with the store and the OS media
  queries. SettingsPage calls it on mount.
- **Request → Agent 12 / main.tsx:** call `startAppearanceSync()` once at startup so
  overrides apply on first paint (not just after first Settings visit).
- Framer-driven JS motion should additionally honour `useReducedMotion()` /
  `MotionConfig reducedMotion` (Agent 4) — the injected CSS only neutralises CSS
  animation/transition.

### Settings sections / navigation
- Entry is unchanged: navigate to the **"Settings"** page (Navbar profile menu /
  footer already do this). `SettingsPage` is self-contained — sidebar tablist +
  search; sign-out calls `api.logout()` + reload.
- Section registry (id/label/icon) is `settings/sections.tsx → SETTINGS_SECTIONS`
  if you need to deep-link a category later.

### Consumes (stubbed if absent)
Agent 8 tokens (icon colour via `currentColor` + kinora-* classes) · Agent 6
`useReadingPrefs` + a11y checklist · Agent 4 motion tokens / `useReducedMotion`.

### Shared-seam requests → Agent 12
`package.json` (drop unused `lucide-react` in the final sweep) · `main.tsx`
(`startAppearanceSync()` at boot) · final cross-file inline-SVG → `<Icon>` sweep.

<!-- /Agent 9 -->
