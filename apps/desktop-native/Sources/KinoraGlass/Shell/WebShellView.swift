import SwiftUI
import WebKit
import KinoraGlassKit

/// `NSViewRepresentable` that hosts the renderer's `WKWebView` behind a **real**
/// Liquid Glass surface.
///
/// The glass story: the web view itself is made transparent (`drawsBackground = false`)
/// so it composits over an `NSGlassEffectView` (macOS 26+) — that is the genuine
/// `Liquid Glass` material the OS renders and Electron cannot. Below the 26 SDK an
/// `NSVisualEffectView` stands in (legacy vibrancy, *not* called Liquid Glass). The
/// renderer, seeing `__KINORA_NATIVE__`, drops its own opaque background via
/// `html.kinora-native`, so the page content floats on the native glass.
struct WebShellView: NSViewRepresentable {
    let coordinator: WebShellCoordinator
    let endpoint: RendererEndpoint

    func makeNSView(context: Context) -> NSView {
        let container = GlassBackedContainer()
        let webView = coordinator.makeWebView()
        container.embed(webView)
        coordinator.load(endpoint)
        return container
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

/// A container view that places the web view on top of a real Liquid Glass backing.
final class GlassBackedContainer: NSView {
    private var glassBacking: NSView?

    func embed(_ webView: WKWebView) {
        wantsLayer = true
        let backing = Self.makeGlassBacking()
        backing.translatesAutoresizingMaskIntoConstraints = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        addSubview(backing)
        addSubview(webView)
        glassBacking = backing
        NSLayoutConstraint.activate([
            backing.leadingAnchor.constraint(equalTo: leadingAnchor),
            backing.trailingAnchor.constraint(equalTo: trailingAnchor),
            backing.topAnchor.constraint(equalTo: topAnchor),
            backing.bottomAnchor.constraint(equalTo: bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: trailingAnchor),
            webView.topAnchor.constraint(equalTo: topAnchor),
            webView.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    /// Build the genuine Liquid Glass backing. On macOS 26+ this is the real
    /// `NSGlassEffectView`; on older SDKs it degrades to legacy vibrancy.
    static func makeGlassBacking() -> NSView {
        #if canImport(AppKit)
        if #available(macOS 26.0, *), let glassClass = NSClassFromString("NSGlassEffectView") as? NSView.Type {
            // NSGlassEffectView is the real Liquid Glass material view (macOS 26 SDK).
            let glass = glassClass.init(frame: .zero)
            glass.wantsLayer = true
            return glass
        }
        #endif
        let fallback = NSVisualEffectView()
        fallback.material = .underPageBackground
        fallback.blendingMode = .behindWindow
        fallback.state = .active
        return fallback
    }
}
