import { describe, it, expect, afterEach, vi } from "vitest";
import {
  parseCombo,
  eventMatchesCombo,
  registerShortcut,
  getRegisteredShortcuts,
  clearAllShortcuts,
  prettyCombo,
  type KeyState,
} from "./keyboard";

afterEach(() => clearAllShortcuts());

function press(target: EventTarget, init: KeyboardEventInit) {
  target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
}

const ev = (over: Partial<KeyState> & { key: string }): KeyState => ({
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  ...over,
});

describe("parseCombo", () => {
  it("parses a bare key, lowercased, with no modifiers", () => {
    expect(parseCombo("R")).toEqual({
      key: "r",
      mod: false,
      ctrl: false,
      meta: false,
      shift: false,
      alt: false,
    });
  });

  it("parses 'mod' as the platform-agnostic command modifier", () => {
    const p = parseCombo("mod+,");
    expect(p.key).toBe(",");
    expect(p.mod).toBe(true);
  });

  it("parses shift + a symbol key", () => {
    const p = parseCombo("shift+?");
    expect(p).toMatchObject({ key: "?", shift: true });
  });

  it("aliases 'esc' and 'space' to their event.key values", () => {
    expect(parseCombo("esc").key).toBe("escape");
    expect(parseCombo("space").key).toBe(" ");
  });

  it("parses combined modifiers regardless of order/case", () => {
    expect(parseCombo("Shift+Alt+K")).toMatchObject({ key: "k", shift: true, alt: true });
  });
});

describe("eventMatchesCombo", () => {
  it("matches a bare letter case-insensitively", () => {
    const p = parseCombo("r");
    expect(eventMatchesCombo(ev({ key: "r" }), p, true)).toBe(true);
    expect(eventMatchesCombo(ev({ key: "R", shiftKey: true }), p, true)).toBe(true);
  });

  it("does NOT fire a bare letter when an unrelated modifier is held", () => {
    const p = parseCombo("r");
    expect(eventMatchesCombo(ev({ key: "r", ctrlKey: true }), p, true)).toBe(false);
    expect(eventMatchesCombo(ev({ key: "r", metaKey: true }), p, true)).toBe(false);
  });

  it("maps 'mod' to metaKey on macOS and ctrlKey elsewhere", () => {
    const p = parseCombo("mod+,");
    // mac: needs meta, not ctrl
    expect(eventMatchesCombo(ev({ key: ",", metaKey: true }), p, true)).toBe(true);
    expect(eventMatchesCombo(ev({ key: ",", ctrlKey: true }), p, true)).toBe(false);
    // non-mac: needs ctrl, not meta
    expect(eventMatchesCombo(ev({ key: ",", ctrlKey: true }), p, false)).toBe(true);
    expect(eventMatchesCombo(ev({ key: ",", metaKey: true }), p, false)).toBe(false);
  });

  it("matches shift+? (the help shortcut) on a US layout", () => {
    const p = parseCombo("shift+?");
    expect(eventMatchesCombo(ev({ key: "?", shiftKey: true }), p, true)).toBe(true);
  });

  it("requires shift only when the combo specifies it", () => {
    // '?' without explicit shift still matches even though shift produced it
    const p = parseCombo("?");
    expect(eventMatchesCombo(ev({ key: "?", shiftKey: true }), p, true)).toBe(true);
    // but a shift-specified combo must see shift
    const p2 = parseCombo("shift+?");
    expect(eventMatchesCombo(ev({ key: "?", shiftKey: false }), p2, true)).toBe(false);
  });

  it("matches named keys like Escape", () => {
    const p = parseCombo("esc");
    expect(eventMatchesCombo(ev({ key: "Escape" }), p, true)).toBe(true);
  });
});

describe("registerShortcut", () => {
  it("fires the handler on a matching keydown", () => {
    const fn = vi.fn();
    registerShortcut("r", fn);
    press(document.body, { key: "r" });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("stops firing after the returned unregister() is called", () => {
    const fn = vi.fn();
    const off = registerShortcut("r", fn);
    off();
    press(document.body, { key: "r" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("ignores shortcuts while the user is typing in a field (by default)", () => {
    const fn = vi.fn();
    registerShortcut("r", fn);
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    press(input, { key: "r" });
    expect(fn).not.toHaveBeenCalled();
    input.remove();
  });

  it("still fires in a field when whenInputFocused is true (e.g. Escape)", () => {
    const fn = vi.fn();
    registerShortcut("esc", fn, { whenInputFocused: true });
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    press(input, { key: "Escape" });
    expect(fn).toHaveBeenCalledTimes(1);
    input.remove();
  });

  it("exposes registered shortcuts (combo, description, scope) for the cheat-sheet", () => {
    registerShortcut("shift+?", () => {}, { description: "Keyboard shortcuts", scope: "Global" });
    registerShortcut("mod+,", () => {}, { description: "Settings", scope: "Global" });
    const list = getRegisteredShortcuts();
    expect(list).toHaveLength(2);
    expect(list[0]).toMatchObject({ combo: "shift+?", description: "Keyboard shortcuts", scope: "Global" });
  });

  it("calls preventDefault when requested", () => {
    registerShortcut("f", () => {}, { preventDefault: true });
    const e = new KeyboardEvent("keydown", { key: "f", bubbles: true, cancelable: true });
    document.body.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(true);
  });
});

describe("prettyCombo", () => {
  it("uses Apple glyphs on macOS", () => {
    expect(prettyCombo("mod+,", true)).toBe("⌘ ,");
    expect(prettyCombo("shift+?", true)).toBe("⇧ ?");
    expect(prettyCombo("mod+shift+p", true)).toBe("⌘ ⇧ P");
  });

  it("uses word modifiers off macOS", () => {
    expect(prettyCombo("mod+,", false)).toBe("Ctrl ,");
    expect(prettyCombo("alt+k", false)).toBe("Alt K");
  });

  it("titles named keys and upper-cases single letters", () => {
    expect(prettyCombo("esc", true)).toBe("Esc");
    expect(prettyCombo("?", true)).toBe("?");
    expect(prettyCombo("r", true)).toBe("R");
  });
});
