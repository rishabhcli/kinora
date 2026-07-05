# Reading-room redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Kinora desktop reading room into a clean, Apple Books-inspired experience: (1) a buttery native continuous-scroll overhaul, (2) the book opens in its own floating Electron window, (3) the old top bar is replaced by a minimal floating Apple Books-style toolbar, and (4) reading themes/text prefs are rehoused into a clean `Aa` popover. Film renders on the **LEFT**, reading column on the **RIGHT**, never overlapping. This plan covers ONLY spec phases **C → A → B → D**; the MiniMax/backend phases (E/F) are owned by a separate plan.

**Architecture:** Electron main process (`apps/desktop/electron/`) gains an `openBook(bookId)` IPC that spawns a dedicated `BrowserWindow` deep-linked to `#/book/:id`; the React renderer (`apps/desktop/src/`) reads the URL hash at the `App.tsx` root and, when a `#/book/:id` route is present, mounts ONLY the reading room (no library chrome). The reading subsystem (`apps/desktop/src/reading/`) keeps its existing pure scroll-math/timeline core (`timeline.ts`) and imperative FilmPane, but `useScrollFilm.ts` and `ScrollFilmEngine.tsx` are overhauled for native momentum + gentle focus + resize-robust metrics, the film/text columns are swapped (film LEFT), the top bar in `ReadingRoomShell.tsx` is replaced by a new floating `BookToolbar`, and the settings popover is replaced by a new `AaPopover` wrapping the existing `ReadingControls` + `readingPrefs`. No new theme engine; no new scroll-math model — existing pure functions are reused and unit-tested.

**Tech Stack:** Electron 33, React 18, Vite, Tailwind, TypeScript, vitest, Playwright/chromium

## Global Constraints

- **NO automatic git commits.** The user commits only on explicit instruction. Do NOT include `git commit` steps anywhere. At each task boundary, instead of a "Commit" step, run the **verification gate**: `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`, confirm it passes, and leave the changes in the working tree. You MAY run `git add -A` to stage, but NEVER `git commit`.
- **Film is on the LEFT, the reading column is on the RIGHT** (user decision). Text NEVER renders over the video — they live in separate columns with a hairline draggable divider.
- **Reading mechanic stays continuous scroll** (overhauled). Do NOT add pagination. The `readingMode: "paged"` pref remains unimplemented; leave it as a no-op (it is out of scope).
- **Reuse existing prefs/themes.** Dark/Night/Sepia/Paper + font family/size/leading/measure/spacing/brightness + TTS voice/rate all come from `apps/desktop/src/a11y/readingPrefs.ts` and `apps/desktop/src/reading/ReadingControls.tsx`. Do NOT build a new theme engine; only restyle the container.
- **Respect reduced-motion.** Every animation/transition added must be gated on the existing `useReducedMotionPref()` (`apps/desktop/src/a11y/useReducedMotionPref.ts`) value (already threaded through the reading components as `reduce`).
- **Keep the existing IPC security model intact.** New channels go through the frozen allowlists in `apps/desktop/electron/shared/ipc-contract.ts`, a validator in `apps/desktop/electron/core/ipc-router.ts`, a handler in `apps/desktop/electron/services/ipc-handlers.ts`, and the bridge method in `apps/desktop/electron/preload.ts`. Never hand raw `ipcRenderer` to the page.
- **Keep cross-platform window config intact.** The book window must preserve the existing macOS `vibrancy: "under-window"` / Windows 11 `backgroundMaterial: "acrylic"` / Linux `backgroundColor` paths from `WindowManager.spawn`. `titleBarStyle: 'hiddenInset'` is macOS-only and must be applied conditionally.
- **Verification is by DRIVING the renderer, not by screenshots.** OS screencapture is blocked on this machine. Verify behaviour by driving `http://localhost:5173` with the project's installed Playwright chromium (`apps/desktop/node_modules/.bin/playwright`, `@playwright/test`) and asserting on DOM/behaviour. Never assert on pixels for these tasks.
- **Do not delete `BookOpenTransition`.** It exists in TWO places: `apps/desktop/src/motion/` (HomePage-level shelf→center travel, used by `HomePage.tsx`) and `apps/desktop/src/reading/builtin/` (in-room reveal, used via `producers.tsx`). Phase A repurposes the library click to call `openBook` but must keep both components in the tree for the in-window reveal and any other usages.

---

## File Structure

### Created
| Path | Responsibility |
|---|---|
| `apps/desktop/src/reading/focusModel.ts` | Pure, DOM-free focus/scroll helpers extracted for unit testing: `activeParagraphIndex`, `focusOpacity`, `measureParagraphTops`-shaped math. Consumed by the overhauled `ScrollFilmEngine`. |
| `apps/desktop/src/reading/focusModel.test.ts` | vitest unit tests for `focusModel.ts` (pure functions, no DOM). |
| `apps/desktop/src/reading/BookToolbar.tsx` | The new minimal floating Apple Books-style toolbar (Phase B). Left contents/notes pill, centered title, right a passive live/buffer status dot + `[share · Aa · search]` pill + bookmark circle. NO AI-Film toggle (removed per user request). |
| `apps/desktop/src/reading/BookToolbar.test.tsx` | vitest component tests for `BookToolbar` (renders groups, fires handlers, status dot reflects live/buffer). |
| `apps/desktop/src/reading/AaPopover.tsx` | The `Aa` themes/text popover (Phase D) wrapping the existing `ReadingControls`. |
| `apps/desktop/src/reading/NotesPanel.tsx` | The notes/highlights surface (Phase B) — folds highlight mode + saved highlights out of the toolbar. |
| `apps/desktop/src/reading/InBookSearch.tsx` | Best-effort in-book text search panel (Phase B, v1-minimal). |
| `apps/desktop/src/routing/useBookRoute.ts` | Pure hash-route reader: parses `#/book/:id` from `window.location.hash`, subscribes to `hashchange`. Consumed by `App.tsx`. |
| `apps/desktop/src/routing/useBookRoute.test.ts` | vitest unit tests for `useBookRoute` parsing. |
| `apps/desktop/src/reading/BookWindowRoom.tsx` | The book-window entry component: resolves a `Book` by id (backend `getBook` first, demo catalog fallback), then mounts the reading room full-window without library chrome. |
| `apps/desktop/scripts/verify-reading.mjs` | A node script that drives `:5173` with Playwright chromium and asserts the reading-room DOM/behaviour for each phase (replaces blocked screenshots). |

### Modified
| Path | Responsibility / change |
|---|---|
| `apps/desktop/src/reading/useScrollFilm.ts` | Phase C: remove anything that fights native momentum; snappier scrub→play; reduced-motion settle; keep the pure-math delegation to `timeline.ts`. |
| `apps/desktop/src/reading/ScrollFilmEngine.tsx` | Phase C + A: swap columns so the **film is LEFT**; replace the heavy 62% dim with the gentle `focusModel` focus; make paragraph-metric caching resize-robust; keep the ~30Hz scrub throttle. |
| `apps/desktop/src/reading/ReadingRoomShell.tsx` | Phase B + D: delete the old top bar (lines ~209–404), render `<BookToolbar>` instead; move bookmark/highlight/settings state into the toolbar/panels; keep `useRoomChrome`, the progress rail, and the warm-up overlay. |
| `apps/desktop/electron/shared/ipc-contract.ts` | Phase A: add `kinora:book:open` to `InvokeChannels` + the frozen `INVOKE_CHANNELS` allowlist. |
| `apps/desktop/electron/core/ipc-router.ts` | Phase A: add the `bookOpen` validator. |
| `apps/desktop/electron/services/window-manager.ts` | Phase A: `createBookWindow(route)` with `titleBarStyle: 'hiddenInset'` (mac) + a `dimOthers`/`undimAll` helper for library dimming. |
| `apps/desktop/electron/services/ipc-handlers.ts` | Phase A: register the `kinora:book:open` handler → `windows.createBookWindow`. |
| `apps/desktop/electron/preload.ts` | Phase A: change `openBook` from the `pick-book` alias to `(bookId: string) => invoke("kinora:book:open", { bookId })`; expose `isBookWindow` flag. |
| `apps/desktop/src/App.tsx` | Phase A: read the route via `useBookRoute`; when on `#/book/:id`, render `<BookWindowRoom>` (no auth-gate chrome, no `HomePage`). |
| `apps/desktop/src/components/HomePage.tsx` | Phase A: `handleOpen` calls `window.kinora.openBook(book.id)` when running in Electron; falls back to the existing in-app `BookOpenTransition` overlay in the browser (no `window.kinora`). |
| `apps/desktop/electron/shared/window-types.ts` (NEW small type module, or inline in ipc-contract) | Optional: shared `BookWindowOpen` request type. (Folded into `ipc-contract.ts`; no separate file required.) |

---

## Task 1 — Phase C.1: Extract the pure focus model + unit tests

**Goal:** Move the paragraph-focus math out of `ScrollFilmEngine` into a pure, DOM-free module so it can be unit-tested and so the engine's hot path reads cached arrays only. This is the foundation for the gentle-focus + resize-robust overhaul.

**Files:**
- Create `apps/desktop/src/reading/focusModel.ts`
- Create `apps/desktop/src/reading/focusModel.test.ts`

**Interfaces:**
- Produces `activeParagraphIndex(tops: number[], focusContentY: number): number` — the last paragraph whose cached content-top is ≤ the focus line.
- Produces `focusContentY(scrollTop: number, clientHeight: number, focusRatio?: number): number` — the content-space Y of the focus line (default ratio 0.4).
- Produces `focusOpacity(distanceFromActive: number, opts?: { min?: number; falloff?: number }): number` — a gentle opacity ramp (active = 1, neighbours fall off softly toward `min`, default `min` 0.78 — much gentler than the old hard 0.62 two-state).
- Consumed by `ScrollFilmEngine.tsx` (Task 3).

### Steps

- [ ] **Create `apps/desktop/src/reading/focusModel.ts`** with the pure helpers:

```ts
// Pure, DOM-free focus math for the Scroll Film Engine's scroll-paint hot path.
// Extracted from ScrollFilmEngine so it is unit-testable and so the engine's rAF
// loop reads only cached arrays (never layout) per frame. The "gentle focus" ramp
// replaces the old hard two-state 1.0 / 0.62 dim with a soft, Apple-Books-calm
// falloff that keeps off-focus paragraphs comfortably readable.

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** The content-space Y (px, relative to the scroll content's top) of the focus
 *  line — the line a paragraph must cross to become "active". `focusRatio` is the
 *  fraction down the viewport (0.4 = 40%, matching the prior behaviour). */
export function focusContentY(scrollTop: number, clientHeight: number, focusRatio = 0.4): number {
  return scrollTop + clientHeight * focusRatio;
}

/** The active paragraph index: the greatest index whose cached content-top is at
 *  or above the focus line. `tops` MUST be ascending (document order). Returns 0
 *  for an empty array (defensive) or when nothing has crossed the line yet. */
export function activeParagraphIndex(tops: number[], focusY: number): number {
  let best = 0;
  for (let i = 0; i < tops.length; i++) {
    if (tops[i] <= focusY) best = i;
    else break; // ascending → no later top can be ≤ focusY
  }
  return best;
}

export interface FocusOpacityOpts {
  /** the floor opacity far-from-focus paragraphs settle to (default 0.78) */
  min?: number;
  /** how many paragraphs away reach the floor (default 2) */
  falloff?: number;
}

/** A gentle opacity for a paragraph `distance` rows from the active one. The
 *  active paragraph (distance 0) is fully opaque; opacity ramps softly toward
 *  `min` over `falloff` rows and stays at `min` beyond. Direction-agnostic. */
export function focusOpacity(distanceFromActive: number, opts: FocusOpacityOpts = {}): number {
  const min = opts.min ?? 0.78;
  const falloff = Math.max(1, opts.falloff ?? 2);
  const d = Math.abs(distanceFromActive);
  if (d === 0) return 1;
  const t = clamp(d / falloff, 0, 1);
  return 1 - t * (1 - min);
}
```

