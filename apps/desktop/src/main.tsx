import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// In the native macOS shell (apps/desktop-native) the window is a real
// NSGlassEffectView; flag the document so the UI goes translucent and the
// genuine Liquid Glass shows through. Electron never sets this, so it's unaffected.
if ((window as unknown as { __KINORA_NATIVE__?: boolean }).__KINORA_NATIVE__) {
  document.documentElement.classList.add("kinora-native");
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
