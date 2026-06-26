import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { A11yProvider } from "./a11y/A11yProvider";
import "./index.css";
// Accessibility layer styles — imported AFTER index.css so the global focus ring
// and reduced-motion rules win on equal specificity. (Agent 06)
import "./styles/a11y.css";

// In the native macOS shell (apps/desktop-native) the window is a real
// NSGlassEffectView; flag the document so the UI goes translucent and the
// genuine Liquid Glass shows through. Electron never sets this, so it's unaffected.
if ((window as unknown as { __KINORA_NATIVE__?: boolean }).__KINORA_NATIVE__) {
  document.documentElement.classList.add("kinora-native");
}

// SVG-filter refraction behind backdrop-filter only renders in Chromium
// (Electron / Chrome). WebKit (the Swift shell's WKWebView) and Firefox keep the
// plain blur+specular glass. Gate the displacement on a Chromium marker class.
if (/Chrome\//.test(navigator.userAgent) && !/Edg\//.test(navigator.userAgent)) {
  document.documentElement.classList.add("lg-refract-on");
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <A11yProvider>
      <App />
    </A11yProvider>
  </React.StrictMode>
);
