# Kinora overnight build — published contracts

Each agent appends the interfaces it owns and that others consume. Stub against
these if the providing branch is not merged yet. Keep signatures stable once
marked **STABLE**.

---

## Agent 06 — Accessibility (`apps/desktop/src/a11y/`)

> Status: **building** (signatures frozen; implementations landing on `agent/06-a11y`).
> Import from `@/a11y/*` (alias `@` → `apps/desktop/src`). Until merged, other
> agents may stub these — the signatures below are the contract.

### Reduced motion — the single source of truth

```ts
// apps/desktop/src/a11y/useReducedMotionPref.ts
function useReducedMotionPref(): boolean;       // OS `prefers-reduced-motion` OR in-app override
function setReducedMotionOverride(v: boolean | null): void;  // null = follow OS
function getReducedMotionSnapshot(): boolean;   // non-hook read (for imperative code)
```

**Agent 4 (motion) and every animating component must consume `useReducedMotionPref()`**
instead of framer-motion's `useReducedMotion()` directly, so the in-app toggle works.

### Reading preferences (moved here from `lib/readingPrefs.ts`; shim left behind)

```ts
// apps/desktop/src/a11y/readingPrefs.ts  (re-exported from lib/readingPrefs.ts)
function useReadingPrefs(): {
  prefs: ReadingPrefs;
  update: (p: Partial<ReadingPrefs>) => void;
  effectiveTheme: ReadingTheme;     // honours autoNight
};
interface ReadingPrefs {
  theme: ReadingTheme;              // 'dark' | 'night' | 'sepia' | 'paper'
  autoNight: boolean;
  fontFamily: ReadingFontFamily;    // 'sans' | 'serif' | 'dyslexic'
  fontScale: number;                // 0.8–1.6 of 15px base
  leading: number;                  // line-height 1.3–2.4
  measure: number;                  // ch, 44–88
  spacing: ReadingSpacing;          // 'normal' | 'relaxed' | 'loose'
  brightness: number;               // 0.5–1.0 page dim
  readingMode: 'scroll' | 'paged';
  ttsRate: number;                  // 0.5–2.0
  ttsVoiceURI: string | null;       // null = system default
}
```

### Announcer / focus / keyboard / hidden-text primitives

```ts
// apps/desktop/src/a11y/announce.ts
function announce(message: string, politeness?: 'polite' | 'assertive'): void;

// apps/desktop/src/a11y/focus.ts
function trapFocus(container: HTMLElement): () => void;  // returns release()
function restoreFocus(previouslyFocused: HTMLElement | null): void;
function getFocusable(container: HTMLElement): HTMLElement[];

// apps/desktop/src/a11y/keyboard.ts
function registerShortcut(
  combo: string,                     // e.g. 'mod+,'  'shift+?'  'r'
  handler: (e: KeyboardEvent) => void,
  opts?: { scope?: string; description?: string; whenInputFocused?: boolean },
): () => void;                       // returns unregister()
```

```tsx
// apps/desktop/src/a11y/VisuallyHidden.tsx
<VisuallyHidden as="span">screen-reader-only text</VisuallyHidden>
```

### Reading controls panel (Agent 10 mounts via the reading-room slot)

```tsx
// apps/desktop/src/reading/ReadingControls.tsx
<ReadingControls
  prefs={ReadingPrefs}
  onChange={(p: Partial<ReadingPrefs>) => void}
  voices?={SpeechSynthesisVoice[]}   // optional; component will enumerate if omitted
/>
```

### Read-aloud engine (Web Speech API)

```ts
// apps/desktop/src/a11y/tts.ts
function useTts(opts: {
  getText: () => TtsToken[];         // tokens with char offsets for word-sync
  rate?: number; voiceURI?: string | null;
  onWord?: (token: TtsToken | null) => void;
}): {
  state: 'idle' | 'playing' | 'paused';
  play(): void; pause(): void; resume(): void; stop(): void;
  next(): void; prev(): void;        // skip sentence/paragraph
  activeWordIndex: number;           // -1 when none
  supported: boolean;
};
interface TtsToken { text: string; start: number; end: number; wordIndex?: number }
```

### Checklist every agent must satisfy

See `apps/desktop/src/a11y/a11y-checklist.md` — labels, roles, focus order,
contrast, keyboard, reduced-motion. Linked from each `coordination/requests/agent-XX.md`.

---
