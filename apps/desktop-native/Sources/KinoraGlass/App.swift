import AppKit
import SwiftUI
import WebKit

/// Native macOS shell for Kinora. Built against the macOS 26+ SDK so the OS
/// enables the Liquid Glass design system — the chrome below is a *real*
/// NSGlassEffectView (via SwiftUI's `.glassEffect`), not a CSS approximation.
/// The existing React UI is hosted unchanged inside a WKWebView; opening a book
/// pops out a dedicated reader window (Apple Books style) via a native↔web bridge.
@main
struct KinoraGlassApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .frame(minWidth: 940, minHeight: 640)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1320, height: 880)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

enum Kinora {
    static let baseURL = "http://localhost:5173"
    static func url(_ path: String) -> URL { URL(string: baseURL + "/#" + path)! }
}

// MARK: - Library (main) window

struct LibraryView: View {
    var body: some View {
        ZStack(alignment: .top) {
            WebView(path: "/")
                .ignoresSafeArea()

            // Real Liquid Glass chrome floating over the web UI.
            HStack(spacing: 16) {
                Text("Kinora")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()
                Image(systemName: "magnifyingglass")
                Image(systemName: "square.and.arrow.up")
                Image(systemName: "person.crop.circle")
            }
            .font(.system(size: 15, weight: .medium))
            .foregroundStyle(.primary)
            .padding(.horizontal, 22)
            .frame(height: 50)
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 22))
            .padding(.horizontal, 16)
            .padding(.top, 12)
        }
    }
}

// MARK: - Reader (book) window — pops out Apple Books style

struct ReaderView: View {
    let bookId: String

    var body: some View {
        WebView(path: "/book/\(bookId)")
            .ignoresSafeArea()
    }
}

@MainActor
final class WindowManager {
    static let shared = WindowManager()
    private var windows: [NSWindow] = []

    func openBook(id: String) {
        let hosting = NSHostingController(rootView: ReaderView(bookId: id))
        let window = NSWindow(contentViewController: hosting)
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.title = "Kinora"
        window.isReleasedWhenClosed = false
        window.setContentSize(NSSize(width: 1120, height: 820))
        window.center()
        window.makeKeyAndOrderFront(nil)
        windows.append(window)
    }
}

// MARK: - WebView host (shared by library + reader windows)

struct WebView: NSViewRepresentable {
    let path: String

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let controller = WKUserContentController()
        controller.add(context.coordinator, name: "kinora")

        // Native↔web bridge: the React UI calls window.kinora.openBook(id),
        // which pops out a native reader window.
        let bridge = """
        window.__KINORA_NATIVE__ = true;
        window.kinora = window.kinora || {};
        window.kinora.openBook = function (id) {
            window.webkit.messageHandlers.kinora.postMessage({ type: 'openBook', id: String(id) });
            return Promise.resolve();
        };
        """
        controller.addUserScript(
            WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        )

        let config = WKWebViewConfiguration()
        config.userContentController = controller

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.load(URLRequest(url: Kinora.url(path)))
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKScriptMessageHandler {
        func userContentController(
            _ controller: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard let body = message.body as? [String: Any],
                  body["type"] as? String == "openBook",
                  let id = body["id"] as? String, !id.isEmpty else { return }
            Task { @MainActor in WindowManager.shared.openBook(id: id) }
        }
    }
}