- [ ] **Create `apps/desktop/src/reading/focusModel.test.ts`** (vitest; this file lives under `src/reading/*.test.ts`, which vitest's `include` covers and whose specific `exclude` entries do NOT list `focusModel`):

```ts
import { describe, it, expect } from "vitest";
import { activeParagraphIndex, focusContentY, focusOpacity } from "./focusModel";

describe("focusContentY", () => {
  it("places the focus line 40% down by default", () => {
    expect(focusContentY(100, 1000)).toBe(500); // 100 + 1000*0.4
  });
  it("honours a custom ratio", () => {
    expect(focusContentY(0, 1000, 0.5)).toBe(500);
  });
});

describe("activeParagraphIndex", () => {
  it("returns the last paragraph whose top crossed the focus line", () => {
    expect(activeParagraphIndex([0, 100, 200, 300], 250)).toBe(2);
  });
  it("returns 0 before any paragraph crosses", () => {
    expect(activeParagraphIndex([100, 200, 300], 50)).toBe(0);
  });
  it("returns the last index when the line is past everything", () => {
    expect(activeParagraphIndex([0, 100, 200], 9999)).toBe(2);
  });
  it("is safe for an empty list", () => {
    expect(activeParagraphIndex([], 100)).toBe(0);
  });
});

describe("focusOpacity", () => {
  it("keeps the active paragraph fully opaque", () => {
    expect(focusOpacity(0)).toBe(1);
  });
  it("never dims below the floor (default 0.78 — gentler than the old 0.62)", () => {
    expect(focusOpacity(5)).toBeCloseTo(0.78, 5);
    expect(focusOpacity(2)).toBeCloseTo(0.78, 5);
  });
  it("ramps softly for near neighbours", () => {
    const one = focusOpacity(1); // 1 - 0.5*(0.22) = 0.89
    expect(one).toBeGreaterThan(0.78);
    expect(one).toBeLessThan(1);
  });
  it("is symmetric (direction-agnostic)", () => {
    expect(focusOpacity(-1)).toBe(focusOpacity(1));
  });
});
```

- [ ] **Run the targeted test:** `pnpm --filter @kinora/desktop exec vitest run src/reading/focusModel.test.ts`
  - Expected: `Test Files 1 passed`, all `focusModel` tests green.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck exits 0; vitest + node tests + electron tests all pass. Leave changes staged (`git add -A`), do NOT commit.

---

## Task 2 — Phase C.2: Overhaul `useScrollFilm` for native momentum + snappier handoff

**Goal:** Make the scroll loop feel native: the rAF loop is a thin DOM adapter over `timeline.ts` (already true), but (a) reduce idle churn so momentum scroll is not throttled, (b) make the scrub→play handoff snappier by lowering the EMA lag, (c) settle immediately under reduced motion, and (d) keep the ~30Hz seek throttle (that lives in `FilmPane`, untouched).

**Files:**
- Modify `apps/desktop/src/reading/useScrollFilm.ts` (whole-file edits to constants + the tick/idle logic; the public `UseScrollFilmArgs`/`ScrollFrame` interface is UNCHANGED so `ScrollFilmEngine` keeps compiling).

**Interfaces:**
- Consumes `computeFrame`, `scrollVelocity`, `schedulerSignal` from `apps/desktop/src/reading/timeline.ts` (unchanged signatures).
- Produces the same `useScrollFilm(args: UseScrollFilmArgs): void` — no interface change; behaviour only.

### Steps

- [ ] **Lower the EMA lag + idle window for a snappier handoff.** In `apps/desktop/src/reading/useScrollFilm.ts`, change the tuning constants (lines ~30–32) from:

```ts
const IDLE_MS = 220; // keep ticking this long after the last scroll, then settle to play
const SCHED_THROTTLE_MS = 150; // scheduler signalling cadence (≈ ReadingRoom's 160ms)
const VELOCITY_ALPHA = 0.35; // EMA smoothing for scrub/play decision (less flicker)
```

to:

```ts
const IDLE_MS = 140; // settle to play sooner after the last scroll → snappier scrub→play handoff
const SCHED_THROTTLE_MS = 150; // scheduler signalling cadence (≈ ReadingRoom's 160ms)
const VELOCITY_ALPHA = 0.5; // EMA smoothing for scrub/play decision (more responsive, still de-flickered)
```

- [ ] **Settle instantly under reduced motion.** Still in `useScrollFilm.ts`, replace the idle-continuation branch at the bottom of `tick` (lines ~111–117):

```ts
      // Keep ticking while recently scrolled or actively scrubbing; otherwise this
      // frame already applied play mode — stop and wait for the next scroll.
      if (now - lastScrollAt.current < IDLE_MS || frame.mode === "scrub") {
        raf.current = requestAnimationFrame(tick);
      } else {
        running.current = false;
      }
```

with a version that does NOT keep spinning to "settle into play" when motion is reduced (under reduced motion the film holds a still frame, so there is nothing to settle):

```ts
      // Keep ticking while recently scrolled or actively scrubbing; otherwise this
      // frame already applied play mode — stop and wait for the next scroll. Under
      // reduced motion the film never plays forward (no settle), so stop as soon as
      // scrubbing ends rather than spinning out the IDLE_MS window.
      const keepAlive = cfg.current.reducedMotion
        ? frame.mode === "scrub"
        : now - lastScrollAt.current < IDLE_MS || frame.mode === "scrub";
      if (keepAlive) {
        raf.current = requestAnimationFrame(tick);
      } else {
        running.current = false;
      }
```

- [ ] **Run the existing pure scroll-math tests** (these cover `computeFrame`/`scrollVelocity`/`schedulerSignal`, the math `useScrollFilm` delegates to — they run via node:test, not vitest):
  `pnpm --filter @kinora/desktop run test:reading`
  - Expected: the `timeline.test.ts` suite prints all assertions passing, no failures.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 3 — Phase C.3: Film LEFT + gentle focus + resize-robust metrics in `ScrollFilmEngine`

**Goal:** Swap the columns so the **film is on the LEFT** and the reading column on the RIGHT; replace the heavy 62% two-state dim with the gentle `focusModel` ramp; make paragraph-metric caching robust to resize (recompute on the scroll element's own resize AND on its content's resize, so a mid-scroll resize never leaves stale tops). Keep continuous scroll and the existing rail/scrub indicator behaviour.

**Files:**
- Modify `apps/desktop/src/reading/ScrollFilmEngine.tsx`:
  - splitter math (lines ~146–169)
  - paragraph paint (`setParaStyle`/`paintParagraph`, lines ~246–275)
  - metric measurement effect (lines ~279–304)
  - the JSX column order in the returned tree (lines ~373–522)

**Interfaces:**
- Consumes `activeParagraphIndex`, `focusContentY`, `focusOpacity` from `apps/desktop/src/reading/focusModel.ts` (Task 1).
- Produces the same `ScrollFilmEngine` component + `ScrollFilmEngineProps` (unchanged props).

### Steps

- [ ] **Import the focus model.** At the top of `apps/desktop/src/reading/ScrollFilmEngine.tsx`, after the `useScrollFilm` import (line ~20), add:

```ts
import { activeParagraphIndex, focusContentY, focusOpacity } from "./focusModel";
```

- [ ] **Replace the two-state paint with the gentle ramp.** Replace `setParaStyle` + `paintParagraph` (lines ~246–275). The old version dims to a flat 0.62 and is two-state; the new version uses a soft falloff and repaints the small band around the active paragraph. Old:

```ts
  const setParaStyle = (p: HTMLElement, active: boolean) => {
    p.style.color = `rgba(${theme.ink}, ${active ? 1 : 0.62})`;
    p.style.borderLeftColor = active ? "rgba(212,164,78,0.7)" : "transparent";
  };

  const paintParagraph = () => {
    const sc = scrollRef.current;
    const nodes = paraNodes.current;
    const tops = paraTops.current;
    if (!sc || nodes.length === 0) return;
    // Active paragraph = last one whose top has crossed the 40% focus line, using
    // cached content offsets + the current scroll position (no layout reads here).
    const focusContentY = sc.scrollTop + sc.clientHeight * 0.4;
    let bestIndex = 0;
    for (let i = 0; i < tops.length; i++) {
      if (tops[i] <= focusContentY) bestIndex = i;
      else break; // paragraphs are in document order, so tops ascend
    }
    if (lastInk.current !== theme.ink) {
      // Theme changed → every paragraph needs the new ink.
      for (let i = 0; i < nodes.length; i++) setParaStyle(nodes[i], i === bestIndex);
      lastInk.current = theme.ink;
    } else if (bestIndex !== lastActive.current) {
      // Only the de-/re-activated paragraphs change.
      const prev = nodes[lastActive.current];
      if (prev) setParaStyle(prev, false);
      setParaStyle(nodes[bestIndex], true);
    }
    lastActive.current = bestIndex;
  };
```

New:

```ts
  // Paint a paragraph at its gentle focus opacity (active = 1, soft falloff toward
  // a comfortable floor). No hard 62% dim and no gold rule — Apple-Books calm. The
  // active paragraph keeps a barely-there weight cue via a hair-thin border.
  const setParaStyle = (p: HTMLElement, distance: number) => {
    p.style.color = `rgba(${theme.ink}, ${focusOpacity(distance)})`;
    p.style.borderLeftColor = distance === 0 ? "rgba(212,164,78,0.35)" : "transparent";
  };

  // The band of paragraphs whose opacity meaningfully differs from the floor; only
  // these need repainting when the active paragraph moves by one (everything beyond
  // is already at the floor). Keep it in sync with focusOpacity's falloff (2).
  const FOCUS_BAND = 3;

  const paintParagraph = () => {
    const sc = scrollRef.current;
    const nodes = paraNodes.current;
    const tops = paraTops.current;
    if (!sc || nodes.length === 0) return;
    const bestIndex = activeParagraphIndex(tops, focusContentY(sc.scrollTop, sc.clientHeight));
    if (lastInk.current !== theme.ink) {
      // Theme (ink) changed → repaint every paragraph against the new ink.
      for (let i = 0; i < nodes.length; i++) setParaStyle(nodes[i], i - bestIndex);
      lastInk.current = theme.ink;
    } else if (bestIndex !== lastActive.current) {
      // Repaint the focus band around BOTH the old and new active rows so the soft
      // ramp updates without touching the whole column.
      const lo = Math.min(bestIndex, lastActive.current) - FOCUS_BAND;
      const hi = Math.max(bestIndex, lastActive.current) + FOCUS_BAND;
      for (let i = Math.max(0, lo); i <= Math.min(nodes.length - 1, hi); i++) {
        setParaStyle(nodes[i], i - bestIndex);
      }
    }
    lastActive.current = bestIndex;
  };
```

- [ ] **Make metric caching resize-robust.** Replace the measurement effect (lines ~279–304). The old version observes only the scroll element (`ro.observe(sc)`); if the *content* reflows (font swap finishing, video pane resize pushing text) without the scroll element resizing, tops go stale. New version observes both the scroll element and its content wrapper, and re-measures on `window` resize too. Old:

```ts
  // Measure paragraph offsets after layout and whenever the text or a layout-
  // affecting reading pref changes; a ResizeObserver keeps them fresh on resize.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    const measure = () => {
      const nodes = Array.from(sc.querySelectorAll<HTMLElement>("[data-para]"));
      paraNodes.current = nodes;
      const contentTop = sc.getBoundingClientRect().top - sc.scrollTop;
      paraTops.current = nodes.map((n) => n.getBoundingClientRect().top - contentTop);
      lastActive.current = -1; // force a repaint against the fresh geometry
      lastInk.current = "";
      paintParagraph();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(sc);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    paragraphs.length,
    prefs.fontScale,
    prefs.leading,
    prefs.spacing,
    prefs.measure,
    prefs.fontFamily,
    themeKey,
  ]);
```

New:

```ts
  // Measure paragraph offsets after layout and whenever the text or a layout-
  // affecting reading pref changes. Resize robustness [C]: observe the scroll
  // element AND its content wrapper (a mid-scroll reflow that doesn't resize the
  // scroller — e.g. a font finishing loading, or the film pane resizing the row —
  // still re-measures), plus window resize. rAF-coalesced so a burst of resize
  // callbacks measures once per frame, never mid-paint.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    let pending = 0;
    const measureNow = () => {
      pending = 0;
      const nodes = Array.from(sc.querySelectorAll<HTMLElement>("[data-para]"));
      paraNodes.current = nodes;
      const contentTop = sc.getBoundingClientRect().top - sc.scrollTop;
      paraTops.current = nodes.map((n) => n.getBoundingClientRect().top - contentTop);
      lastActive.current = -1; // force a repaint against the fresh geometry
      lastInk.current = "";
      paintParagraph();
    };
    const measure = () => {
      if (pending) return;
      pending = requestAnimationFrame(measureNow);
    };
    measureNow();
    const ro = new ResizeObserver(measure);
    ro.observe(sc);
    const content = sc.querySelector<HTMLElement>("[data-reading-content]");
    if (content) ro.observe(content);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
      if (pending) cancelAnimationFrame(pending);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    paragraphs.length,
    prefs.fontScale,
    prefs.leading,
    prefs.spacing,
    prefs.measure,
    prefs.fontFamily,
    themeKey,
  ]);
```

- [ ] **Tag the content wrapper** so the ResizeObserver above can find it. In the returned JSX, the paragraphs are wrapped by the `<div className="mx-auto" style={{ maxWidth ... }}>` block (line ~393). Add `data-reading-content` to that div:

```tsx
            <div
              className="mx-auto"
              data-reading-content
              style={{
```

- [ ] **Set the initial per-paragraph opacity from the gentle ramp** so the first paint matches (avoids a flash from the old hard-coded `0.92 / 0.62`). Replace the paragraph map's inline `color` (line ~413) inside the `paragraphs.map(...)`:

```tsx
                    style={{
                      color: `rgba(${theme.ink}, ${i === 0 ? 0.92 : 0.62})`,
```

with:

```tsx
                    style={{
                      color: `rgba(${theme.ink}, ${focusOpacity(i)})`,
```

  and change the paragraph transition (line ~418) from `"color 0.4s ease"` to a snappier `"color 0.25s ease"`:

```tsx
                      transition: reduce ? "none" : "color 0.25s ease",
```

- [ ] **Flip the columns so the film is LEFT.** This is the load-bearing layout change. Two edits:

  1. **Update the splitter drag math** (lines ~146–155). Film is now LEFT, so the new film width = distance from the container's LEFT edge to the mouse. Old:

```ts
      const rect = container.getBoundingClientRect();
      // Film is on the right; new width = distance from mouse to container's right edge.
      const newWidth = rect.right - e.clientX;
      setFilmWidth(Math.max(FILM_MIN, Math.min(FILM_MAX, newWidth)));
```

  New:

```ts
      const rect = container.getBoundingClientRect();
      // Film is on the LEFT; new width = distance from the container's left edge to
      // the cursor.
      const newWidth = e.clientX - rect.left;
      setFilmWidth(Math.max(FILM_MIN, Math.min(FILM_MAX, newWidth)));
```

  2. **Reorder the three flex children** in the returned tree so the render order is: film pane → splitter → text+rail. Currently the order (lines ~374–522) is: `[text+rail block]`, then `[splitter]`, then `[film block]`. Move the **entire film block** (the `{/* Pinned vertical film (720×1280 / 9:16) */}` `<div className="flex-shrink-0 self-start" style={{ width: filmWidth }}>…</div>`, lines ~473–521) to be the FIRST child inside `<div ref={containerRef} …>`, before the text+rail block. Keep the splitter between film and text. The resulting child order must be:

```tsx
    <div ref={containerRef} className="mx-auto flex min-h-0 w-full max-w-[1180px] flex-1 items-stretch overflow-hidden px-8 py-8">
      {/* Pinned vertical film — now on the LEFT */}
      <div className="flex-shrink-0 self-start" style={{ width: filmWidth }}>
        … (unchanged film-pane subtree) …
      </div>

      {/* Draggable splitter — drag right to expand film, left to shrink */}
      <div
        onMouseDown={onSplitterDown}
        className="group relative flex-shrink-0 cursor-col-resize select-none"
        style={{ width: 6, marginLeft: 10, marginRight: 10 }}
        aria-label="Drag to resize video and text panels"
        role="separator"
        aria-orientation="vertical"
      >
        … (unchanged splitter subtree) …
      </div>

      {/* Scrolling book text + reading-progress rail — now on the RIGHT */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        … (unchanged text + rail subtree) …
      </div>
    </div>
```

  Update the splitter's `aria-label` comment text only (the `aria-label` string itself can stay "Drag to resize video and text panels"). The text scroll column keeps `pr-6`; consider it cosmetic — leave padding as-is for this task.

- [ ] **Run the engine's existing tests** (none import the DOM paint directly, but confirm nothing in the reading vitest set broke):
  `pnpm --filter @kinora/desktop exec vitest run src/reading`
  - Expected: all included reading vitest files pass (e.g. `clipCache.test.ts`, `ReadingControls.test.tsx`, `focusModel.test.ts`).

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 4 — Phase C.4: Render-driving verification of the scroll overhaul

**Goal:** Prove the overhaul behaves at runtime by driving the dev renderer with Playwright chromium (screenshots are blocked). Create a reusable verification script that opens a book, settles into reading, scrolls, and asserts: (a) the film pane `<video>` is to the LEFT of the scroll column, (b) the scroll container scrolls natively without page errors, (c) the active paragraph's opacity is higher than a far paragraph's (gentle focus is applied), (d) no `pageerror`.

**Files:**
- Create `apps/desktop/scripts/verify-reading.mjs`

**Interfaces:**
- Consumes the running dev server at `http://localhost:5173` and `@playwright/test`'s `chromium` (already installed at `apps/desktop/node_modules`).
- Produces a `node` script that exits 0 on success, non-zero on the first failed assertion, printing a per-check log.

> NOTE: This script drives the BROWSER renderer (no Electron, so `window.kinora` is undefined → `handleOpen` uses the in-app overlay path; see Task 9). That is exactly the deterministic, screenshot-free surface the e2e suite already uses. The same script is extended in later phases.

### Steps

- [ ] **Create `apps/desktop/scripts/verify-reading.mjs`:**

```js
// Render-driving verification for the reading-room redesign. Screenshots are
// blocked on this machine, so we assert on DOM/behaviour instead. Requires the
// Vite dev renderer running at http://localhost:5173 (pnpm --filter
// @kinora/desktop run dev:web). Runs against the BROWSER surface, where
// window.kinora is undefined, so a book opens via the in-app overlay path.
//
//   node apps/desktop/scripts/verify-reading.mjs
import { chromium } from "@playwright/test";

const BASE = process.env.KINORA_VERIFY_URL ?? "http://localhost:5173";
const checks = [];
const check = (name, cond) => {
  checks.push({ name, ok: !!cond });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

await page.goto(BASE, { waitUntil: "domcontentloaded" });

// Enter the demo library (LoginPage → "explore the demo library"), then open the
// first book on the public-domain shelf.
const demo = page.getByRole("button", { name: /explore the demo library/i });
if (await demo.count()) await demo.first().click();

// Open a book: click the first book cover on the shelf.
await page.locator(".book-cover").first().click({ timeout: 20000 }).catch(() => {});

// Wait until the reading scroll container is present (warm-up settled).
const scroll = page.locator('[data-reading-scroll]').first();
await scroll.waitFor({ state: "visible", timeout: 30000 });
check("reading scroll container is visible", await scroll.isVisible());

// (a) film <video> is LEFT of the scroll column.
const layout = await page.evaluate(() => {
  const sc = document.querySelector('[data-reading-scroll]');
  const vid = document.querySelector('video');
  if (!sc || !vid) return null;
  return { videoLeft: vid.getBoundingClientRect().left, scrollLeft: sc.getBoundingClientRect().left };
});
check("film <video> renders LEFT of the reading column", layout && layout.videoLeft < layout.scrollLeft);

// (b) native scroll moves scrollTop.
const before = await scroll.evaluate((el) => el.scrollTop);
await scroll.evaluate((el) => el.scrollBy({ top: 1200 }));
await page.waitForTimeout(300);
const after = await scroll.evaluate((el) => el.scrollTop);
check("scrolling moves scrollTop (native, not jacked)", after > before);

// (c) gentle focus: an in-focus paragraph is more opaque than a far one.
const opacity = await page.evaluate(() => {
  const paras = Array.from(document.querySelectorAll('[data-para]'));
  if (paras.length < 6) return null;
  const alpha = (el) => {
    const m = /rgba?\([^)]*?,\s*([\d.]+)\s*\)/.exec(getComputedStyle(el).color);
    return m ? parseFloat(m[1]) : 1;
  };
  // The active one near the 40% focus line vs. the very first (far above).
  const mid = paras[Math.floor(paras.length / 2)];
  return { mid: alpha(mid), far: alpha(paras[0]) };
});
check("gentle focus applied (active paragraph more opaque than a far one)", opacity && opacity.mid >= opacity.far);

check("no uncaught page errors during scroll", errors.length === 0);
if (errors.length) console.log("  errors:", errors.join("\n  "));

await browser.close();
const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
```

- [ ] **Start the dev renderer in one shell** (leave running): `pnpm --filter @kinora/desktop run dev:web`
  - Expected: Vite prints `Local: http://localhost:5173/`.

- [ ] **Run the verifier in another shell:** `node apps/desktop/scripts/verify-reading.mjs`
  - Expected output ends with `6/6 checks passed` and the process exits 0. Specifically `PASS  film <video> renders LEFT of the reading column`, `PASS  scrolling moves scrollTop (native, not jacked)`, `PASS  gentle focus applied …`.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit. (The `.mjs` script is not typechecked by `tsc --noEmit`; that is fine.)

---

## Task 5 — Phase A.1: `kinora:book:open` IPC contract + validator

**Goal:** Add the typed, allowlisted IPC channel that the renderer will call to open a book in its own window. This is contract-only (channel + validator); the handler and bridge come next.

**Files:**
- Modify `apps/desktop/electron/shared/ipc-contract.ts` (add channel type ~line 49 and allowlist entry ~line 186)
- Modify `apps/desktop/electron/core/ipc-router.ts` (add validator in the `v` object ~line 152)

**Interfaces:**
- Produces channel `"kinora:book:open"`: `{ request: { bookId: string }; response: { id: number } }` (returns the new window's `webContents.id`).
- Produces validator `v.bookOpen`.

### Steps

- [ ] **Add the channel type.** In `apps/desktop/electron/shared/ipc-contract.ts`, inside `interface InvokeChannels`, after the existing `"kinora:window:open"` entry (line ~49), add:

```ts
  /** Open a book in its own dedicated window (deep-links to #/book/:id). */
  "kinora:book:open": { request: { bookId: string }; response: { id: number } };
```

- [ ] **Add it to the frozen allowlist.** In the same file, in `INVOKE_CHANNELS` (lines ~172–187), add `"kinora:book:open"` after `"kinora:window:open"`:

```ts
  "kinora:window:open",
  "kinora:book:open",
  "kinora:open-external",
```

- [ ] **Add the validator.** In `apps/desktop/electron/core/ipc-router.ts`, in the `v` object (after `windowOpen`, line ~153), add:

```ts
  bookOpen: (p: unknown): p is InvokeChannels["kinora:book:open"]["request"] =>
    isObj(p) && isStr(p.bookId) && p.bookId.length > 0,
```

- [ ] **Run the electron contract tests** (these assert the allowlist + validators are coherent):
  `pnpm --filter @kinora/desktop exec node electron/__tests__/run-electron-tests.mjs`
  - Expected: all electron tests pass, including `ipc-contract.test.mjs` and `ipc-router.test.mjs`.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 6 — Phase A.2: Book window in `WindowManager` (hiddenInset + dim/undim)

**Goal:** Add `createBookWindow(route)` to the window manager: a window with `titleBarStyle: 'hiddenInset'` on macOS (native traffic lights over the toolbar) that preserves the existing vibrancy/acrylic config, plus a `dimOthers(except)` / `undimAll()` pair so the library window visibly dims while a book window is open and undims on close. Multiple book windows are allowed.

**Files:**
- Modify `apps/desktop/electron/services/window-manager.ts`:
  - `spawn` options (lines ~86–113) to accept a `book` flag adding `titleBarStyle`
  - add `createBookWindow`, `dimOthers`, `undimAll`, and track book windows
  - close handler (line ~125) to undim when the last book window closes

**Interfaces:**
- Produces `WindowManager.createBookWindow(route: string): BrowserWindow`.
- Produces `WindowManager.dimOthers(except: BrowserWindow): void` and `WindowManager.undimAll(): void` (implemented via a renderer broadcast — see below — so dimming is a renderer overlay, robust cross-platform).
- Consumes the existing `broadcast(channel, payload)` already on `WindowManager` (line ~214) — but dimming is targeted, so we send to specific windows.

> DESIGN NOTE: "Dimming the library" is done by sending a `kinora:window:dim` event to the library window's renderer, which paints a translucent overlay. This is cross-platform and avoids OS-specific opacity quirks. It requires a new EVENT channel (Task 6b within this task).

### Steps

- [ ] **Add the dim EVENT channel to the contract.** In `apps/desktop/electron/shared/ipc-contract.ts`, in `interface EventChannels` (after `"kinora:menu-action"`, line ~79) add:

```ts
  /** Ask a window's renderer to show/hide a "another window is focused" dim veil. */
  "kinora:window:dim": { payload: { dim: boolean } };
```

  and add `"kinora:window:dim"` to the frozen `EVENT_CHANNELS` array (lines ~194–200):

```ts
  "kinora:menu-action",
  "kinora:window:dim",
```

- [ ] **Track book windows + add the create/dim/undim methods.** In `apps/desktop/electron/services/window-manager.ts`, add a field next to `private readonly windows` (line ~44):

```ts
  private readonly bookWindows = new Set<BrowserWindow>();
```

  Then add these methods to the `WindowManager` class (e.g. right after `createWindow`, line ~84):

```ts
  /** Create a dedicated BOOK window: native traffic-light inset on macOS so the
   *  Apple-Books-style toolbar can host them, while preserving the existing
   *  vibrancy/acrylic/background config. Dims every other window while it's open. */
  createBookWindow(route: string): BrowserWindow {
    const focused = this.focused();
    const displays = this.displayRects();
    const area = (displays[0] ?? { bounds: { x: 0, y: 0, width: 1280, height: 800 }, id: 0 }).bounds;
    let bounds: Bounds;
    if (focused && !focused.isDestroyed()) {
      bounds = cascadeFrom(focused.getBounds(), area);
    } else {
      bounds = reconcileWindowState(null, displays).bounds;
    }
    const win = this.spawn(bounds, { route, book: true });
    this.bookWindows.add(win);
    win.on("closed", () => {
      this.bookWindows.delete(win);
      if (this.bookWindows.size === 0) this.undimAll();
    });
    // Re-dim the library while ANY book window is the focused one.
    win.on("focus", () => this.dimOthers(win));
    win.once("ready-to-show", () => this.dimOthers(win));
    return win;
  }

  /** Tell every window EXCEPT `except` to paint its dim veil. */
  dimOthers(except: BrowserWindow): void {
    for (const w of this.windows) {
      if (w.isDestroyed()) continue;
      w.webContents.send("kinora:window:dim", { dim: w !== except });
    }
  }

  /** Clear the dim veil everywhere (e.g. the last book window closed). */
  undimAll(): void {
    for (const w of this.windows) {
      if (!w.isDestroyed()) w.webContents.send("kinora:window:dim", { dim: false });
    }
  }
```

- [ ] **Teach `spawn` about the `book` flag.** In `spawn`'s signature (line ~87), extend the opts:

```ts
  private spawn(
    bounds: Bounds,
    opts: { route?: string; restorePrimary?: WindowState; book?: boolean },
  ): BrowserWindow {
```

  Then, in the `new BrowserWindow({ ... })` options (lines ~93–113), add `titleBarStyle` conditionally on macOS book windows. Insert immediately after the `...(isWin ? { backgroundMaterial: "acrylic" as const } : {}),` line (line ~103):

```ts
      ...(isMac && opts.book ? { titleBarStyle: "hiddenInset" as const } : {}),
```

  (Windows/Linux book windows keep `frame: true` + their existing material — no regression.)

- [ ] **Run the electron tests + the window-state test:**
  `pnpm --filter @kinora/desktop exec node electron/__tests__/run-electron-tests.mjs`
  - Expected: all electron tests pass (the new methods are not directly unit-tested here, but nothing existing breaks; `window-state.test.mjs` still passes).

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0 (the electron tsconfig compiles `window-manager.ts`); all suites pass. Stage, do not commit.

---

## Task 7 — Phase A.3: Handler + preload bridge for `openBook`

**Goal:** Wire the `kinora:book:open` channel end to end: register the main-process handler (→ `createBookWindow('#/book/'+bookId)`) and change the preload `window.kinora.openBook` from the old `pick-book` alias to the real call. Also expose an `isBookWindow` flag so the renderer can tell it is in a book window (set via a query the route carries; simplest: the renderer infers it from the hash, so the bridge flag is optional — we expose `platform`/`isNativeGlass` already). We keep the bridge minimal and add `openBook(bookId)`.

**Files:**
- Modify `apps/desktop/electron/services/ipc-handlers.ts` (register handler, ~after line 109)
- Modify `apps/desktop/electron/preload.ts` (replace `openBook`, lines ~71–73)

**Interfaces:**
- Produces `window.kinora.openBook(bookId: string): Promise<{ id: number }>`.
- Consumes `deps.windows.createBookWindow` (Task 6).

### Steps

- [ ] **Register the handler.** In `apps/desktop/electron/services/ipc-handlers.ts`, after the `"kinora:window:open"` handler block (lines ~102–109), add:

```ts
  router.handle(
    "kinora:book:open",
    ({ bookId }) => {
      const win = deps.windows.createBookWindow(`#/book/${encodeURIComponent(bookId)}`);
      return { id: win.webContents.id };
    },
    v.bookOpen,
  );
