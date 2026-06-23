# Kinora — native macOS shell

Real macOS **Liquid Glass** for the Kinora desktop app.

## Why this exists

Liquid Glass (the macOS 26+ design system, `NSGlassEffectView`) only turns on for
apps whose binary links the **macOS 26+ SDK**. Electron — even the latest 42 —
links an older SDK (our 33.x build reports `macosx14.5`), so macOS renders
Electron windows in legacy mode and real glass is impossible there. This native
SwiftUI/AppKit shell is built against the 26+ SDK (verify with
`otool -l … | grep -A4 LC_BUILD_VERSION` → `sdk 27.x`), so the glass is genuine.
It hosts the existing React renderer unchanged inside a `WKWebView`.

## Architecture

- `Sources/KinoraGlass/App.swift` — SwiftUI app: a real `.glassEffect` chrome bar
  over a `WKWebView` that loads the React renderer (`http://localhost:5173`).
- **Native↔web bridge:** `window.kinora.openBook(id)` posts to native, which pops
  out a dedicated reader window (Apple Books style). `window.__KINORA_NATIVE__`
  signals the web UI that it is running inside the native shell.
- **Auth** persists via the renderer's `localStorage` fallback (WKWebView storage,
  made durable by the bundle identity below).

## Requirements

macOS 26+ and Command Line Tools (Swift 6.2+, macOS 27 SDK). **No full Xcode
needed** — SwiftPM links against the CLT SDK.

## Develop

The React dev server must be running (`make app-desktop-dev` from the repo root
serves it at `http://localhost:5173`). Then:

```bash
swift run --package-path apps/desktop-native           # quick dev run
# …or build a real .app bundle (Info.plist, bundle id, min macOS 26) and open it:
bash apps/desktop-native/build-app.sh && open apps/desktop-native/KinoraGlass.app
```
