# Kinora overnight build — published contracts

Each agent appends the interfaces it owns and that others consume. Stub against
these if the providing branch is not merged yet. Keep signatures stable once
marked **STABLE**.

---

## Agent 06 — Accessibility (`apps/desktop/src/a11y/`)

> Status: **WS1–WS3 LANDED** on `agent/06-a11y` (foundation + ReadingControls +
> read-aloud, 90 tests, typecheck + build green). Import from `@/a11y/*`
> (alias `@` → `apps/desktop/src`). Signatures below are the contract.

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

### Read-aloud engine + view (Web Speech API) — **LANDED** `cc117b7`

```ts
// apps/desktop/src/a11y/tts.ts
function tokenizeWords(text: string): TtsToken[];                 // pure, char offsets
function findTokenAtChar(tokens: TtsToken[], charIndex: number): TtsToken | null;
function useTts(opts: {
  getText: () => string;
  rate?: number; voiceURI?: string | null;
  onError?: (e: string) => void;
  onActiveWordChange?: (token: TtsToken | null) => void;
}): {
  supported: boolean;
  state: "idle" | "playing" | "paused";
  activeWordIndex: number;           // -1 when none
  tokens: TtsToken[];
  play(): void; pause(): void; resume(): void; toggle(): void; stop(): void;
};
interface TtsToken { text: string; start: number; end: number; index: number }
```

```tsx
// apps/desktop/src/a11y/ReadAloudView.tsx — mount inside the text-pane (Agent 10)
<ReadAloudView text={pageText} rate? voiceURI? showControls? />
// renders words + highlights the spoken one (aria-current) in lockstep.
```

### Checklist every agent must satisfy

See `apps/desktop/src/a11y/a11y-checklist.md` — labels, roles, focus order,
contrast, keyboard, reduced-motion. Linked from each `coordination/requests/agent-XX.md`.

---
