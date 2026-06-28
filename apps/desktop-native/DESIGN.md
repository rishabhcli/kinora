# KinoraGlass — the native macOS Liquid Glass shell

> Living roadmap for `apps/desktop-native`. This is the **real** Liquid Glass surface
> for Kinora — SwiftUI `.glassEffect` / AppKit `NSGlassEffectView`, compiled against the
> macOS 26+ SDK. CSS `backdrop-filter` in the Electron app is an *imitation*; this is the
> genuine article and the only place the OS turns Liquid Glass on. Keep this file current
> as milestones land.

---

## 1. What this app is (and is not)

Kinora's primary product is the Electron desktop app (`apps/desktop`) — auth, library, the
two-pane reading room, live backend wiring. **This** app is a *native shell* whose job is to
host that same React renderer inside a `WKWebView` floating over **real Liquid Glass** native
chrome (sidebar, toolbar, command bar, traffic-light region), proving the glass that Electron
structurally cannot render (Electron 33 links the macOS 14.5 SDK, below the 26 gate).

- **It is**: a thin, native, glass-everywhere shell + a faithful JS bridge that mirrors the
  Electron preload contract (`window.__KINORA_NATIVE__`, `window.kinora` token bridge +
  `openBook` deep-linking), plus native menus, shortcuts, multi-window, file-drop import,
  full-screen immersive reading, and native notifications.
- **It is not**: a fork of the React UI, a backend client, or a second design language. The web
  UI owns its own chrome; when `__KINORA_NATIVE__` is set it defers the window frame to us.

There are **two faces** of this package, both real and both shipped:

1. **Showcase mode** (`HomeView`/`LibraryView`/`WatchView`/`SettingsView`, pre-existing) — a
   self-contained native UI demonstrating glass on every control, backed by bundled demo films.
   No server needed. This is the launchpad / offline fallback.
2. **Shell mode** (new) — the `WKWebView` host that loads the live renderer (dev `:5173` or a
   bundled build) behind the native glass strip, with the full bridge.

A launcher chooses between them; shell mode falls back to showcase mode automatically when no
renderer is reachable, so the app is never a blank window.

---

## 2. The bridge contract (authoritative — mirrors the Electron preload)

The Electron preload (`apps/desktop/electron/preload.ts`) today exposes exactly one thing:
`window.__KINORA_NATIVE__ = true` on macOS/Windows. The renderer (`apps/desktop/src/main.tsx`)
reads it to add `html.kinora-native` and go translucent. Our shell **must** set the same flag,
and additionally provides the richer `window.kinora` object the CLAUDE.md native-shell note
promises ("`window.kinora` (token bridge + `openBook`) mirrors the Electron preload"). Because
the renderer keeps its bearer token in `localStorage["kinora.token"]` (`api.ts`), the bridge is
deliberately **non-invasive**: it never forces a token into the page; it observes and persists.

`window.kinora` surface (injected at `documentStart`, before the React bundle runs):

| Member | Direction | Purpose |
|---|---|---|
| `kinora.native` | read | `true` — same meaning as `__KINORA_NATIVE__`, convenience alias |
| `kinora.platform` | read | `"macos-native"` |
| `kinora.version` | read | shell semantic version string |
| `kinora.getToken()` | JS→native | returns the token the shell persisted (Keychain), or the page's own localStorage token |
| `kinora.setToken(t)` | JS→native | persist a token to the Keychain (called by the renderer on login) |
| `kinora.clearToken()` | JS→native | drop the persisted token (logout) |
| `kinora.openBook(id)` | native→JS | navigate the renderer to a book; the shell calls this for deep links / file-drop |
| `kinora.onOpenBook(cb)` | JS registers | the renderer registers a handler; the shell flushes any queued deep links |
| `kinora.notify({title,body})` | JS→native | post a native `UNUserNotification` |
| `kinora.setBadge(n)` | JS→native | set the Dock badge |
| `kinora.ready()` | JS→native | renderer signals it has mounted; flush queued `openBook` calls |

Transport: `WKScriptMessageHandlerWithReply` for request/response (`getToken`), plain
`WKScriptMessageHandler` for fire-and-forget (`setToken`, `notify`, …), and
`webView.evaluateJavaScript` for native→JS (`openBook`). A small JS shim (`bridge.js`,
injected as a `WKUserScript`) defines `window.kinora` and `window.__KINORA_NATIVE__` and marshals
to/from the message handlers, so the page sees a clean synchronous-looking API.

Deep links: the custom URL scheme `kinora://book/<id>` (and `kinora://open?...`) maps to
`openBook`. File-drop / "Open With" a `.pdf`/`.epub` maps to an import intent
(`kinora://import?path=...`) which the renderer turns into an upload.

