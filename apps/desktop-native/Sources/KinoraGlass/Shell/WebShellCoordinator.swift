import AppKit
import WebKit
import UserNotifications
import UniformTypeIdentifiers
import KinoraGlassKit

/// The glue between the `WKWebView`, the `BridgeRouter`, and AppKit side-effects.
///
/// Responsibilities:
///   • register every `BridgeContract.MessageName` handler on the web view's
///     `WKUserContentController` (reply-style for `getToken`/`importFile`);
///   • implement `BridgeHost` so the pure `BridgeRouter` can drive Keychain /
///     notifications / dock / NSWorkspace;
///   • own navigation-delegate callbacks → drive the `ShellViewModel` state machine;
///   • expose native→JS dispatch (`dispatchOpenBook`, `dispatchImport`, `dispatchRoute`).
///
/// This class is `@MainActor` because every WebKit / AppKit touch must be on the main
/// thread; the testable *decisions* live in the kit (`BridgeRouter`, `ShellViewModel`).
@MainActor
final class WebShellCoordinator: NSObject {
    private let viewModel: ShellViewModel
    private let tokenStore: TokenStore
    private let router = BridgeRouter()
    private weak var webView: WKWebView?
    /// Called when an import intent resolves to a file the renderer should ingest.
    var onImportFile: ((URL) -> Void)?
    /// Called when the renderer becomes ready (so the host can flush queued deep links).
    var onReady: (() -> Void)?

    init(viewModel: ShellViewModel, tokenStore: TokenStore) {
        self.viewModel = viewModel
        self.tokenStore = tokenStore
        super.init()
    }

    // MARK: - Web view construction

    /// Build a configured `WKWebView` with the bridge user-script + message handlers.
    func makeWebView() -> WKWebView {
        let controller = WKUserContentController()

        // Inject the bridge shim at documentStart, before the React bundle runs, in the
        // main frame only.
        let shim = WKUserScript(
            source: BridgeContract.userScriptSource(),
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        controller.addUserScript(shim)

        // Fire-and-forget handlers.
        for name in BridgeContract.MessageName.allCases where !Self.replyHandlers.contains(name) {
            controller.add(self, name: name.rawValue)
        }
        // Reply-style handlers (return a value to JS via a Promise).
        for name in Self.replyHandlers {
            controller.addScriptMessageHandler(self, contentWorld: .page, name: name.rawValue)
        }

        let config = WKWebViewConfiguration()
        config.userContentController = controller
        config.websiteDataStore = .default()          // persistent: keeps the renderer's localStorage token
        config.allowsAirPlayForMediaPlayback = true
        config.suppressesIncrementalRendering = false
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        if #available(macOS 12.0, *) {
            config.preferences.isElementFullscreenEnabled = true
        }
        // Autoplay the AI films without a click (the reading room expects it).
        config.mediaTypesRequiringUserActionForPlayback = []

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // transparent → glass shows through
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsLinkPreview = false
        if #available(macOS 13.3, *) {
            webView.isInspectable = true                     // Web Inspector in dev
        }
        self.webView = webView
        return webView
    }

    private static let replyHandlers: Set<BridgeContract.MessageName> = [.getToken, .importFile]

    /// Load the resolved endpoint.
    func load(_ endpoint: RendererEndpoint) {
        guard let webView, let url = endpoint.url else { return }
        switch endpoint {
        case .devServer(let u):
            webView.load(URLRequest(url: u, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8))
        case .bundled:
            // Bundled file:// build — grant read access to its directory.
            webView.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        case .showcase:
            break
        }
        Log.shell.info("loading \(endpoint.label, privacy: .public)")
    }

    func reload() { webView?.reload() }

    // MARK: - Native → JS dispatch

    func dispatchOpenBook(_ id: String) {
        evaluate("\(BridgeContract.openBookJSFunction)(\(jsString(id)))")
    }
    func dispatchImport(_ url: URL) {
        let descriptor = "{path: \(jsString(url.path)), name: \(jsString(url.lastPathComponent))}"
        evaluate("\(BridgeContract.importJSFunction)(\(descriptor))")
    }
    func dispatchRoute(_ path: String) {
        evaluate("\(BridgeContract.routeJSFunction)(\(jsString(path)))")
    }
    /// Flush a list of queued deep links to the now-ready renderer.
    func dispatch(_ links: [DeepLink]) {
        for link in links {
            switch link {
            case .openBook(let id): dispatchOpenBook(id)
            case .importFile(let u): dispatchImport(u)
            case .route(let p): dispatchRoute(p)
            case .home: dispatchRoute("/")
            }
        }
    }

    private func evaluate(_ js: String) {
        webView?.evaluateJavaScript(js) { _, error in
            if let error { Log.bridge.error("evaluate failed: \(error.localizedDescription, privacy: .public)") }
        }
    }

    /// JSON-encode a string for safe interpolation into a JS expression.
    private func jsString(_ s: String) -> String {
        if let data = try? JSONEncoder().encode(s), let str = String(data: data, encoding: .utf8) {
            return str
        }
        return "\"\""
    }
}