```

- [ ] **Replace the preload `openBook` alias.** In `apps/desktop/electron/preload.ts`, the current bridge (lines ~71–73) is:

```ts
  pickBook: () => invoke("kinora:pick-book"),
  /** Compat alias mirroring the native shell's `openBook` entry point. */
  openBook: () => invoke("kinora:pick-book"),
```

  Replace the `openBook` line with the real, parameterised call:

```ts
  pickBook: () => invoke("kinora:pick-book"),
  /** Open a book in its own window (deep-links the new window to #/book/:id). */
  openBook: (bookId: string) => invoke("kinora:book:open", { bookId }),
```

- [ ] **Run the electron tests** (asserts the router still has every handler):
  `pnpm --filter @kinora/desktop exec node electron/__tests__/run-electron-tests.mjs`
  - Expected: pass. The `registerIpcHandlers` `missing()` check covers `kinora:book:open` now having a handler.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 8 — Phase A.4: Hash route reader + book-window room mount

**Goal:** In the renderer, parse `#/book/:id` and (when present) mount ONLY the reading room. Add a pure `useBookRoute` hook (unit-tested), a `BookWindowRoom` component that resolves the `Book` by id (backend first, demo catalog fallback) and renders the reading room full-window, and a renderer-side dim-veil listener so the library window paints a veil when told to.

