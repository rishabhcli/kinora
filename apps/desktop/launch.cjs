// Launcher: removes ELECTRON_RUN_AS_NODE and spawns Electron (cross-platform)
const { spawn } = require("child_process");
const path = require("path");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

// Set dev server URL if not already set (dev mode)
if (!env.VITE_DEV_SERVER_URL) {
  env.VITE_DEV_SERVER_URL = "http://localhost:5173";
}

// The `electron` package's main export is the absolute path to the platform's
// Electron binary (electron.exe on Windows, Electron.app/.../Electron on macOS,
// electron on Linux) — resolves correctly on every OS, unlike a hardcoded path.
const electronPath = require("electron");
const mainPath = path.join(__dirname, "dist-electron", "main.js");

const child = spawn(electronPath, [mainPath, ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
  cwd: __dirname,
});

child.on("close", (code) => {
  process.exit(code);
});