---

## 3. Package / target layout

```
apps/desktop-native/
  Package.swift                      swift-tools 6.0, .macOS("26.0"), 3 targets
  build-app.sh                       CLT-only .app bundler (referenced by Makefile)
  DESIGN.md                          this file
  Sources/
    KinoraGlassKit/                  LIBRARY — pure, testable, no live-server dependency
      Bridge/
        BridgeContract.swift         message names, payload codables, the JS shim source
        BridgeMessage.swift          typed inbound/outbound message model + decoding
        TokenStore.swift             Keychain-backed token persistence (protocol + impl)
        DeepLink.swift               kinora:// URL parsing → intents
      Model/
        Book.swift                   shared book model (used by showcase + shell)
        RendererEndpoint.swift       dev :5173 vs bundled file:// resolution + reachability
        ShellSettings.swift          persisted shell prefs (UserDefaults-backed)
      ViewModel/
        ShellViewModel.swift         observable shell state machine (connecting/live/showcase)
        WindowState.swift            per-window restorable state (Codable)
      Util/
        Log.swift                    os.Logger category helpers
    KinoraGlass/                     EXECUTABLE — the AppKit/SwiftUI shell + showcase
      KinoraApp.swift                @main App + scenes + commands wiring (existing, extended)
      Shell/
        WebShellView.swift           NSViewRepresentable wrapping WKWebView + glass
        WebShellCoordinator.swift    WKNavigationDelegate + script message handling
        GlassChrome.swift            native glass sidebar / toolbar / command bar
        ConnectionOverlay.swift      "connecting to renderer…" glass overlay + retry
      Showcase/                      (existing views moved under here conceptually)
        ReaderView.swift  WatchView.swift  SettingsView.swift  (+ Home/Library in KinoraApp)
      AppKit/
        AppDelegate.swift            NSApplicationDelegate: URL events, notifications, dock
        MainMenu.swift               native menu bar + ⌘-shortcuts
        TouchBar.swift               NSTouchBar provider for reading controls
        WindowController.swift       multi-window, state restoration, full-screen
      Resources/                     Info.plist bits, bundled films (existing)
  Tests/
    KinoraGlassKitTests/             XCTest — bridge decode, token store, deep links, VM, endpoint
```

Why split a library target out: SwiftPM can only run unit tests against a **library**, not an
`executableTarget` that has an `@main`. All the logic worth testing (bridge marshalling, deep-link
parsing, token store, endpoint resolution, the shell state machine) lives in `KinoraGlassKit` and
is covered by `KinoraGlassKitTests`. The executable stays a thin UI/AppKit shell over it.

---

## 4. Milestones

- [x] **M0 — Recon & baseline.** Read CLAUDE.md + kinora.md §5; confirmed Swift 6.4 / macOS 27.0
      SDK under `/Applications/Xcode-beta.app`; baseline `swift build` is green.
- [x] **M1 — Kit foundation.** Split `KinoraGlassKit` library target; Book model, endpoint
      resolver, shell settings, logging. Package.swift → 3 targets. Build green.
- [x] **M2 — Bridge core.** `BridgeContract` (JS shim), typed `BridgeMessage`, `TokenStore`
      (Keychain), `DeepLink` parser. **77 unit tests, all green.**
- [x] **M3 — Web shell.** `WebShellView` (WKWebView composited over a real
      `NSGlassEffectView`, legacy `NSVisualEffectView` fallback below 26),
      `WebShellCoordinator` wiring every message handler (fire-and-forget + async reply),
      `ConnectionOverlay` + showcase fallback, `ShellContainerView` composing it all.
- [x] **M4 — Native chrome.** `GlassChrome` — glass sidebar + toolbar (traffic-light inset)
      + floating Director command bar; sidebar/commands route into the renderer via the
      deep-link bridge; the renderer defers its own chrome via `__KINORA_NATIVE__`.
- [x] **M5 — AppKit integration.** `AppDelegate`: `kinora://` URL-scheme events, file
      open / Open-With import, `UNUserNotificationCenter` (auth + foreground + tap-routing),
      dock-tile recents menu. `KinoraCommands`: full ⌘-shortcut menu set (New Window, Import,
      Viewer/Director, page nav, play/pause, comment/timeline/canon, immersive toggle).
      Multi-window via a second `WindowGroup(for: String.self)` + restorable `WindowState`/
      `WindowStateRegistry` in the kit. Full-screen immersive reading (`toggleFullScreen`).
      `KinoraTouchBarProvider` reading-controls Touch Bar. `ShellPreferencesView` Settings scene.
