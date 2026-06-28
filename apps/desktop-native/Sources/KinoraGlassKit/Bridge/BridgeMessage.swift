import Foundation

/// A decoded inbound message from the JS bridge. The AppKit coordinator receives a
/// raw `WKScriptMessage` (name + `Any` body); it hands the name + body dictionary to
/// `BridgeMessage.decode`, which produces a typed value (or an error). Keeping decode
/// here — pure, no WebKit import — is what makes the bridge unit-testable.
public enum BridgeMessage: Equatable, Sendable {
    case setToken(String)
    case clearToken
    case getToken
    case notify(title: String, body: String, id: String?)
    case setBadge(Int)
    case openExternal(URL)
    case ready
    case log(level: LogLevel, message: String)
    case importFile

    public enum LogLevel: String, Sendable { case log, warn, error }

    /// Errors surfaced when a message body is malformed. The coordinator logs these;
    /// it never trusts page-supplied data without validation.
    public enum DecodeError: Error, Equatable {
        case unknownName(String)
        case missingField(String)
        case badField(String)
    }

    /// Decode a raw bridge message. `body` is whatever `postMessage` sent — for our
    /// shim that is always a JSON-object-shaped dictionary (possibly empty).
    public static func decode(name: String, body: Any?) throws -> BridgeMessage {
        guard let mname = BridgeContract.MessageName(rawValue: name) else {
            throw DecodeError.unknownName(name)
        }
        let dict = (body as? [String: Any]) ?? [:]

        func string(_ key: String) -> String? {
            (dict[key] as? String).flatMap { $0.isEmpty ? nil : $0 }
        }

        switch mname {
        case .setToken:
            guard let t = string("token") else { throw DecodeError.missingField("token") }
            return .setToken(t)
        case .clearToken:
            return .clearToken
        case .getToken:
            return .getToken
        case .notify:
            let title = string("title") ?? "Kinora"
            let bodyText = string("body") ?? ""
            return .notify(title: title, body: bodyText, id: string("id"))
        case .setBadge:
            // JS Number arrives as NSNumber/Double; coerce defensively.
            let count: Int
            if let n = dict["count"] as? Int { count = n }
            else if let d = dict["count"] as? Double { count = Int(d) }
            else if let s = string("count"), let n = Int(s) { count = n }
            else { count = 0 }
            return .setBadge(max(0, count))
        case .openExternal:
            guard let raw = string("url") else { throw DecodeError.missingField("url") }
            guard let url = URL(string: raw), let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https" || scheme == "mailto" else {
                // Refuse to open arbitrary schemes (e.g. file://, javascript:) from the page.
                throw DecodeError.badField("url")
            }
            return .openExternal(url)
        case .ready:
            return .ready
        case .log:
            let level = string("level").flatMap(LogLevel.init(rawValue:)) ?? .log
            return .log(level: level, message: string("message") ?? "")
        case .importFile:
            return .importFile
        }
    }
}

/// Side-effect surface the bridge router drives. The AppKit coordinator supplies a
/// concrete implementation (Keychain, NSUserNotificationCenter, NSWorkspace, dock).
/// The protocol exists so the router's dispatch logic is exercised in tests with a
/// recording spy — no WebKit, no AppKit.
///
/// `@MainActor` because every conforming implementation touches AppKit/WebKit, which
/// must run on the main thread; this lets the `@MainActor` coordinator conform cleanly.
@MainActor
public protocol BridgeHost: AnyObject {
    /// Persist a bearer token (login).
    func persistToken(_ token: String)
    /// Drop the persisted token (logout).
    func clearPersistedToken()
    /// Answer `getToken`: the persisted token (or nil).
    func currentToken() -> String?
    /// Post a native notification.
    func postNotification(title: String, body: String, id: String?)
    /// Set the Dock badge (0 clears it).
    func setDockBadge(_ count: Int)
    /// Open a URL in the default browser.
    func openExternal(_ url: URL)
    /// The renderer mounted and called `ready()`.
    func rendererBecameReady()
    /// Renderer console line bridged to native logging.
    func rendererLog(level: BridgeMessage.LogLevel, message: String)
    /// Present a native open-panel for import; returns the chosen file path or nil.
    func presentImportPanel() -> String?
}

/// Routes a decoded `BridgeMessage` to the `BridgeHost`. Returns a reply value for the
/// reply-style messages (`getToken`, `importFile`); `nil` for fire-and-forget ones.
/// Pure dispatch — the only place that maps the contract onto host side-effects.
public struct BridgeRouter: Sendable {
    public init() {}

    @MainActor
    @discardableResult
    public func route(_ message: BridgeMessage, host: BridgeHost) -> Any? {
        switch message {
        case .setToken(let t):
            host.persistToken(t)
            return nil
        case .clearToken:
            host.clearPersistedToken()
            return nil
        case .getToken:
            return host.currentToken()
        case .notify(let title, let body, let id):
            host.postNotification(title: title, body: body, id: id)
            return nil
        case .setBadge(let n):
            host.setDockBadge(n)
            return nil
        case .openExternal(let url):
            host.openExternal(url)
            return nil
        case .ready:
            host.rendererBecameReady()
            return nil
        case .log(let level, let message):
            host.rendererLog(level: level, message: message)
            return nil
        case .importFile:
            return host.presentImportPanel()
        }
    }
}
