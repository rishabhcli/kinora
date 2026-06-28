import SwiftUI
import KinoraGlassKit

/// The top-level shell scene content: native glass chrome framing the WKWebView
/// (live renderer), with the showcase UI as the offline fallback. This is what a
/// `WindowGroup` renders.
struct ShellContainerView: View {
    @State private var model: ShellViewModel
    @State private var coordinator: WebShellCoordinator
    @State private var sidebar: SidebarItem = .home
    @State private var mode: WindowState.Mode = .viewer
    @State private var query = ""
    private let endpoint: RendererEndpoint

    /// External deep links pushed in by the AppDelegate / window controller.
    let deepLinkBus: DeepLinkBus

    init(environment: [String: String] = ProcessInfo.processInfo.environment,
         bundledIndexURL: URL? = BundledRenderer.indexURL,
         deepLinkBus: DeepLinkBus) {
        let resolved = RendererEndpoint.resolve(environment: environment, bundledIndexURL: bundledIndexURL)
        let vm = ShellViewModel()
        let backend: KeychainBackend
        #if canImport(Security)
        backend = SystemKeychainBackend()
        #else
        backend = InMemoryKeychainBackend()
        #endif
        let coord = WebShellCoordinator(viewModel: vm, tokenStore: TokenStore(backend: backend))
        _model = State(initialValue: vm)
        _coordinator = State(initialValue: coord)
        self.endpoint = resolved
        self.deepLinkBus = deepLinkBus
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            KinoraBackground()

            switch model.phase {
            case .live:
                // Renderer is up — native glass chrome framing the web view.
                webShell
            case .fallback:
                // No renderer reachable — the self-contained native showcase (always
                // works offline). A small glass "reconnect" pill sits top-trailing so the
                // user can re-attempt the live renderer without blocking the showcase.
                ShowcaseRootView().transition(.opacity)
                ReconnectPill { retryConnection() }
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
                    .padding(.top, 18)
                    .padding(.trailing, 18)
            case .connecting, .idle:
                // Connecting: glass progress overlay over the background.
                overlay
            }
        }
        .ignoresSafeArea()
        .onAppear { start() }
        .onReceive(deepLinkBus.publisher) { handleDeepLink($0) }
    }

    // MARK: - Web shell composition (chrome + web view)

    private var webShell: some View {
        HStack(spacing: 0) {
            GlassSidebar(selection: $sidebar) { item in
                coordinator.dispatchRoute(item.route)
            }
            VStack(spacing: 0) {
                GlassToolbar(query: $query,
                             onImport: presentImport,
                             onSearchSubmit: { coordinator.dispatchRoute("/search?q=\($0)") })
                ZStack(alignment: .bottom) {
                    WebShellView(coordinator: coordinator, endpoint: endpoint)
                        .clipShape(RoundedRectangle(cornerRadius: 18))
                        .overlay(RoundedRectangle(cornerRadius: 18).stroke(.white.opacity(0.1), lineWidth: 0.5))
                        .padding(.trailing, 10)
                        .padding(.bottom, 10)

                    GlassCommandBar(mode: $mode,
                                    onComment: { coordinator.dispatchRoute("/director/comment") },
                                    onTimeline: { coordinator.dispatchRoute("/director/timeline") },
                                    onCanon: { coordinator.dispatchRoute("/director/canon") })
                        .padding(.bottom, 22)
                }
            }
        }
    }

    private var overlay: some View {
        ConnectionOverlay(
            phase: model.phase,
            onRetry: { retryConnection() },
            onUseShowcase: { withAnimation { model.forceFallback(reason: "showcase requested") } }
        )
    }

    // MARK: - Lifecycle

    private func start() {
        coordinator.onReady = { dispatchPending() }
        coordinator.onImportFile = { url in coordinator.dispatchImport(url) }
        model.begin(endpoint: endpoint)
    }

    private func retryConnection() {
        withAnimation { model.begin(endpoint: endpoint) }
        coordinator.load(endpoint)
    }

    private func dispatchPending() {
        let links = model.markReady()
        coordinator.dispatch(links)
    }

    private func handleDeepLink(_ link: DeepLink) {
        if let ready = model.enqueue(link) {
            coordinator.dispatch([ready])
        }
        // else queued — flushed when the renderer signals ready.
    }

    private func presentImport() {
        if let path = coordinator.presentImportPanel() {
            handleDeepLink(.importFile(URL(fileURLWithPath: path)))
        }
    }
}

/// A small glass pill shown over the offline showcase, offering a one-tap retry of the
/// live renderer connection without blocking the showcase UI.
private struct ReconnectPill: View {
    var onTap: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 7) {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 11, weight: .bold))
                Text("Reconnect to renderer")
                    .font(.system(size: 11.5, weight: .semibold))
            }
            .foregroundStyle(.white.opacity(0.85))
            .padding(.horizontal, 13)
            .frame(height: 30)
        }
        .buttonStyle(.plain)
        .glassEffect(.regular, in: .capsule)
        .scaleEffect(hover ? 1.04 : 1)
        .animation(.smooth(duration: 0.2), value: hover)
        .onHover { hover = $0 }
        .help("Try connecting to the live renderer again")
    }
}

/// Locates a bundled `dist/index.html` inside the app's Resources, if a web build was
/// copied in at bundle time. Returns nil in `swift run` dev where only the dev server exists.
enum BundledRenderer {
    static var indexURL: URL? {
        Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "dist")
            ?? Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "Resources/dist")
    }
}