- [x] **M6 — Bundler.** `build-app.sh` release-builds + assembles `KinoraGlass.app` with an
      Info.plist declaring the `kinora://` URL scheme, PDF/EPUB document types, notification
      usage + `LSMinimumSystemVersion 26.0`; copies the SwiftPM resource bundle (demo films);
      optionally embeds `apps/desktop/dist` (`KINORA_EMBED_RENDERER=1`); ad-hoc signs. Wired to
      the existing Makefile `app-native-bundle` target. **Verified: bundle assembles + signs.**
- [x] **M7 — Polish & docs.** Zero-warning clean build, 86 green tests, README (this file +
      §8 below), roadmap finalised.

---

## 5. Build / verification status

- **Toolchain present**: YES. `swift` 6.4, target `arm64-apple-macosx27.0.0`, SDK 27.0, via
  `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer`. Real Liquid Glass APIs compile.
- **Baseline build**: `swift build` GREEN (Swift-6 actor-isolation *warnings* only, no errors).
- Build the library + executable:
  `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer swift build --package-path apps/desktop-native`
- Run the test suite:
  `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer swift test --package-path apps/desktop-native`
- Run the shell: `make app-native` (auto-sets DEVELOPER_DIR). Bundle: `make app-native-bundle`.

(Status lines below are updated as milestones land — see the changelog at the end.)

---

## 6. Design rules (do not regress)

- Real Liquid Glass only: `NSGlassEffectView` / SwiftUI `.glassEffect`. Never describe CSS as it.
- Stay inside `apps/desktop-native/`. Treat the renderer (`apps/desktop/`) as read-only; rely on
  its documented bridge flag only.
- The bridge is non-invasive: never overwrite the page's own auth state; observe + persist.
- Showcase mode must always work offline (bundled films), so the app never shows a blank window.
- Keep `KinoraGlass` (executable) thin; put testable logic in `KinoraGlassKit`.

## 7. Changelog

- M0 landed: recon complete, baseline build verified green on the real 26+ SDK toolchain.
- M1–M7 landed in one pass:
  - Package split into `KinoraGlassKit` (lib) + `KinoraGlass` (exe) + `KinoraGlassKitTests`.
  - Bridge core (`BridgeContract` JS shim, typed `BridgeMessage` decode, `BridgeRouter`,
    `TokenStore` Keychain, `DeepLink` parser), all `@MainActor` where they touch AppKit.
  - Web shell (`WebShellView`/`WebShellCoordinator`/`ShellContainerView`), native glass
    chrome (`GlassChrome`), connection overlay, deep-link bus, AppDelegate, menus, Touch Bar,
    preferences, `build-app.sh`.
  - Showcase migrated onto the kit's `Book` model (kept as `KBook` typealias), `RootView`
    renamed `ShowcaseRootView`, ReaderView player-loop Swift-6 warning fixed.
  - Shell phase→view flow: `.live` shows the glass-chrome web shell; `.connecting`/`.idle`
    shows the glass `ConnectionOverlay`; `.fallback` shows the offline showcase with a
    non-blocking glass `ReconnectPill` (top-trailing) to re-attempt the live renderer.
  - **Verification: `swift build` clean (0 warnings, 0 errors, full clean compile in ~21s),
    `swift test` 86/86 green, `build-app.sh` assembles + ad-hoc signs `KinoraGlass.app`
    against the macOS 27.0 SDK (Info.plist URL scheme + doc types + LSMinimumSystemVersion 26.0
    all verified).** Toolchain: Swift 6.4, `arm64-apple-macosx27.0.0`, Xcode-beta.

## 8. How to run / build / test

```bash
# From the repo root. The Makefile auto-sets DEVELOPER_DIR to the installed Xcode.

# Live shell against the renderer dev server (run `make app-desktop-dev` in another shell):
make app-native

# Or with an explicit toolchain + custom port:
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  KINORA_RENDERER_URL=http://localhost:5173 \
  swift run --package-path apps/desktop-native KinoraGlass

# Unit tests (kit logic — bridge, deep-links, token store, view-model, endpoint, settings):
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  swift test --package-path apps/desktop-native

# Real .app bundle (Info.plist URL scheme + doc types, demo films, optional embedded renderer):
make app-native-bundle
KINORA_EMBED_RENDERER=1 make app-native-bundle   # ship with apps/desktop/dist embedded
```

Runtime knobs:
- `KINORA_RENDERER_URL` — override the renderer origin (`http(s)://…`, a `file://index.html`,
  or the literal `showcase` to force the offline UI).
- With no server reachable, the shell shows the connection overlay, retries with backoff, then
  drops to the bundled-films showcase so the window is never blank.