// MARK: - BridgeHost (drives the pure router's side-effects)

extension WebShellCoordinator: BridgeHost {
    func persistToken(_ token: String) { tokenStore.save(token) }
    func clearPersistedToken() { tokenStore.delete() }
    func currentToken() -> String? { tokenStore.token }

    func postNotification(title: String, body: String, id: String?) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: id ?? UUID().uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request) { error in
            if let error { Log.app.error("notify failed: \(error.localizedDescription, privacy: .public)") }
        }
    }

    func setDockBadge(_ count: Int) {
        NSApp.dockTile.badgeLabel = count > 0 ? String(count) : nil
    }

    func openExternal(_ url: URL) { NSWorkspace.shared.open(url) }

    func rendererBecameReady() {
        Log.shell.info("renderer ready")
        onReady?()
    }

    func rendererLog(level: BridgeMessage.LogLevel, message: String) {
        switch level {
        case .log: Log.shell.debug("[web] \(message, privacy: .public)")
        case .warn: Log.shell.warning("[web] \(message, privacy: .public)")
        case .error: Log.shell.error("[web] \(message, privacy: .public)")
        }
    }

    func presentImportPanel() -> String? {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = ImportTypes.contentTypes
        panel.message = "Choose a PDF or EPUB to adapt into a Kinora film."
        return panel.runModal() == .OK ? panel.url?.path : nil
    }
}

// MARK: - WKScriptMessageHandler (fire-and-forget)

extension WebShellCoordinator: WKScriptMessageHandler {
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        do {
            let decoded = try BridgeMessage.decode(name: message.name, body: message.body)
            router.route(decoded, host: self)
        } catch {
            Log.bridge.error("bad bridge message '\(message.name, privacy: .public)': \(String(describing: error), privacy: .public)")
        }
    }
}

// MARK: - WKScriptMessageHandlerWithReply (request/response)

extension WebShellCoordinator: WKScriptMessageHandlerWithReply {
    func userContentController(
        _ controller: WKUserContentController,
        didReceive message: WKScriptMessage
    ) async -> (Any?, String?) {
        do {
            let decoded = try BridgeMessage.decode(name: message.name, body: message.body)
            let reply = router.route(decoded, host: self)
            return (reply, nil)
        } catch {
            Log.bridge.error("bad reply message '\(message.name, privacy: .public)'")
            return (nil, "decode failed")
        }
    }
}

// MARK: - WKNavigationDelegate → state machine

extension WebShellCoordinator: WKNavigationDelegate {
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        viewModel.markLive()
        Log.shell.info("navigation finished — live")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(error)
    }

    private func handleNavigationFailure(_ error: Error) {
        let retry = viewModel.markConnectFailure(error.localizedDescription)
        Log.shell.error("navigation failed (retry=\(retry)): \(error.localizedDescription, privacy: .public)")
        if retry {
            // Backoff retry against the same endpoint.
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                guard let self else { return }
                self.load(self.viewModel.endpoint)
            }
        }
    }

    // Route normal http(s) link clicks to the system browser; keep the SPA in-app.
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction
    ) async -> WKNavigationActionPolicy {
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url,
           let scheme = url.scheme?.lowercased(),
           scheme == "http" || scheme == "https",
           !isRendererOrigin(url) {
            NSWorkspace.shared.open(url)
            return .cancel
        }
        return .allow
    }

    private func isRendererOrigin(_ url: URL) -> Bool {
        guard let base = viewModel.endpoint.url else { return false }
        return url.host == base.host && url.port == base.port
    }
}

// MARK: - WKUIDelegate (full-screen video, JS dialogs)

extension WebShellCoordinator: WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo
    ) async {
        let alert = NSAlert()
        alert.messageText = "Kinora"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }
}

/// Supported import file types, derived once. Kept here (not the kit) since UTType is
/// AppKit/UniformTypeIdentifiers territory.
enum ImportTypes {
    static var contentTypes: [UTType] {
        var types: [UTType] = [.pdf]
        if let epub = UTType("org.idpf.epub-container") { types.append(epub) }
        return types
    }
}