**Files:**
- Create `apps/desktop/src/routing/useBookRoute.ts`
- Create `apps/desktop/src/routing/useBookRoute.test.ts`
- Create `apps/desktop/src/reading/BookWindowRoom.tsx`

**Interfaces:**
- Produces `useBookRoute(): { bookId: string | null }` — reads `window.location.hash`, subscribes to `hashchange`.
- Produces `parseBookRoute(hash: string): string | null` (pure; the unit-tested core).
- Produces `<BookWindowRoom bookId={string} />`.
- Consumes `api.getBook`, `api.getPageCached`, `api.toUiBook`, `api.toBrowserUrl` from `apps/desktop/src/lib/api.ts`; the demo catalog from `apps/desktop/src/data/books.ts`; and `ReadingRoom` from `apps/desktop/src/components/ReadingRoom.tsx` (note: `ReadingRoom` actually lives at `apps/desktop/src/reading/ReadingRoom.tsx`, imported by `HomePage` as `./ReadingRoom` from `src/components/` — confirm the path when wiring; the file is `apps/desktop/src/reading/ReadingRoom.tsx`).

> RESOLUTION on the `ReadingRoom` import path: `HomePage.tsx` does `const ReadingRoom = lazy(() => import("./ReadingRoom"))` from `src/components/`, but the file is `src/reading/ReadingRoom.tsx`. Check whether `src/components/ReadingRoom.tsx` is a re-export shim. If it is, import `../components/ReadingRoom` from `BookWindowRoom`; otherwise import `./ReadingRoom` relative to `src/reading/`. (See the first step.)

### Steps

- [ ] **Confirm the `ReadingRoom` module path.** Run: `ls apps/desktop/src/components/ReadingRoom.tsx apps/desktop/src/reading/ReadingRoom.tsx 2>/dev/null` and, if `src/components/ReadingRoom.tsx` exists, read it to see whether it re-exports `../reading/ReadingRoom`. Use whichever resolves; the steps below import from `../reading/ReadingRoom` (the real file) since `BookWindowRoom.tsx` lives in `src/reading/` → import `./ReadingRoom`.

- [ ] **Create `apps/desktop/src/routing/useBookRoute.ts`:**

```ts
import { useEffect, useState } from "react";

/** Parse `#/book/:id` out of a location hash. Returns the (URI-decoded) book id,
 *  or null when the hash is not a book route. Pure + unit-testable. */
export function parseBookRoute(hash: string): string | null {
  // Accept "#/book/abc", "#book/abc", with or without a trailing slash.
  const m = /^#\/?book\/([^/?#]+)/.exec(hash);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return m[1];
  }
}

/** React hook: the current book id from the URL hash, kept in sync with
 *  hashchange. null ⇒ not on a book route (render the library/home). */
export function useBookRoute(): { bookId: string | null } {
  const [bookId, setBookId] = useState<string | null>(() =>
    typeof window === "undefined" ? null : parseBookRoute(window.location.hash),
  );
  useEffect(() => {
    const onHash = () => setBookId(parseBookRoute(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return { bookId };
}
```

- [ ] **Create `apps/desktop/src/routing/useBookRoute.test.ts`:**

```ts
import { describe, it, expect } from "vitest";
import { parseBookRoute } from "./useBookRoute";

describe("parseBookRoute", () => {
  it("parses #/book/:id", () => {
    expect(parseBookRoute("#/book/abc123")).toBe("abc123");
  });
  it("parses #book/:id (no leading slash)", () => {
    expect(parseBookRoute("#book/xyz")).toBe("xyz");
  });
  it("URI-decodes the id", () => {
    expect(parseBookRoute("#/book/a%20b")).toBe("a b");
  });
  it("stops at a trailing slash or query", () => {
    expect(parseBookRoute("#/book/abc/extra")).toBe("abc");
    expect(parseBookRoute("#/book/abc?x=1")).toBe("abc");
  });
  it("returns null for non-book routes", () => {
    expect(parseBookRoute("")).toBeNull();
    expect(parseBookRoute("#/library")).toBeNull();
    expect(parseBookRoute("#/")).toBeNull();
  });
});
```

- [ ] **Create `apps/desktop/src/reading/BookWindowRoom.tsx`** — resolves the book by id and mounts the reading room without library chrome. It silently re-auths as the demo user (mirroring HomePage) so the backend `getBook` works in a fresh window, and falls back to the bundled demo catalog by id:

```tsx
import { useEffect, useState } from "react";
import { api, toUiBook, toBrowserUrl, ApiError, type BookResponse } from "../lib/api";
import type { Book } from "../data/books";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
} from "../data/books";
import ReadingRoom from "./ReadingRoom";

const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;

const DEMO_CATALOG: Book[] = [
  ...continueReading,
  ...recentlyAdded,
  ...popularOnKinora,
  ...recommended,
  ...awardWinners,
];

async function resolveBook(bookId: string): Promise<Book | null> {
  // Demo / bundled catalog first (instant, offline-safe).
  const local = DEMO_CATALOG.find((b) => b.id === bookId);
  if (local) return local;
  // Otherwise the real backend (a live library book). Recover a stale token.
  try {
    if (!api.isAuthed()) await api.loginOrRegister(DEMO.email, DEMO.password).catch(() => {});
    let meta: BookResponse;
    try {
      meta = await api.getBook(bookId);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        api.logout();
        await api.loginOrRegister(DEMO.email, DEMO.password);
        meta = await api.getBook(bookId);
      } else throw e;
    }
    let cover = "";
    try {
      cover = toBrowserUrl((await api.getPageCached(meta.id, 1)).image_url);
    } catch {
      /* no page image yet */
    }
    return toUiBook(meta, cover);
  } catch {
    return null;
  }
}

/** The book-window entry: resolves the routed book id and mounts ONLY the reading
 *  room (no library chrome). Closing the window is owned by the OS chrome; the
 *  in-room Back/Escape closes the renderer's reading room → returns to a minimal
 *  empty state (the window is meant to be closed by the user). */
export default function BookWindowRoom({ bookId }: { bookId: string }) {
  const [book, setBook] = useState<Book | null>(null);
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    let alive = true;
    setResolved(false);
    resolveBook(bookId).then((b) => {
      if (!alive) return;
      setBook(b);
      setResolved(true);
    });
    return () => {
      alive = false;
    };
  }, [bookId]);

  if (!resolved) {
    return (
      <div className="kinora-bg flex h-screen items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-white/10 border-t-white/30" />
      </div>
    );
  }
  if (!book) {
    return (
      <div className="kinora-bg flex h-screen items-center justify-center text-kinora-muted">
        Couldn’t open this book.
      </div>
    );
  }
  // Close in a book window: close the OS window if available, else clear the hash.
  const onClose = () => {
    if (typeof window !== "undefined") window.close();
  };
  return (
    <div className="kinora-bg h-screen w-screen overflow-hidden">
      <ReadingRoom book={book} onClose={onClose} />
    </div>
  );
}
```

> NOTE: `ReadingRoom` (in `src/reading/ReadingRoom.tsx`) wraps the room in the in-room `BookOpenTransition` (the builtin reveal) — that is the reveal we keep for the window. It accepts `{ book, onClose, originRect? }`; we pass no `originRect` (the window IS the travel).

- [ ] **Run the routing tests:** `pnpm --filter @kinora/desktop exec vitest run src/routing/useBookRoute.test.ts`
  - Expected: all 5 `parseBookRoute` tests pass.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 9 — Phase A.5: Route at the App root + library opens via `openBook`

**Goal:** Make `App.tsx` branch on the hash route: on `#/book/:id` mount `<BookWindowRoom>` (no auth gate, no `HomePage`); otherwise keep today's behaviour. Make the library click open a book window in Electron (`window.kinora.openBook`) while keeping the in-app overlay path in the browser. Add the dim-veil listener to the main/library window. Keep `BookOpenTransition` intact for the in-room reveal and the browser path.

