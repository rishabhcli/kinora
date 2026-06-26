import AppKit
import WebKit

/// Native macOS shell for Kinora. An NSWindow whose background is a genuine
/// system glass material — `NSGlassEffectView` (Liquid Glass, macOS 26+) when
/// available, `NSVisualEffectView` vibrancy otherwise — hosting the React
/// renderer in a transparent `WKWebView` so the glass shows through the chrome.
final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let urlString = ProcessInfo.processInfo.environment["KINORA_URL"] ?? "http://localhost:5173"
        guard let url = URL(string: urlString) else { return }

        let frame = NSRect(x: 0, y: 0, width: 1280, height: 820)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Kinora"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = true
        window.backgroundColor = .clear

        // Transparent web view so the native glass behind it is visible. The web
        // UI keys off `window.__KINORA_NATIVE__` to defer its chrome to the shell.
        let config = WKWebViewConfiguration()
        let nativeFlag = WKUserScript(
            source: "window.__KINORA_NATIVE__ = true;",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        )
        config.userContentController.addUserScript(nativeFlag)
        let web = WKWebView(frame: frame, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground") // KVC: let the glass show through
        web.underPageBackgroundColor = .clear           // belt-and-suspenders transparency
        web.load(URLRequest(url: url))

        let container = makeGlassContainer(frame: frame, content: web)
        window.contentView = container
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Real Liquid Glass on macOS 26+, real vibrancy below — never a CSS fake.
    private func makeGlassContainer(frame: NSRect, content: NSView) -> NSView {
        content.frame = frame
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView(frame: frame)
            glass.autoresizingMask = [.width, .height]
            glass.contentView = content
            return glass
        } else {
            let vev = NSVisualEffectView(frame: frame)
            vev.autoresizingMask = [.width, .height]
            vev.material = .underWindowBackground
            vev.blendingMode = .behindWindow
            vev.state = .active
            vev.addSubview(content)
            return vev
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
