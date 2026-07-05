# Electron main process — design & roadmap

This document is the living roadmap for the Electron **main-process + preload +
packaging** domain (`apps/desktop/electron/`) — a solid, well-tested foundation.
It does NOT cover the renderer (`apps/desktop/src/**`), which is owned separately.

> **Glass note:** Kinora's Electron window uses native OS *material* —
> macOS `vibrancy: "under-window"` and Windows 11 `backgroundMaterial: "acrylic"`
> — both look great in their own right. This is **not** Liquid Glass, though.
> Real Liquid Glass is macOS-26-SDK-only and lives in `apps/desktop-native/`.
> Nothing here claims otherwise.

## Architecture

The old monolithic `main.ts` is now a thin **orchestrator** — a much cleaner shape.
Logic is split into two layers so the hard parts are unit-testable without
launching Electron:

- **`electron/core/`** — pure, Electron-free modules. No `import "electron"` at
  load time, so they run under plain Node (`node:test`).
- **`electron/services/`** — Electron-bound wrappers that wire the pure cores to
  `BrowserWindow`, `ipcMain`, `safeStorage`, `powerMonitor`, etc. Where a service
  needs a singleton (`safeStorage`, `app`), it is `require`d **lazily** inside a
  try/catch so the module still imports in Node (returning a degraded default).
- **`electron/shared/`** — the IPC contract, imported by BOTH main and preload.

```
electron/
  main.ts              orchestrator: builds + wires every service, drives lifecycle
  preload.ts           the ONLY bridge — one frozen window.kinora over contextBridge
  shared/
    ipc-contract.ts    channel names + payload types + frozen allowlists (source of truth)
  core/                (pure, tested)
    app-config.ts      env → typed AppConfig
    book-files.ts      pdf/epub recognition
    config-store.ts    atomic JSON store (injected AtomicFile)
    deep-link.ts       kinora:// parsing + route mapping
    deep-link-queue.ts queue links until renderer-ready
    fs-adapter.ts      node:fs AtomicFile (atomic tmp+rename)
    ipc-router.ts      typed, validated, least-privilege dispatch + validators
    logger.ts          ring buffer + sinks + secret redaction
    menu-template.ts   menu shape + accelerator registry (no electron)
    system-state.ts    power/network state equality + thermal normalisation
    token-codec.ts     auth-token envelope framing + plausibility check
    update-policy.ts   staged-rollout cohort math + status reducer
    window-state.ts    multi-display bounds reconciliation/clamping
  services/            (electron-bound)
    auto-update.ts     electron-updater adapter (optional dep, lazy require)
    diagnostics.ts     crashReporter + process-gone hooks + snapshot
    ipc-handlers.ts    registers every invoke handler + the dispatch shim
    menu.ts            Menu + globalShortcut + Tray
    monitors.ts        powerMonitor + net.isOnline → SystemState
    protocol.ts        kinora:// registration, single-instance, open-file/url
    secure-store.ts    safeStorage-backed token store (lazy safeStorage)
    window-manager.ts  multi-window, state persistence, navigation hardening
  __tests__/           node:test suites (run against compiled dist-electron/)
```

## Security posture (hardened IPC)

- `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`,
  `webSecurity: true` on every window.
- The preload exposes exactly one object (`window.kinora`); **no raw
  `ipcRenderer`** reaches the page. Every call funnels through two channels:
  `kinora:invoke` (request/response) and `kinora:send` (fire-and-forget).
- The main-process `IpcRouter` enforces a **frozen allowlist** + per-channel
  payload **validators** + an **origin allowlist** (dev server / `file://`).
  Handler throws are caught and returned as structured errors (no stack leaks,
  no hung promises).
- `will-navigate`, `setWindowOpenHandler`, and `will-attach-webview` are locked
  down: in-app navigation is same-origin only; external links go to the OS
  browser and only if `http(s)`/`mailto`; `<webview>` is blocked.
