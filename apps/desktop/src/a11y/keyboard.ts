// Keyboard combo parsing + matching — the pure core of the global shortcut layer.
// Combo grammar: `+`-separated tokens, case-insensitive, last token is the key.
//   modifiers: mod (= ⌘ on macOS, Ctrl elsewhere), cmd/meta, ctrl, alt/option, shift
//   keys: any single char (',', '?', 'k') or a named key ('esc', 'space', 'arrowup')

export interface ParsedCombo {
  key: string; // compared against event.key.toLowerCase()
  mod: boolean; // platform-agnostic command modifier
  ctrl: boolean;
  meta: boolean;
  alt: boolean;
  shift: boolean;
}

/** Structural subset of KeyboardEvent — so matching is testable without a DOM. */
export interface KeyState {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}

const KEY_ALIASES: Record<string, string> = {
  esc: "escape",
  space: " ",
  spacebar: " ",
  plus: "+",
};

export function parseCombo(combo: string): ParsedCombo {
  const tokens = combo
    .split("+")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
  const result: ParsedCombo = {
    key: "",
    mod: false,
    ctrl: false,
    meta: false,
    alt: false,
    shift: false,
  };
  tokens.forEach((tok, i) => {
    const isLast = i === tokens.length - 1;
    switch (tok) {
      case "mod":
        result.mod = true;
        return;
      case "cmd":
      case "command":
      case "meta":
        result.meta = true;
        return;
      case "ctrl":
      case "control":
        result.ctrl = true;
        return;
      case "alt":
      case "option":
      case "opt":
        result.alt = true;
        return;
      case "shift":
        result.shift = true;
        return;
      default:
        // a non-modifier token is the key (last one wins)
        if (isLast || !result.key) result.key = KEY_ALIASES[tok] ?? tok;
    }
  });
  return result;
}

export function eventMatchesCombo(e: KeyState, p: ParsedCombo, isMac: boolean): boolean {
  if ((e.key || "").toLowerCase() !== p.key) return false;
  const wantMeta = p.meta || (p.mod && isMac);
  const wantCtrl = p.ctrl || (p.mod && !isMac);
  if (Boolean(e.metaKey) !== wantMeta) return false;
  if (Boolean(e.ctrlKey) !== wantCtrl) return false;
  if (Boolean(e.altKey) !== p.alt) return false;
  // Shift is only enforced when the combo explicitly asks for it, so symbol
  // keys (which require shift on many layouts) still match a bare combo.
  if (p.shift && !e.shiftKey) return false;
  return true;
}

// ---- Global shortcut registry -------------------------------------------------

export interface ShortcutOpts {
  scope?: string;
  description?: string;
  /** Fire even when focus is in a text field (e.g. Escape). Default false. */
  whenInputFocused?: boolean;
  /** Call preventDefault() when the combo matches. Default false. */
  preventDefault?: boolean;
  /** Functional but intentionally omitted from the `?` cheat-sheet / any UI
   *  surface (power-user navigation we don't advertise). Default false. */
  hidden?: boolean;
}

export interface RegisteredShortcut {
  combo: string;
  description?: string;
  scope?: string;
}

interface Entry extends RegisteredShortcut {
  parsed: ParsedCombo;
  handler: (e: KeyboardEvent) => void;
  whenInputFocused: boolean;
  preventDefault: boolean;
  hidden: boolean;
}

const entries: Entry[] = [];
let listening = false;

export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const probe = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  return /Mac|iPhone|iPad|iPod/i.test(probe);
}

// Only true text-entry contexts suppress single-key shortcuts. Non-text inputs
// (range/checkbox/radio/button/etc.) and selects must NOT swallow them, so e.g.
// "?" opens the cheat-sheet while a slider has focus.
const TEXT_ENTRY_INPUT_TYPES = new Set([
  "text",
  "search",
  "email",
  "url",
  "tel",
  "password",
  "number",
  "date",
  "datetime-local",
  "month",
  "time",
  "week",
]);

function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "textarea") return true;
  if (el.isContentEditable) return true;
  if (tag === "input") {
    const type = ((el as HTMLInputElement).type || "text").toLowerCase();
    return TEXT_ENTRY_INPUT_TYPES.has(type);
  }
  return false;
}

function onKeyDown(e: KeyboardEvent): void {
  const mac = isMacPlatform();
  const typing = isTypingTarget(e.target);
  // Snapshot so a handler that (un)registers shortcuts can't mutate mid-loop.
  for (const entry of entries.slice()) {
    if (typing && !entry.whenInputFocused) continue;
    if (eventMatchesCombo(e, entry.parsed, mac)) {
      if (entry.preventDefault) e.preventDefault();
      entry.handler(e);
    }
  }
}

function ensureListening(): void {
  if (listening || typeof document === "undefined") return;
  document.addEventListener("keydown", onKeyDown);
  listening = true;
}

function stopListening(): void {
  if (!listening || typeof document === "undefined") return;
  document.removeEventListener("keydown", onKeyDown);
  listening = false;
}

/** Register a global keyboard shortcut. Returns an unregister function. */
export function registerShortcut(
  combo: string,
  handler: (e: KeyboardEvent) => void,
  opts: ShortcutOpts = {},
): () => void {
  const entry: Entry = {
    combo,
    parsed: parseCombo(combo),
    handler,
    scope: opts.scope,
    description: opts.description,
    whenInputFocused: opts.whenInputFocused ?? false,
    preventDefault: opts.preventDefault ?? false,
    hidden: opts.hidden ?? false,
  };
  entries.push(entry);
  ensureListening();
  return () => {
    const i = entries.indexOf(entry);
    if (i >= 0) entries.splice(i, 1);
    if (entries.length === 0) stopListening();
  };
}

/** Currently-registered shortcuts for the `?` cheat-sheet. Hidden shortcuts
 *  (power-user navigation we don't advertise) are intentionally excluded. */
export function getRegisteredShortcuts(): RegisteredShortcut[] {
  return entries
    .filter((e) => !e.hidden)
    .map(({ combo, description, scope }) => ({ combo, description, scope }));
}

/** Remove every shortcut + the listener (HMR / teardown). */
export function clearAllShortcuts(): void {
  entries.length = 0;
  stopListening();
}

// ---- Pretty-printing (cheat-sheet / tooltips) --------------------------------

const NAMED_KEY_LABELS: Record<string, string> = {
  escape: "Esc",
  " ": "Space",
  arrowup: "↑",
  arrowdown: "↓",
  arrowleft: "←",
  arrowright: "→",
  enter: "Enter",
  tab: "Tab",
  backspace: "⌫",
  delete: "Del",
};

function prettyKey(key: string): string {
  if (NAMED_KEY_LABELS[key]) return NAMED_KEY_LABELS[key];
  if (key.length === 1) return key.toUpperCase();
  return key.charAt(0).toUpperCase() + key.slice(1);
}

/** Human-readable combo for the cheat-sheet, e.g. "mod+," → "⌘ ," on macOS. */
export function prettyCombo(combo: string, isMac: boolean = isMacPlatform()): string {
  const p = parseCombo(combo);
  const parts: string[] = [];
  if (p.mod) parts.push(isMac ? "⌘" : "Ctrl");
  if (p.meta && !p.mod) parts.push(isMac ? "⌘" : "Meta");
  if (p.ctrl) parts.push(isMac ? "⌃" : "Ctrl");
  if (p.alt) parts.push(isMac ? "⌥" : "Alt");
  if (p.shift) parts.push(isMac ? "⇧" : "Shift");
  parts.push(prettyKey(p.key));
  return parts.join(" ");
}
