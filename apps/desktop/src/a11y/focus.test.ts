import { describe, it, expect, beforeEach } from "vitest";
import { getFocusable, trapFocus, restoreFocus } from "./focus";

beforeEach(() => {
  document.body.innerHTML = "";
});

function mount(html: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = html;
  document.body.appendChild(root);
  return root;
}

describe("getFocusable", () => {
  it("returns visible, enabled, tabbable elements in DOM order", () => {
    const root = mount(`
      <a href="#x">link</a>
      <button>b1</button>
      <input />
      <button disabled>nope</button>
      <div tabindex="-1">skip</div>
      <div tabindex="0">div</div>
    `);
    const labels = getFocusable(root).map((el) => el.textContent?.trim() || el.tagName.toLowerCase());
    expect(labels).toEqual(["link", "b1", "input", "div"]);
  });

  it("excludes elements hidden via inline display:none and aria-hidden", () => {
    const root = mount(`
      <button>shown</button>
      <button style="display:none">hidden</button>
      <button aria-hidden="true">aria</button>
    `);
    expect(getFocusable(root).map((b) => b.textContent)).toEqual(["shown"]);
  });
});

describe("trapFocus", () => {
  it("wraps Tab from the last element back to the first", () => {
    const root = mount(`<button>a</button><button>b</button><button>c</button>`);
    const [a, , c] = Array.from(root.querySelectorAll("button"));
    const release = trapFocus(root);
    c.focus();
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    expect(document.activeElement).toBe(a);
    release();
  });

  it("wraps Shift+Tab from the first element back to the last", () => {
    const root = mount(`<button>a</button><button>b</button><button>c</button>`);
    const [a, , c] = Array.from(root.querySelectorAll("button"));
    const release = trapFocus(root);
    a.focus();
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }));
    expect(document.activeElement).toBe(c);
    release();
  });

  it("stops trapping after release()", () => {
    const root = mount(`<button>a</button><button>b</button>`);
    const [a, b] = Array.from(root.querySelectorAll("button"));
    const release = trapFocus(root);
    release();
    b.focus();
    root.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    // no wrap — focus stays put
    expect(document.activeElement).toBe(b);
  });
});

describe("restoreFocus", () => {
  it("returns focus to a previously-focused element", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);
    const other = document.createElement("input");
    document.body.appendChild(other);
    other.focus();
    restoreFocus(opener);
    expect(document.activeElement).toBe(opener);
  });

  it("is a no-op for null", () => {
    expect(() => restoreFocus(null)).not.toThrow();
  });
});