**Files:**
- Modify `apps/desktop/src/App.tsx` (top of `App()`, add route branch)
- Modify `apps/desktop/src/components/HomePage.tsx` (`handleOpen`, lines ~76–81; add dim-veil listener)

**Interfaces:**
- Consumes `useBookRoute` (Task 8), `BookWindowRoom` (Task 8), `window.kinora.openBook` (Task 7).
- Produces the dim-veil overlay in the library window driven by the `kinora:window:dim` event (Task 6).

### Steps

- [ ] **Branch `App.tsx` on the route.** In `apps/desktop/src/App.tsx`, add the imports near the top (after line ~13):

```ts
import { useBookRoute } from "./routing/useBookRoute";
const BookWindowRoom = lazy(() => import("./reading/BookWindowRoom"));
```

  Then, at the very start of the `App()` function body (before `const [entered, setEntered] = ...`, line ~18), add:

```ts
  const { bookId } = useBookRoute();
```

  And immediately after the hooks but before the existing `return (`, add a short-circuit render for the book route (a book window has no login/home chrome):

```ts
  // A dedicated book window deep-links to #/book/:id — render ONLY the reading
  // room (no auth gate, no library chrome). The window is its own surface.
  if (bookId) {
    return (
      <IntlProvider>
        <Suspense fallback={<div className="kinora-bg min-h-screen" />}>
          <BookWindowRoom bookId={bookId} />
        </Suspense>
      </IntlProvider>
    );
  }
```

  (`Suspense`, `lazy`, and `IntlProvider` are already imported in `App.tsx`.)

- [ ] **Open via `openBook` in Electron.** In `apps/desktop/src/components/HomePage.tsx`, replace `handleOpen` (lines ~76–81):

```ts
  const handleOpen = (book: Book) => {
    // The shelf cover's rect was captured on pointer-down (capture phase).
    setOriginRect(shared.takeRect());
    setSelectedBook(book);
    setRoomOpen(true);
  };
```

  with a version that prefers the pop-out window when the Electron bridge is present, and keeps the in-app overlay for the browser renderer:

```ts
  const handleOpen = (book: Book) => {
    // In the desktop app, a book pops out into its own window in front of the
    // library (which stays open, dimmed). In the browser (no window.kinora) fall
    // back to the in-app shared-element overlay.
    const bridge = (window as unknown as { kinora?: { openBook?: (id: string) => Promise<unknown> } }).kinora;
    if (bridge?.openBook) {
      void bridge.openBook(book.id);
      return;
    }
    // The shelf cover's rect was captured on pointer-down (capture phase).
    setOriginRect(shared.takeRect());
    setSelectedBook(book);
    setRoomOpen(true);
  };
```

- [ ] **Add the dim-veil listener to the library window.** Still in `HomePage.tsx`, add a small overlay state + subscription. Insert after the existing `const [searchSeed, setSearchSeed] = useState("");` (line ~74):

```ts
  // When a book window is open + focused, the main library window dims (focus cue).
  const [dimmed, setDimmed] = useState(false);
  useEffect(() => {
    const k = (window as unknown as {
      kinora?: { subscribe?: never };
    }).kinora as undefined | { onWindowDim?: (cb: (p: { dim: boolean }) => void) => () => void };
    // The bridge exposes window-dim via a dedicated subscriber added in the preload.
    if (!k?.onWindowDim) return;
    return k.onWindowDim((p) => setDimmed(Boolean(p.dim)));
  }, []);
```

  Then render the veil inside the top-level `<div className="kinora-bg min-h-screen flex flex-col relative">` (line ~273), as its first child:

```tsx
      {dimmed && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-[60]"
          style={{ background: "rgba(6,5,4,0.42)", transition: "opacity 0.2s ease" }}
        />
      )}
```

- [ ] **Expose `onWindowDim` in the preload.** Because the dim event is a main→renderer broadcast on `kinora:window:dim` (Task 6), add a subscriber to the bridge in `apps/desktop/electron/preload.ts`, in the `// --- Windows ---` group (after `openExternal`, line ~109):

```ts
  /** Subscribe to the "another window is focused — dim" veil signal. */
  onWindowDim: (cb: (payload: EventChannels["kinora:window:dim"]["payload"]) => void) =>
    subscribe("kinora:window:dim", cb),
```

  (`subscribe` already guards against non-allowlisted channels via `EVENT_CHANNELS`, which now includes `kinora:window:dim`.)

- [ ] **Run the full desktop test set:** `pnpm --filter @kinora/desktop run test`
  - Expected: vitest, node, and electron suites all pass.

- [ ] **Render-driving check (browser path still works):** with `dev:web` running, re-run `node apps/desktop/scripts/verify-reading.mjs`
  - Expected: still `6/6 checks passed` — the browser has no `window.kinora`, so `handleOpen` uses the in-app overlay and the reading room mounts exactly as in Task 4.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

> ELECTRON MANUAL NOTE (not a CI gate; record findings in the task summary): `pnpm --filter @kinora/desktop run dev` launches Electron. Clicking a library book should (1) open a SEPARATE OS window showing only the reading room, (2) dim the library window, (3) undim it when the book window closes, (4) keep native traffic lights on macOS over the toolbar. This is verified by hand because OS window enumeration/screencapture is blocked from the agent; the renderer-level behaviour is covered by the Playwright checks above.

---

## Task 10 — Phase B.1: The notes panel + in-book search (extracted surfaces)

**Goal:** Build the two surfaces the new toolbar references, so the toolbar (Task 11) stays thin. `NotesPanel` folds in highlight mode + saved highlights (same `kinora.highlights.{bookId}` localStorage as the old shell). `InBookSearch` is a v1-minimal in-book text search over the rendered page text the room already has.

**Files:**
- Create `apps/desktop/src/reading/NotesPanel.tsx`
- Create `apps/desktop/src/reading/InBookSearch.tsx`

**Interfaces:**
- Produces `<NotesPanel bookId={string} pages={PageText[]} onClose={() => void} reduce={boolean} />`. Owns highlight-mode toggle + save-selection + the saved-highlight list, all persisted to `kinora.highlights.{bookId}`.
- Produces `<InBookSearch pages={PageText[]} onJump={(pageIndex: number, matchIndex: number) => void} onClose={() => void} />`. Best-effort: lists matching snippets; `onJump` is wired in a follow-up (v1 keeps it a no-op-friendly callback).
- Consumes `PageText` from `apps/desktop/src/reading/slots.ts` (the `{ n: number; text: string }` shape used by `useFilmSession`/`ScrollFilmEngine`).

> SCOPE FLAG (search): v1-minimal — `InBookSearch` searches the in-memory page text and renders matching snippets with their page number. Jump-to-match scroll integration is intentionally deferred (the spec says "best-effort"; the reading column is virtual-ish and word-indexed, so precise scroll-to-match is follow-up work). The component returns matches and a count; clicking a result calls `onJump`, which the shell currently maps to scrolling the page's first paragraph (approximate).

### Steps

- [ ] **Create `apps/desktop/src/reading/NotesPanel.tsx`:**

```tsx
import { useCallback, useEffect, useState } from "react";

// Notes / highlights surface. Folds the old top-bar "highlight mode" + saved
// highlights out of the toolbar. Same per-book localStorage key + shape as the
// previous ReadingRoomShell implementation, so existing saved highlights load.
const HL_KEY = (id: string) => `kinora.highlights.${id}`;

interface Highlight {
  text: string;
  at: number;
}

function readHighlights(id: string): Highlight[] {
  try {
    const raw = localStorage.getItem(HL_KEY(id));
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? (arr as Highlight[]) : [];
  } catch {
    return [];
  }
}
function writeHighlights(id: string, list: Highlight[]): void {
  try {
    localStorage.setItem(HL_KEY(id), JSON.stringify(list));
  } catch {
    /* storage blocked */
  }
}

export function NotesPanel({
  bookId,
  onClose,
  reduce,
}: {
  bookId: string;
  onClose: () => void;
  reduce: boolean;
}) {
  const [highlightMode, setHighlightMode] = useState(false);
  const [list, setList] = useState<Highlight[]>(() => readHighlights(bookId));

  useEffect(() => {
    setList(readHighlights(bookId));
    setHighlightMode(false);
  }, [bookId]);

  const saveSelection = useCallback(() => {
    const sel = window.getSelection?.();
    const text = sel?.toString().trim();
    if (!text) return;
    const next = [...readHighlights(bookId), { text, at: Date.now() }];
    writeHighlights(bookId, next);
    setList(next);
    sel?.removeAllRanges();
  }, [bookId]);

  const remove = useCallback(
    (at: number) => {
      const next = readHighlights(bookId).filter((h) => h.at !== at);
      writeHighlights(bookId, next);
      setList(next);
    },
    [bookId],
  );

  return (
    <div
      data-notes-panel
      role="dialog"
      aria-label="Notes and highlights"
      className="absolute left-0 top-12 z-50 w-[320px] rounded-2xl p-4"
      style={{
        background: "rgba(20, 17, 15, 0.93)",
        border: "0.5px solid rgba(246,240,231,0.16)",
        boxShadow: "0 34px 90px -22px rgba(6,5,4,0.85)",
        backdropFilter: "blur(36px) saturate(135%)",
        WebkitBackdropFilter: "blur(36px) saturate(135%)",
        maxHeight: "70vh",
        overflowY: "auto",
      }}
    >
      <div className="mb-3 flex items-center justify-between">
        <span className="text-[12px] font-semibold uppercase tracking-[0.12em] text-kinora-muted">
          Notes
        </span>
        <button
          onClick={() => setHighlightMode((v) => !v)}
          aria-pressed={highlightMode}
          className="rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors"
          style={{
            background: highlightMode ? "rgba(212,164,78,0.14)" : "rgba(255,255,255,0.04)",
            border: `1px solid ${highlightMode ? "rgba(212,164,78,0.25)" : "rgba(255,255,255,0.08)"}`,
            color: highlightMode ? "#e8c878" : "rgba(232,226,216,0.7)",
            transition: reduce ? "none" : undefined,
          }}
        >
          {highlightMode ? "Highlighting — select & Save" : "Highlight mode"}
        </button>
      </div>
      {highlightMode && (
        <button
          onClick={saveSelection}
          className="mb-3 w-full rounded-md px-2.5 py-1.5 text-[11px] font-semibold"
          style={{ background: "linear-gradient(180deg, #e8c878 0%, #d4a44e 100%)", color: "#0a0908" }}
        >
          Save current selection
        </button>
      )}
      {list.length === 0 ? (
        <p className="text-[12px] text-kinora-muted">No highlights yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {list
            .slice()
            .reverse()
            .map((h) => (
              <li key={h.at} className="group rounded-lg p-2 text-[12px]" style={{ background: "rgba(255,255,255,0.03)" }}>
                <p className="text-kinora-text/90">{h.text}</p>
                <button
                  onClick={() => remove(h.at)}
                  className="mt-1 text-[10px] text-kinora-muted opacity-0 transition-opacity group-hover:opacity-100"
                  aria-label="Remove highlight"
                >
                  Remove
                </button>
              </li>
            ))}
        </ul>
      )}
      <button onClick={onClose} className="sr-only">
        Close notes
      </button>
    </div>
  );
}
```

