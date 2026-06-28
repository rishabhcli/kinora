import Foundation

/// Where the shell loads the React renderer from.
///
/// Two sources, in priority order:
///   1. **Dev server** — `http://localhost:5173` (Vite, started by `make app-desktop-dev`).
///      This is the live-development path; the renderer hot-reloads.
///   2. **Bundled build** — a `dist/index.html` copied into the `.app` (file://), so the
///      shipped shell works with no dev server.
///
/// If neither is reachable the shell falls back to its self-contained **showcase** UI, so
/// the window is never blank. The chosen URL can be overridden by the
/// `KINORA_RENDERER_URL` environment variable (used by `make app-native` for custom ports
/// and by tests).
public enum RendererEndpoint: Equatable, Sendable {
    case devServer(URL)
    case bundled(URL)
    case showcase

    /// The default dev-server origin (matches `apps/desktop/vite.config.ts` `server.port`).
    public static let defaultDevURL = URL(string: "http://localhost:5173")!

    /// The environment variable a caller can set to override the renderer origin.
    public static let overrideEnvKey = "KINORA_RENDERER_URL"

    /// The URL to load, if this endpoint loads a web page at all.
    public var url: URL? {
        switch self {
        case .devServer(let u), .bundled(let u): return u
        case .showcase: return nil
        }
    }

    /// True for the two web-backed endpoints; false for the native showcase.
    public var isWeb: Bool { url != nil }

    /// Resolve the *preferred* endpoint from the environment and a bundled-build lookup,
    /// **without** doing any network I/O. Reachability is probed separately (async) so this
    /// stays pure and unit-testable.
    ///
    /// - Parameters:
    ///   - environment: the process environment (injected for tests).
    ///   - bundledIndexURL: the `file://…/index.html` of a bundled build, if one exists.
    /// - Returns: dev-server if an override/default origin is configured, else bundled,
    ///   else showcase.
    public static func resolve(
        environment: [String: String],
        bundledIndexURL: URL?
    ) -> RendererEndpoint {
        if let raw = environment[overrideEnvKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !raw.isEmpty {
            if raw.lowercased() == "showcase" { return .showcase }
            if let u = URL(string: raw), u.scheme != nil {
                return u.isFileURL ? .bundled(u) : .devServer(u)
            }
        }
        if let bundled = bundledIndexURL {
            return .bundled(bundled)
        }
        return .devServer(defaultDevURL)
    }

    /// A short human label for logs / the connection overlay.
    public var label: String {
        switch self {
        case .devServer(let u): return "dev server \(u.absoluteString)"
        case .bundled: return "bundled build"
        case .showcase: return "showcase (offline)"
        }
    }
}
