import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { A11yProvider } from "./a11y/A11yProvider";
import { applyThemeAttribute } from "./a11y/readingPrefs";
// Side-effect init of the i18next singleton BEFORE first render so every
// useTranslation() consumer has the detected language + sets <html lang>.
import "./i18n";
// Single CSS aggregator (Captain seam) — imports tailwind + all owned partials,
// incl. a11y.css last so A6's focus-ring / reduced-motion overrides win.
import "./styles/index.css";

const root = document.documentElement;

// Set the global theme attribute before first paint so the whole app (incl. the
// login screen) renders in the reader's chosen theme with no flash of default.
applyThemeAttribute();

// In the native macOS shell (apps/desktop-native) the window is a real
// NSGlassEffectView; flag the document so the UI goes translucent and the
// genuine Liquid Glass shows through. Electron never sets this, so it's unaffected.
const nativeShell = Boolean((window as unknown as { __KINORA_NATIVE__?: boolean }).__KINORA_NATIVE__);
if (nativeShell) {
  root.classList.add("kinora-native");
}

function savedFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1" || localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

const params = new URLSearchParams(window.location.search);
const forceCinematic =
  params.has("cinematic") ||
  params.get("visuals") === "cinematic" ||
  savedFlag("kinora.cinematic");

// Balanced is the default desktop profile: keep the warm Kinora look, but avoid
// always-on refraction/filter paths that can saturate Electron's GPU process.
if (!forceCinematic && !nativeShell) root.classList.add("kinora-balanced");

// SVG-filter refraction behind backdrop-filter only renders in Chromium
// (Electron / Chrome), and it is one of the app's most expensive effects. Keep
// it opt-in for cinematic captures instead of forcing it on every session.
if (
  forceCinematic &&
  /Chrome\//.test(navigator.userAgent) &&
  !/Edg\//.test(navigator.userAgent)
) {
  root.classList.add("lg-refract-on");
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <App />
    </A11yProvider>
  </React.StrictMode>
);
