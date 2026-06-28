import Foundation

/// The shell's custom URL scheme. `kinora://…` links arrive from three places:
///   • the OS, when another app / the browser opens a `kinora://` link;
///   • "Open With Kinora" / file-drop, which we re-express as an `import` intent;
///   • internally, from a book's `deepLink` (so one code path addresses books).
///
/// Parsing is pure and exhaustively unit-tested; the AppKit layer only turns an
/// `NSAppleEventDescriptor` / dropped file URL into a `URL` and hands it here.
public enum DeepLink: Equatable, Sendable {
    /// Open a book by its renderer id: `kinora://book/<id>` or `kinora://open?book=<id>`.
    case openBook(id: String)
    /// Import a local file (PDF/EPUB): `kinora://import?path=<percent-encoded-path>`.
    case importFile(URL)
    /// Navigate the renderer to an arbitrary in-app route: `kinora://route?path=/library`.
    case route(String)
    /// Bring the library/home to front: `kinora://home`.
    case home

    /// The canonical scheme string.
    public static let scheme = "kinora"

    /// The `URL` representation of this link (round-trips with `parse`).
    public var url: URL? {
        var c = URLComponents()
        c.scheme = Self.scheme
        switch self {
        case .openBook(let id):
            c.host = "book"
            c.path = "/" + id
        case .importFile(let fileURL):
            c.host = "import"
            c.queryItems = [URLQueryItem(name: "path", value: fileURL.path)]
        case .route(let path):
            c.host = "route"
            c.queryItems = [URLQueryItem(name: "path", value: path)]
        case .home:
            c.host = "home"
        }
        return c.url
    }

    /// Parse a `kinora://` URL into a typed intent. Returns `nil` for a foreign
    /// scheme or an unrecognised host so the caller can ignore it safely.
    public static func parse(_ url: URL) -> DeepLink? {
        guard let scheme = url.scheme?.lowercased(), scheme == Self.scheme else { return nil }
        let comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let host = (url.host ?? comps?.host)?.lowercased()
        let query = comps?.queryItems ?? []
        func q(_ name: String) -> String? {
            query.first { $0.name == name }?.value
        }

        switch host {
        case "book":
            // kinora://book/<id>  (id is the first non-empty path segment)
            let id = url.pathComponents.first { $0 != "/" && !$0.isEmpty }
            if let id, !id.isEmpty { return .openBook(id: id) }
            return nil
        case "open":
            // kinora://open?book=<id>
            if let id = q("book")?.trimmingCharacters(in: .whitespacesAndNewlines), !id.isEmpty {
                return .openBook(id: id)
            }
            return nil
        case "import":
            // kinora://import?path=<file path>  or  kinora://import?url=file://…
            if let raw = q("path"), !raw.isEmpty {
                return .importFile(URL(fileURLWithPath: raw))
            }
            if let raw = q("url"), let u = URL(string: raw) {
                return .importFile(u)
            }
            return nil
        case "route":
            if let path = q("path"), !path.isEmpty { return .route(path) }
            return nil
        case "home":
            return .home
        default:
            return nil
        }
    }

    /// A `file://` or local-path URL → an import intent, used by file-drop / Open-With.
    public static func from(droppedFile url: URL) -> DeepLink? {
        guard url.isFileURL else { return nil }
        let ext = url.pathExtension.lowercased()
        guard ["pdf", "epub"].contains(ext) else { return nil }
        return .importFile(url)
    }
}
