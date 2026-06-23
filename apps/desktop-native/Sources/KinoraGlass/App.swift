import AppKit
import SwiftUI
import UniformTypeIdentifiers
import WebKit

/// Native macOS shell for Kinora. Built against the macOS 26+ SDK so the OS
/// enables the real Liquid Glass design system (NSGlassEffectView via SwiftUI
/// `.glassEffect`). Hosts the existing React renderer unchanged in a WKWebView
/// and bridges window.kinora to native (mirrors the Electron preload).
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
        .commands {
            CommandGroup(after: .sidebar) {
                Button("Reload") {
                    NotificationCenter.default.post(name: .kinoraReload, object: nil)
                }
                .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}

extension Notification.Name {
    static let kinoraReload = Notification.Name("kinora.reload")
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
    static let apiBaseURL = "http://localhost:8000"
    static func url(_ path: String) -> URL { URL(string: baseURL + "/#" + path)! }
}

/// Native "Add book": pick a PDF/EPUB with the system panel and upload it to the
/// backend with the persisted token, then refresh the library.
enum BookUpload {
    @MainActor static func present() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.pdf, UTType(filenameExtension: "epub") ?? .data]
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            upload(url)
        }
    }

    static func upload(_ fileURL: URL) {
        guard let token = TokenStore.get(),
              let fileData = try? Data(contentsOf: fileURL),
              let endpoint = URL(string: Kinora.apiBaseURL + "/api/books") else { return }
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let mime = fileURL.pathExtension.lowercased() == "epub" ? "application/epub+zip" : "application/pdf"
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(fileURL.lastPathComponent)\"\r\n"
                .data(using: .utf8)!
        )
        body.append("Content-Type: \(mime)\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        URLSession.shared.dataTask(with: req) { _, response, _ in
            if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                DispatchQueue.main.async { NotificationCenter.default.post(name: .kinoraReload, object: nil) }
            }
        }.resume()
    }
}

/// Durable token store mirroring the Electron safeStorage bridge; backs the
/// renderer's window.kinora.secure interface so login survives relaunch.
enum TokenStore {
    private static let key = "kinora.token"
    static func get() -> String? { UserDefaults.standard.string(forKey: key) }
    static func set(_ token: String?) {
        if let token, !token.isEmpty {
            UserDefaults.standard.set(token, forKey: key)
        } else {
            UserDefaults.standard.removeObject(forKey: key)
        }
    }
}

// MARK: - Library (main) window

struct LibraryView: View {
    var body: some View {
        ZStack(alignment: .top) {
            WebView(path: "/")
                .ignoresSafeArea()

            // Real Liquid Glass title strip (branding + drag region). The web UI
            // owns the functional controls (search / add / profile).
            HStack(spacing: 14) {
                Text("Kinora")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()
                Button { BookUpload.present() } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.glass)
                .help("Add a book (PDF or EPUB)")
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 22)
            .frame(height: 48)
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

        let tokenLiteral: String = TokenStore.get()
            .map { "\"\($0.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\""))\"" }
            ?? "null"

        // Mirror the Electron preload so the renderer runs unchanged:
        // { platform, secure.getToken/setToken, openBook }.
        let bridge = """
        window.__KINORA_NATIVE__ = true;
        window.__KINORA_TOKEN__ = \(tokenLiteral);
        window.kinora = window.kinora || {};
        window.kinora.platform = 'darwin';
        window.kinora.secure = {
            getToken: function () { return Promise.resolve(window.__KINORA_TOKEN__ || null); },
            setToken: function (t) {
                window.__KINORA_TOKEN__ = (t == null ? null : String(t));
                window.webkit.messageHandlers.kinora.postMessage({ type: 'setToken', token: (t == null ? '' : String(t)) });
                return Promise.resolve();
            }
        };
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
        NotificationCenter.default.addObserver(
            forName: .kinoraReload, object: nil, queue: .main
        ) { [weak webView] _ in webView?.reload() }
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKScriptMessageHandler {
        func userContentController(
            _ controller: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard let body = message.body as? [String: Any],
                  let type = body["type"] as? String else { return }
            switch type {
            case "setToken":
                TokenStore.set((body["token"] as? String).flatMap { $0.isEmpty ? nil : $0 })
            case "openBook":
                if let id = body["id"] as? String, !id.isEmpty {
                    Task { @MainActor in WindowManager.shared.openBook(id: id) }
                }
            default:
                break
            }
        }
    }
}
