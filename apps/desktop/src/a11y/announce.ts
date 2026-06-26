// Screen-reader live-region announcer. One shared `polite` (status) region and
// one `assertive` (alert) region live at the end of <body>; `announce()` writes
// into the right one. Used for generation/crew status, save confirmations, etc.

export type Politeness = "polite" | "assertive";

const REGION_ID: Record<Politeness, string> = {
  polite: "kinora-live-polite",
  assertive: "kinora-live-assertive",
};

// Visually-hidden styles applied inline so the region works even before
// a11y.css loads (and so SR-only text never affects layout).
const SR_ONLY: Partial<CSSStyleDeclaration> = {
  position: "absolute",
  width: "1px",
  height: "1px",
  margin: "-1px",
  padding: "0",
  border: "0",
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  clipPath: "inset(50%)",
  whiteSpace: "nowrap",
};

function ensureRegion(politeness: Politeness): HTMLElement {
  const id = REGION_ID[politeness];
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement("div");
    el.id = id;
    el.setAttribute("aria-live", politeness);
    el.setAttribute("aria-atomic", "true");
    el.setAttribute("role", politeness === "assertive" ? "alert" : "status");
    Object.assign(el.style, SR_ONLY);
    document.body.appendChild(el);
  }
  return el;
}

// A zero-width space toggled each call so identical consecutive messages still
// register as a content change (otherwise some screen readers won't re-announce).
const NUDGE = "\u200B"; // zero-width space
let toggle = false;

export function announce(message: string, politeness: Politeness = "polite"): void {
  if (typeof document === "undefined") return;
  const el = ensureRegion(politeness);
  toggle = !toggle;
  el.textContent = toggle ? message : message + NUDGE;
}

/** Remove both live regions (HMR / teardown). */
export function clearAnnouncer(): void {
  if (typeof document === "undefined") return;
  document.getElementById(REGION_ID.polite)?.remove();
  document.getElementById(REGION_ID.assertive)?.remove();
}
