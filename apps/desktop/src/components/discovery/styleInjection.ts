// Owned, self-contained style injection for the discovery surface. The shared
// CSS files (src/styles/*.css) belong to other agents, so the discovery surface
// ships its own keyframes/utility classes by injecting a single <style> element
// once, idempotently. SSR/jsdom-safe (no-ops without `document`).
const STYLE_ID = "kinora-discovery-styles";

const CSS = `
@keyframes discovery-pop {
  from { opacity: 0; transform: translateY(2%) scale(0.97); }
  to   { opacity: 1; transform: translateY(-8%) scale(1); }
}
@keyframes discovery-row-in {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}
.discovery-row-in { animation: discovery-row-in 480ms cubic-bezier(0.22,1,0.36,1) both; }
@media (prefers-reduced-motion: reduce) {
  .discovery-row-in { animation: none; }
}
`;

let injected = false;

/** Inject the discovery stylesheet once. Safe to call from many components. */
export function ensureDiscoveryStyles(): void {
  if (injected) return;
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ID)) {
    injected = true;
    return;
  }
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CSS;
  document.head.appendChild(el);
  injected = true;
}

/** Test-only: reset the injection guard. */
export function resetDiscoveryStylesForTest(): void {
  injected = false;
  if (typeof document !== "undefined") {
    document.getElementById(STYLE_ID)?.remove();
  }
}