- [ ] **Create `apps/desktop/src/reading/InBookSearch.tsx`:**

```tsx
import { useMemo, useState } from "react";
import type { PageText } from "./slots";

interface Match {
  page: number;
  snippet: string;
}

function findMatches(pages: PageText[], query: string, max = 40): Match[] {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const out: Match[] = [];
  for (const p of pages) {
    const text = p.text;
    const lower = text.toLowerCase();
    let from = 0;
    while (out.length < max) {
      const idx = lower.indexOf(q, from);
      if (idx === -1) break;
      const start = Math.max(0, idx - 36);
      const end = Math.min(text.length, idx + q.length + 36);
      out.push({ page: p.n, snippet: (start > 0 ? "…" : "") + text.slice(start, end).trim() + (end < text.length ? "…" : "") });
      from = idx + q.length;
    }
    if (out.length >= max) break;
  }
  return out;
}

/** v1-minimal in-book text search over the rendered page text. Lists matching
 *  snippets with their page number; jumping is best-effort via onJump. */
export function InBookSearch({
  pages,
  onJump,
  onClose,
}: {
  pages: PageText[];
  onJump: (page: number) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const matches = useMemo(() => findMatches(pages, query), [pages, query]);

  return (
    <div
      data-search-panel
      role="dialog"
      aria-label="Search in book"
      className="absolute right-0 top-12 z-50 w-[340px] rounded-2xl p-4"
      style={{
        background: "rgba(20, 17, 15, 0.93)",
        border: "0.5px solid rgba(246,240,231,0.16)",
        boxShadow: "0 34px 90px -22px rgba(6,5,4,0.85)",
        backdropFilter: "blur(36px) saturate(135%)",
        WebkitBackdropFilter: "blur(36px) saturate(135%)",
        maxHeight: "70vh",
        overflowY: "auto",
      }}
    >
      <label className="sr-only" htmlFor="inbook-search">
        Search in book
      </label>
      <input
        id="inbook-search"
        type="search"
        autoFocus
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search in this book…"
        className="mb-3 w-full rounded-lg px-3 py-2 text-[13px]"
        style={{ background: "#1f1b16", color: "#e8e2d8", border: "1px solid rgba(255,255,255,0.18)" }}
      />
      {query.trim().length >= 2 && (
        <p className="mb-2 text-[11px] text-kinora-muted">
          {matches.length === 0 ? "No matches" : `${matches.length} match${matches.length === 1 ? "" : "es"}`}
        </p>
      )}
      <ul className="flex flex-col gap-1.5">
        {matches.map((m, i) => (
          <li key={`${m.page}-${i}`}>
            <button
              onClick={() => onJump(m.page)}
              className="w-full rounded-lg p-2 text-left text-[12px] transition-colors hover:bg-white/5"
              style={{ background: "rgba(255,255,255,0.02)" }}
            >
              <span className="text-[10px] uppercase tracking-wide text-kinora-muted">Page {m.page}</span>
              <span className="block text-kinora-text/90">{m.snippet}</span>
            </button>
          </li>
        ))}
      </ul>
      <button onClick={onClose} className="sr-only">
        Close search
      </button>
    </div>
  );
}

export { findMatches };
```

- [ ] **(Optional but recommended) add a tiny test for `findMatches`.** Create the test inline by appending to a new `apps/desktop/src/reading/InBookSearch.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { findMatches } from "./InBookSearch";

describe("findMatches", () => {
  const pages = [
    { n: 1, text: "The frog king sat by the well." },
    { n: 2, text: "A well-known tale about a well." },
  ];
  it("ignores queries shorter than 2 chars", () => {
    expect(findMatches(pages, "a")).toEqual([]);
  });
  it("finds all matches with page numbers", () => {
    const m = findMatches(pages, "well");
    expect(m.length).toBe(3); // p1 once, p2 twice
    expect(m[0].page).toBe(1);
  });
  it("trims and ellipsizes snippets", () => {
    const m = findMatches(pages, "frog");
    expect(m[0].snippet.length).toBeGreaterThan(0);
  });
});
```

- [ ] **Run the new tests:** `pnpm --filter @kinora/desktop exec vitest run src/reading/InBookSearch.test.tsx`
  - Expected: 3 `findMatches` tests pass.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 11 — Phase B.2: The Apple Books `BookToolbar` (+ live/buffer status dot)

> **AMENDMENT (2026-06-29, user request): REMOVE the AI Film toggle.** Do NOT render an AI-Film toggle button, and DROP the `generateVideo` and `onToggleGenerate` props from `BookToolbar`. The film is always-on (no user on/off control); real spend stays gated by the backend `KINORA_LIVE_VIDEO` + budget. KEEP the pure `bufferStatus(live, bufferAhead)` helper, but render the live/buffer state as a small **passive status dot** — a non-interactive `<span>` (`aria-label="AI film status"`, no `onClick`, no enable/disable text) at the left of the right-hand `[share · Aa · search]` pill; `off` → grey/hidden. In the tests: DELETE the "toggles AI film via the handler" test and remove `generateVideo`/`onToggleGenerate` from the shared test props; KEEP the `bufferStatus` tests and assert the passive dot reflects status. (Related: in Task 13, DELETE the `aiFilmToggle` e2e selector rather than realign it.) Everything below that describes the toggle as a button is superseded by this note.

**Goal:** Build the minimal floating toolbar that replaces the old bar: left pill `[contents · notes]`, centered title, right a **passive live/buffer status dot** → pill `[share · Aa · search]` → bookmark circle. The status dot is driven by `session.bufferAhead`/`session.live` (replacing the old "Buffered Ns ahead" pill) and is NOT interactive. Bookmark stays the same localStorage. Grouped pills with hairline separators.

**Files:**
- Create `apps/desktop/src/reading/BookToolbar.tsx`
- Create `apps/desktop/src/reading/BookToolbar.test.tsx`

**Interfaces:**
- Produces `<BookToolbar>` with props:
  ```ts
  interface BookToolbarProps {
    title: string;
    author: string;
    onClose: () => void;            // Back / close the room
    generateVideo: boolean;
    onToggleGenerate: (next: boolean) => void;
    live: boolean;                  // session.live
    bufferAhead: number | null;     // session.bufferAhead
    bookmarked: boolean;
    onToggleBookmark: () => void;
    onToggleContents: () => void;   // opens a contents/outline surface (stub button for v1)
    onToggleNotes: () => void;      // opens NotesPanel
    onToggleAa: () => void;         // opens AaPopover
    onToggleSearch: () => void;     // opens InBookSearch
    onShare: () => void;            // share stub
    notesOpen: boolean; aaOpen: boolean; searchOpen: boolean; // for aria-expanded
    reduce: boolean;
  }
  ```
- Consumes nothing external beyond React; pure presentational + handler-wiring. The status-dot color logic is exported as a pure helper `bufferStatus(live, bufferAhead)` for the test.

### Steps

- [ ] **Create `apps/desktop/src/reading/BookToolbar.tsx`:**