- Auth tokens are stored via `safeStorage` (Keychain/DPAPI/libsecret). When OS
  encryption is unavailable the token is stored **obfuscated + explicitly
  marked `plain`** so the diagnostics panel can surface the weaker posture.
- The logger **redacts** keys matching `token|secret|password|authorization|…`
  before anything reaches a sink.

## Capabilities delivered

| Area | Where | Tested |
|---|---|---|
| Typed/validated/least-privilege IPC | `core/ipc-router.ts`, `shared/ipc-contract.ts`, `services/ipc-handlers.ts`, `preload.ts` | ✅ ipc-router, ipc-contract |
| Auto-update + staged rollout + signature gate | `core/update-policy.ts`, `services/auto-update.ts` | ✅ update-policy, auto-update |
| Native menu + global shortcuts + tray | `core/menu-template.ts`, `services/menu.ts` | ✅ menu-template |
| Deep linking (`kinora://`) + file-open + single instance | `core/deep-link.ts`, `core/deep-link-queue.ts`, `core/book-files.ts`, `services/protocol.ts` | ✅ deep-link, deep-link-queue, book-files |
| Multi-window + window-state persistence | `core/window-state.ts`, `core/config-store.ts`, `services/window-manager.ts` | ✅ window-state, config-store |
| Crash reporting + structured logging + diagnostics | `core/logger.ts`, `services/diagnostics.ts` | ✅ logger |
| Secure session/token via safeStorage | `core/token-codec.ts`, `services/secure-store.ts` | ✅ token-codec, secure-store |
| Power/network monitors | `core/system-state.ts`, `services/monitors.ts` | ✅ system-state |
| Packaging (mac/win/linux) + signing/notarize stubs | `electron-builder.yml`, `build/` | n/a |

## Tests

Run without launching Electron: `pnpm --filter @kinora/desktop run test:electron`
(also part of `pnpm … run test`). The runner builds `dist-electron/` then
executes every `electron/__tests__/*.test.mjs` via `node --test`, importing the
**compiled** modules (the source uses `.js` import specifiers for the CJS build).
Current: **105 tests across 15 files, all green!**

## Additive shared-file changes

- `apps/desktop/package.json` — added scripts only: `test:electron`, and
  appended the electron runner to the existing `test` script. No deps changed.
- `apps/desktop/electron/tsconfig.json` — broadened `include` to the new
  subtree, added `target: ES2020` + `strict` + `noImplicitOverride` + `types:
  ["node"]`. Still emits to `dist-electron/`; the `main`/preload entry paths are
  unchanged.

## Optional runtime dependency

`electron-updater` is **not** a declared dependency. The auto-update service
`require`s it lazily; when absent (dev, or a build that didn't bundle it) the
updater gracefully reports `phase: "disabled"` instead of crashing. To enable
real updates, add `electron-updater` to `dependencies` and run a packaged build
that publishes to the `electron-builder.yml` `publish` target.

## Roadmap (next phases)

1. **Renderer wiring** (needs the renderer owner): subscribe to
   `window.kinora.onMenuAction` / `onDeepLink` / `update.onStatus`; call
   `window.kinora.ready()` on mount; render a diagnostics panel from
   `window.kinora.diagnostics()` + `logs()`.
2. **Update UX**: a small in-app toast driven by `update.onStatus`, with
   "Restart to update" calling `update.install()`.
3. **Session hardening**: rotate/expire the persisted token; bind it to the
   backend's refresh flow.
4. **Telemetry opt-in**: a remote `LogSink` behind an explicit user toggle.
5. **Packaging CI**: a GitHub Actions matrix that injects `CSC_*` / `APPLE_*` /
   `GH_TOKEN` and runs `electron-builder --config electron-builder.yml`.
6. **Window restore per-monitor**: persist + restore the `displayId` so a
   multi-monitor layout returns windows to their original screens.
