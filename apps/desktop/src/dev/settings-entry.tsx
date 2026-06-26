// Dev-only entry to preview the real <SettingsPage> in isolation (served by
// `vite` dev at /settings-preview.html). Mounts the actual component, so the
// screenshots are of the shipping panel — just without the login/nav chrome.
import { createRoot } from "react-dom/client";
import SettingsPage from "../components/SettingsPage";
import "../index.css";

createRoot(document.getElementById("root")!).render(
  <div className="kinora-bg min-h-screen">
    <SettingsPage />
  </div>,
);