```tsx
import type { CSSProperties } from "react";

const HAIRLINE = "rgba(246,240,231,0.14)";
const PILL_BG = "rgba(20,17,15,0.7)";
const PILL_BORDER = "0.5px solid rgba(246,240,231,0.16)";

export type BufferStatus = "off" | "live-buffering" | "live-ahead";

/** The AI-Film status dot state, derived from the live session. `off` = grey
 *  (not generating), `live-buffering` = amber pulse (live but little buffer),
 *  `live-ahead` = green (comfortably buffered ahead). Replaces the old
 *  "Buffered Ns ahead" pill. */
export function bufferStatus(live: boolean, bufferAhead: number | null): BufferStatus {
  if (!live) return "off";
  return (bufferAhead ?? 0) >= 4 ? "live-ahead" : "live-buffering";
}

const DOT_COLOR: Record<BufferStatus, string> = {
  off: "rgba(255,255,255,0.28)",
  "live-buffering": "#e8c878",
  "live-ahead": "#5fce8e",
};

interface BookToolbarProps {
  title: string;
  author: string;
  onClose: () => void;
  generateVideo: boolean;
  onToggleGenerate: (next: boolean) => void;
  live: boolean;
  bufferAhead: number | null;
  bookmarked: boolean;
  onToggleBookmark: () => void;
  onToggleContents: () => void;
  onToggleNotes: () => void;
  onToggleAa: () => void;
  onToggleSearch: () => void;
  onShare: () => void;
  notesOpen: boolean;
  aaOpen: boolean;
  searchOpen: boolean;
  reduce: boolean;
}

const iconBtn: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  width: 30,
  height: 30,
  background: "transparent",
  border: "none",
  color: "rgba(232,226,216,0.8)",
  cursor: "pointer",
};

function Sep() {
  return <span aria-hidden style={{ width: 1, height: 18, background: HAIRLINE }} />;
}

export function BookToolbar(props: BookToolbarProps) {
  const status = bufferStatus(props.live, props.bufferAhead);
  return (
    <div
      data-book-toolbar
      className="flex flex-shrink-0 items-center gap-3 px-4 py-2.5"
      style={{ borderBottom: `1px solid ${HAIRLINE}`, background: "transparent" }}
      // On macOS hiddenInset the traffic lights sit at the far left; pad the left
      // group so the contents pill clears them.
    >
      {/* Left group: Back + [contents · notes] pill (clears traffic lights) */}
      <div className="flex items-center gap-2" style={{ paddingLeft: 64 }}>
        <button onClick={props.onClose} aria-label="Close reader" style={iconBtn} title="Back to library">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <div className="flex items-center gap-0.5 rounded-full px-1 py-0.5" style={{ background: PILL_BG, border: PILL_BORDER }}>
          <button onClick={props.onToggleContents} aria-label="Table of contents" style={iconBtn} title="Contents">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 6h16M4 12h16M4 18h10" />
            </svg>
          </button>
          <Sep />
          <button
            onClick={props.onToggleNotes}
            aria-label="Notes and highlights"
            aria-expanded={props.notesOpen}
            style={iconBtn}
            title="Notes"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M15 4l5 5-9 9H6v-5z" />
              <path d="M14 5l5 5" />
            </svg>
          </button>
        </div>
      </div>

      {/* Center: title (single line, truncate) */}
      <div className="flex min-w-0 flex-1 items-center justify-center">
        <span className="truncate font-serif text-[14px] font-semibold text-kinora-text" title={`${props.title} — ${props.author}`}>
          {props.title}
        </span>
      </div>

      {/* Right group: AI Film toggle (with status dot) → [share · Aa · search] → bookmark */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => props.onToggleGenerate(!props.generateVideo)}
          aria-pressed={props.generateVideo}
          aria-label={props.generateVideo ? "Disable AI film generation" : "Enable AI film generation"}
          title={
            status === "live-ahead"
              ? `AI film: ON — buffered ${Math.round(props.bufferAhead ?? 0)}s ahead`
              : status === "live-buffering"
                ? "AI film: ON — generating ahead…"
                : props.generateVideo
                  ? "AI film: ON"
                  : "AI film: OFF — click to enable"
          }
          className="flex items-center gap-2 rounded-full px-3 py-1.5 text-[11px] font-medium transition-colors"
          style={{
            background: props.generateVideo ? "rgba(212,164,78,0.12)" : PILL_BG,
            border: props.generateVideo ? "0.5px solid rgba(212,164,78,0.25)" : PILL_BORDER,
            color: props.generateVideo ? "#e8c878" : "rgba(232,226,216,0.75)",
          }}
        >
          <span>AI Film</span>
          <span
            data-buffer-dot
            data-status={status}
            aria-hidden
            className="inline-block h-2 w-2 rounded-full"
            style={{
              background: DOT_COLOR[status],
              boxShadow: status === "live-ahead" ? "0 0 6px rgba(95,206,142,0.7)" : undefined,
              animation: status === "live-buffering" && !props.reduce ? "kinoraDotPulse 1.4s ease-in-out infinite" : undefined,
            }}
          />
        </button>

        <div className="flex items-center gap-0.5 rounded-full px-1 py-0.5" style={{ background: PILL_BG, border: PILL_BORDER }}>
          <button onClick={props.onShare} aria-label="Share" style={iconBtn} title="Share">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7" />
              <path d="M12 3v13M8 7l4-4 4 4" />
            </svg>
          </button>
          <Sep />
          <button
            onClick={props.onToggleAa}
            aria-label="Reading appearance"
            aria-expanded={props.aaOpen}
            style={{ ...iconBtn, color: props.aaOpen ? "#e8c878" : iconBtn.color }}
            title="Themes & text"
          >
            <span style={{ fontWeight: 700, fontSize: 14, lineHeight: 1 }}>A</span>
            <span style={{ fontWeight: 700, fontSize: 10, lineHeight: 1, marginLeft: 1 }}>a</span>
          </button>
          <Sep />
          <button
            onClick={props.onToggleSearch}
            aria-label="Search in book"
            aria-expanded={props.searchOpen}
            style={iconBtn}
            title="Search"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="7" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
          </button>
        </div>

        <button
          onClick={props.onToggleBookmark}
          aria-pressed={props.bookmarked}
          aria-label={props.bookmarked ? "Remove bookmark" : "Bookmark this book"}
          title={props.bookmarked ? "Bookmarked" : "Add bookmark"}
          className="flex items-center justify-center rounded-full"
          style={{
            width: 30,
            height: 30,
            background: props.bookmarked ? "rgba(212,164,78,0.12)" : PILL_BG,
            border: props.bookmarked ? "0.5px solid rgba(212,164,78,0.25)" : PILL_BORDER,
            color: props.bookmarked ? "#e8c878" : "rgba(232,226,216,0.75)",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill={props.bookmarked ? "currentColor" : "none"} stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Add the pulse keyframe.** The status dot's `kinoraDotPulse` animation needs a keyframe. Append it to the reading-room partial `apps/desktop/src/styles/reading.css` (the reading-room CSS owner; it is `@import`ed by `src/styles/index.css`, which `src/main.tsx` loads — so the keyframe is globally available):

```css
@keyframes kinoraDotPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
```

- [ ] **Create `apps/desktop/src/reading/BookToolbar.test.tsx`:**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BookToolbar, bufferStatus } from "./BookToolbar";

function setup(over: Partial<Parameters<typeof BookToolbar>[0]> = {}) {
  const props = {
    title: "The Frog-King",
    author: "Brothers Grimm",
    onClose: vi.fn(),
    generateVideo: false,
    onToggleGenerate: vi.fn(),
    live: false,
    bufferAhead: null,
    bookmarked: false,
    onToggleBookmark: vi.fn(),
    onToggleContents: vi.fn(),
    onToggleNotes: vi.fn(),
    onToggleAa: vi.fn(),
    onToggleSearch: vi.fn(),
    onShare: vi.fn(),
    notesOpen: false,
    aaOpen: false,
    searchOpen: false,
    reduce: true,
    ...over,
  };
  render(<BookToolbar {...props} />);
  return props;
}

describe("bufferStatus", () => {
  it("is off when not live", () => {
    expect(bufferStatus(false, 99)).toBe("off");
  });
  it("is live-buffering with little buffer", () => {
    expect(bufferStatus(true, 1)).toBe("live-buffering");
    expect(bufferStatus(true, null)).toBe("live-buffering");
  });
  it("is live-ahead when comfortably buffered", () => {
    expect(bufferStatus(true, 10)).toBe("live-ahead");
  });
});

describe("BookToolbar", () => {
  it("shows the centered title", () => {
    setup();
    expect(screen.getByText("The Frog-King")).toBeInTheDocument();
  });
  it("toggles AI film via the handler", () => {
    const p = setup({ generateVideo: false });
    fireEvent.click(screen.getByRole("button", { name: /enable ai film/i }));
    expect(p.onToggleGenerate).toHaveBeenCalledWith(true);
  });
  it("renders the status dot reflecting the live buffer", () => {
    setup({ live: true, bufferAhead: 12 });
    const dot = document.querySelector("[data-buffer-dot]");
    expect(dot?.getAttribute("data-status")).toBe("live-ahead");
  });
  it("fires notes / Aa / search / bookmark / share handlers", () => {
    const p = setup();
    fireEvent.click(screen.getByRole("button", { name: /notes and highlights/i }));
    fireEvent.click(screen.getByRole("button", { name: /reading appearance/i }));
    fireEvent.click(screen.getByRole("button", { name: /search in book/i }));
    fireEvent.click(screen.getByRole("button", { name: /bookmark this book/i }));
    fireEvent.click(screen.getByRole("button", { name: /share/i }));
    expect(p.onToggleNotes).toHaveBeenCalled();
    expect(p.onToggleAa).toHaveBeenCalled();
    expect(p.onToggleSearch).toHaveBeenCalled();
    expect(p.onToggleBookmark).toHaveBeenCalled();
    expect(p.onShare).toHaveBeenCalled();
  });
});
```

- [ ] **Run the toolbar tests:** `pnpm --filter @kinora/desktop exec vitest run src/reading/BookToolbar.test.tsx`
  - Expected: all `bufferStatus` + `BookToolbar` tests pass.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 12 — Phase D + B.3: `AaPopover` + wire the toolbar into `ReadingRoomShell`

> **AMENDMENT (2026-06-29): no AI Film toggle.** Do NOT pass `generateVideo`/`onToggleGenerate` to `<BookToolbar>` (those props were removed in Task 11). The reading room always generates the film — treat `generateVideo` as always true wherever the session needs it, and you may drop the `kinora.reading.generateVideo` persistence and the toggle handler entirely. Only `live`/`bufferAhead` flow to the toolbar (for the passive status dot).

**Goal:** Replace the entire old top bar in `ReadingRoomShell` with `<BookToolbar>` plus the three popover surfaces (`AaPopover`, `NotesPanel`, `InBookSearch`), wiring the existing bookmark handler and `session` live/buffer state (no generate toggle). `AaPopover` (Phase D) rehouses the existing `ReadingControls` in an Apple-Books-style panel anchored to the `Aa` button. Keep `useRoomChrome`, the progress rail, and the warm-up overlay. Add a `share` stub and a `contents` stub.

**Files:**
- Create `apps/desktop/src/reading/AaPopover.tsx`
- Modify `apps/desktop/src/reading/ReadingRoomShell.tsx` (remove the old top bar lines ~206–405; add toolbar + popovers; relocate state)

**Interfaces:**
- Produces `<AaPopover prefs={ReadingPrefs} onChange={(p: Partial<ReadingPrefs>) => void} reduce={boolean} onClose={() => void} />` — wraps `ReadingControls`.
- Consumes `BookToolbar`, `NotesPanel`, `InBookSearch` (Tasks 10–11); `ReadingControls` + `useReadingPrefs` (existing).

### Steps

- [ ] **Create `apps/desktop/src/reading/AaPopover.tsx`:**

```tsx
import type { ReadingPrefs } from "../lib/readingPrefs";
import { ReadingControls } from "./ReadingControls";

/** The Apple-Books "Aa" appearance popover. Pure rehousing of the existing
 *  ReadingControls (themes + font + size/leading/measure/spacing/brightness +
 *  TTS) — NO new theme engine. Anchored under the toolbar's Aa button. */
export function AaPopover({
  prefs,
  onChange,
  onClose,
}: {
  prefs: ReadingPrefs;
  onChange: (partial: Partial<ReadingPrefs>) => void;
  reduce: boolean;
  onClose: () => void;
}) {
  return (
    <div
      data-aa-popover
      role="dialog"
      aria-label="Reading appearance"
      className="absolute right-0 top-12 z-50 rounded-2xl p-4"
      style={{
        background: "rgba(20, 17, 15, 0.93)",
        border: "0.5px solid rgba(246,240,231,0.16)",
        boxShadow: "inset 0 1px 0 rgba(246,240,231,0.1), 0 34px 90px -22px rgba(6,5,4,0.85)",
        maxHeight: "74vh",
        overflowY: "auto",
        backdropFilter: "blur(36px) saturate(135%)",
        WebkitBackdropFilter: "blur(36px) saturate(135%)",
      }}
    >
      <ReadingControls prefs={prefs} onChange={onChange} />
      <button onClick={onClose} className="sr-only">
        Close appearance
      </button>
    </div>
  );
}
```

- [ ] **Rewrite the shell's chrome.** In `apps/desktop/src/reading/ReadingRoomShell.tsx`:

  1. **Update imports** (top of file). Add:

```ts
import { BookToolbar } from "./BookToolbar";
import { AaPopover } from "./AaPopover";
import { NotesPanel } from "./NotesPanel";
import { InBookSearch } from "./InBookSearch";
```

   The existing `import { ScrollFilmEngine, ReadingControls } from "./producers";` can drop `ReadingControls` (now used via `AaPopover`); change it to `import { ScrollFilmEngine } from "./producers";`.

  2. **Replace the popover/panel state.** The shell currently has `controlsOpen`, `highlightMode`, `highlightCount` and the `saveSelection` callback (lines ~118–168, 156–168). Replace the `useState` block (lines ~117–121):

```ts
  const [progress, setProgress] = useState(0);
  const [controlsOpen, setControlsOpen] = useState(false);
  const [bookmarked, setBookmarked] = useState(() => readBookmark(book.id).on);
  const [highlightMode, setHighlightMode] = useState(false);
  const [highlightCount, setHighlightCount] = useState(() => readHighlightCount(book.id));
```

   with a single panel selector + bookmark (highlights now live entirely in `NotesPanel`):

```ts
  const [progress, setProgress] = useState(0);
  const [panel, setPanel] = useState<null | "notes" | "aa" | "search">(null);
  const [bookmarked, setBookmarked] = useState(() => readBookmark(book.id).on);
```

  3. **Remove the now-dead `saveSelection`, `highlightCount`, and `readHighlightCount` usages.** Delete the `saveSelection` callback (lines ~156–168) and the per-book sync of `highlightCount`/`highlightMode` inside the book-switch effect (lines ~124–138) — keep the bookmark restore. The book-switch effect becomes:

```ts
  // Sync per-book bookmark when switching books and restore scroll position.
  useEffect(() => {
    const bm = readBookmark(book.id);
    setBookmarked(bm.on);
    setPanel(null);
    if (bm.on && bm.scrollFraction > 0) {
      const t = window.setTimeout(() => {
        const scrollEl = rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]");
        if (scrollEl) {
          scrollEl.scrollTop = bm.scrollFraction * (scrollEl.scrollHeight - scrollEl.clientHeight);
        }
      }, 120);
      return () => window.clearTimeout(t);
    }
  }, [book.id]);
```

   You may also delete the `readHighlightCount` helper (lines ~86–93) since `NotesPanel` owns its own read; leave `BM_KEY`/`HL_KEY`/`readBookmark`/`writeBookmark` (bookmark still used; `HL_KEY` now only in `NotesPanel`, so `HL_KEY` here can be removed too).

  4. **Replace the outside-click closer** (lines ~172–181) to close any open panel when clicking outside the toolbar + panels:

```ts
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (
        !target.closest("[data-book-toolbar]") &&
        !target.closest("[data-aa-popover]") &&
        !target.closest("[data-notes-panel]") &&
        !target.closest("[data-search-panel]")
      ) {
        setPanel(null);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);
```

  5. **Replace the entire old top bar** — delete the `{/* Top bar */}` `<div …>…</div>` block (lines ~209–405, i.e. everything from the comment through its closing `</div>` right before `{/* Content area … */}`). In its place render the toolbar + a relative wrapper holding the popovers:

```tsx
      {/* Apple-Books floating toolbar (replaces the old top bar). The popovers
          anchor to it; nothing here ever overlaps the film/text columns below. */}
      <div className="relative flex-shrink-0">
        <BookToolbar
          title={book.title}
          author={book.author}
          onClose={onClose}
          generateVideo={generateVideo}
          onToggleGenerate={onToggleGenerate}
          live={session.live}
          bufferAhead={session.bufferAhead}
          bookmarked={bookmarked}
          onToggleBookmark={toggleBookmark}
          onToggleContents={() => { /* contents/outline — v1 stub */ }}
          onToggleNotes={() => setPanel((p) => (p === "notes" ? null : "notes"))}
          onToggleAa={() => setPanel((p) => (p === "aa" ? null : "aa"))}
          onToggleSearch={() => setPanel((p) => (p === "search" ? null : "search"))}
          onShare={() => {
            // Share — minimal stub (no backend share API yet). Copy a deep link.
            try {
              void navigator.clipboard?.writeText(`kinora://book/${book.id}`);
            } catch { /* clipboard blocked */ }
          }}
          notesOpen={panel === "notes"}
          aaOpen={panel === "aa"}
          searchOpen={panel === "search"}
          reduce={reduce}
        />
        {panel === "notes" && (
          <NotesPanel bookId={book.id} onClose={() => setPanel(null)} reduce={reduce} />
        )}
        {panel === "aa" && (
          <AaPopover prefs={prefs} onChange={update} reduce={reduce} onClose={() => setPanel(null)} />
        )}
        {panel === "search" && (
          <InBookSearch
            pages={session.pages}
            onClose={() => setPanel(null)}
            onJump={(pageNum) => {
              // Approximate jump: scroll to the page's first paragraph by index.
              const scrollEl = rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]");
              const target = rootRef.current?.querySelector<HTMLElement>(`[data-para="${Math.max(0, pageNum - 1)}"]`);
              if (scrollEl && target) {
                scrollEl.scrollTo({ top: target.offsetTop - scrollEl.clientHeight * 0.3, behavior: reduce ? "auto" : "smooth" });
              }
              setPanel(null);
            }}
          />
        )}
      </div>
```

  6. **Delete the old "Buffered Ns ahead" pill** — it was inside the removed top bar (`session.live && (…pill…)`, lines ~388–404). It is replaced by the toolbar's status dot. Also delete the now-unused `pill`/`showWarmUp` `pill` local (lines ~201–204) IF `pill` is unused elsewhere; `showWarmUp` is still used by the warm-up overlay so keep it. Remove only the `pill` const.

  7. **Keep** `useRoomChrome(onClose, rootRef)`, the focus-into-reader effect, `onProgress`, `totalWords`, the `<ScrollFilmEngine .../>` mount, the progress/buffer rail block (lines ~423–454), and the warm-up overlay (lines ~456–459) unchanged.

- [ ] **Run the shell-adjacent tests + a broad reading run:** `pnpm --filter @kinora/desktop exec vitest run src/reading`
  - Expected: all included reading vitest tests pass, including the new `BookToolbar.test.tsx`, `focusModel.test.ts`, `InBookSearch.test.tsx`, and the existing `ReadingControls.test.tsx`.

- [ ] **Render-driving check (toolbar + popover):** with `dev:web` running, extend/append to `apps/desktop/scripts/verify-reading.mjs` these assertions before `await browser.close();` (the book is already open):

```js
// Phase B/D: the Apple-Books toolbar replaced the old bar; Aa opens the popover.
check("floating book toolbar is present", (await page.locator('[data-book-toolbar]').count()) > 0);
check("old 'Buffered ahead' pill is gone", (await page.getByText(/buffered .* ahead/i).count()) === 0);
await page.getByRole("button", { name: /reading appearance/i }).click();
check("Aa opens the appearance popover with theme controls", await page.getByRole("dialog", { name: /reading appearance/i }).isVisible());
check("appearance popover hosts the existing reading controls", await page.getByRole("group", { name: /reading settings/i }).isVisible());
await page.keyboard.press("Escape").catch(() => {});
```

  Update the final tally comment accordingly. Re-run: `node apps/desktop/scripts/verify-reading.mjs`
  - Expected: all checks PASS (now ~11/11), exit 0. Crucially `PASS  floating book toolbar is present`, `PASS  old 'Buffered ahead' pill is gone`, `PASS  Aa opens the appearance popover…`.

- [ ] **Verification gate:** `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test`
  - Expected: typecheck 0; all suites pass. Stage, do not commit.

---

## Task 13 — Cross-cutting: e2e selector + page-object alignment, final sweep

**Goal:** The existing e2e suite (`apps/desktop/e2e/`) references the OLD top bar via the `ReadingRoom` page object (`backButton` = "close reader", `settingsButton` = "reading settings", `aiFilmToggle` = `button[aria-label*="AI film" i]`, `bookmarkToggle`, `highlightToggle`). The redesign renamed/relocated several of these. Update the page object + selectors so the e2e specs still drive the room, and confirm the whole desktop check is green. This keeps `pnpm --filter @kinora/desktop run test` honest and the e2e suite runnable.

**Files:**
- Modify `apps/desktop/e2e/pageobjects/ReadingRoom.ts` (selectors: `settingsButton`, `bookmarkToggle`, `highlightToggle`, `backButton`)
- Modify `apps/desktop/e2e/support/selectors.ts` if any `TEXT.*`/`HOOK.*` entries reference removed labels (e.g. `readingSettings`, `bookmark`, `highlight`)
- Modify `apps/desktop/e2e/specs/reading-room.spec.ts` only if a spec asserts a now-removed control (e.g. "opens the reading-settings popover", "highlight" toggle)

**Interfaces:**
- No product interface changes; test-harness alignment only.

> NOTE: This task is about not breaking the e2e harness; it does NOT run e2e in the CI gate (e2e needs the dev server). Keep the unit gate (`typecheck && test`) green and make the e2e selectors *consistent* with the new DOM so a later `pnpm --filter @kinora/desktop run e2e:smoke` works.

### Steps

- [ ] **Inspect the current references:** `grep -rnE "readingSettings|settingsButton|bookmarkToggle|highlightToggle|aiFilmToggle|reading-settings|highlight" apps/desktop/e2e | sort`
  - Note every spec/page-object/selectors line that names a control the redesign moved.

- [ ] **Update the `ReadingRoom` page object.** In `apps/desktop/e2e/pageobjects/ReadingRoom.ts`:
  - `settingsButton` (line ~30): the gear became the `Aa` button → change to `page.getByRole("button", { name: /reading appearance/i })`.
  - `bookmarkToggle` (line ~32): unchanged label aria ("Bookmark this book" / "Remove bookmark") — keep, but it is now a circle in the right group; the role+name selector still matches.
  - `highlightToggle` (line ~33): the standalone highlight toggle is gone (folded into Notes). Replace with a `notesButton = page.getByRole("button", { name: /notes and highlights/i })`, and update any method that opened highlight mode to open Notes then toggle "Highlight mode" inside the panel.
  - `aiFilmToggle` (line ~31): DELETE this selector and any spec step that uses it — the AI-Film toggle was removed (film is always-on). The only "AI film" element now is a passive status dot (`aria-label="AI film status"`), not a button; do not assert clicks on it.
  - `backButton` (line ~28): `TEXT.closeReader` → the toolbar's Back button has `aria-label="Close reader"`; keep `TEXT.closeReader` if it is `/close reader/i`, else update.

- [ ] **Update `apps/desktop/e2e/support/selectors.ts`** if it defines `readingSettings`, `highlight`, or `bookmark` `TEXT` entries that no longer match: set `readingSettings: /reading appearance/i` (the Aa button), keep `bookmark: /bookmark/i`, and either drop `highlight` or repoint it to `/notes and highlights/i`.

- [ ] **Fix the two affected specs** in `apps/desktop/e2e/specs/reading-room.spec.ts`:
  - "opens the reading-settings popover" — assert the appearance popover by role/name `getByRole("dialog", { name: /reading appearance/i })` instead of the old gear popover.
  - Any "highlight mode" spec — drive it through the Notes panel: click Notes → click "Highlight mode" → assert it toggled. If that is heavier than warranted for this pass, mark the single highlight spec `test.fixme(...)` with a one-line reason ("highlight folded into Notes panel; rewrite pending") so the suite stays green and the gap is explicit — but PREFER the real rewrite.

- [ ] **Typecheck the e2e project** (separate tsconfig): `pnpm --filter @kinora/desktop run e2e:typecheck`
  - Expected: exits 0 (selectors/page-object compile against `@playwright/test`).

- [ ] **Full desktop check (the canonical gate from CLAUDE.md):**
  `pnpm --filter @kinora/desktop run typecheck && pnpm --filter @kinora/desktop run test && pnpm --filter @kinora/desktop run build`
  - Expected: typecheck 0; vitest + node + electron suites pass; `electron-vite`/`vite build` completes with no TS errors. Stage everything (`git add -A`), do NOT commit.

- [ ] **(Recommended) e2e smoke against the dev server.** With `pnpm --filter @kinora/desktop run dev:web` running, run `pnpm --filter @kinora/desktop run e2e:smoke`
  - Expected: the `@smoke`-tagged specs pass against the redesigned reading room (the suite mocks the backend; no Docker needed). Record any failures and whether they are pre-existing.

---

## Open questions / risks encountered

1. **`ReadingRoom` import path mismatch.** `HomePage.tsx` imports `./ReadingRoom` from `src/components/` but the file is `src/reading/ReadingRoom.tsx`. Either a `src/components/ReadingRoom.tsx` re-export shim exists, or the bundler resolves it some other way. Task 8 starts by confirming this; `BookWindowRoom` imports `./ReadingRoom` (sibling in `src/reading/`).
2. **Library dimming on non-mac & in the browser.** The dim veil is implemented as a renderer overlay driven by a new `kinora:window:dim` event — cross-platform and robust, but only fires under Electron. In the browser renderer there is no second window, so no dimming is needed. Verified renderer-side via the overlay state; the actual two-window dim/undim is a manual Electron check (screencapture blocked).
3. **`titleBarStyle: 'hiddenInset'` is macOS-only.** Applied conditionally; Win/Linux book windows keep `frame: true` + acrylic/background. The toolbar reserves `paddingLeft: 64` for the mac traffic lights — on Win/Linux that left gap is cosmetic (acceptable for v1; could be made conditional on `window.kinora.platform` in a follow-up).
4. **In-book search jump precision.** Page text is word-indexed and the column is continuous; `onJump` does an approximate scroll to the `data-para` whose index ≈ page number. This is intentionally v1-minimal per the spec ("best-effort"). Flagged in Task 10.
5. **Share is a stub.** No backend share API exists; `onShare` copies a `kinora://book/:id` deep link to the clipboard. Kept lean and flagged.
6. **e2e harness coupling.** Several e2e specs/page-objects name the old controls (gear "reading settings", standalone "highlight" toggle, "Buffered ahead" pill). Task 13 realigns them; the unit gate (`typecheck && test`) does not run e2e, so a missed selector won't fail the gate — hence the explicit `e2e:typecheck` + optional `e2e:smoke` steps.
7. **`paged` reading mode.** `readingPrefs` still exposes a "Paged" segmented option (rendered by `ReadingControls`, now inside the `Aa` popover) that is unimplemented. Out of scope per the constraint to keep continuous scroll; it remains a visible-but-inert control. If that is undesirable, hiding the "Paged" option is a one-line follow-up in `ReadingControls.tsx` (not done here to avoid touching shared a11y UI).
