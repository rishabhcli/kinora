import Foundation
import os

/// Centralised `os.Logger` categories so every subsystem logs under the same
/// subsystem and shows up coherently in Console.app / `log stream`.
///
/// Usage: `Log.bridge.debug("…")`, `Log.shell.error("…")`. Keeping the channels
/// here (rather than scattering `Logger(subsystem:category:)` literals) means a
/// single place to retune levels and one consistent subsystem string.
public enum Log {
    /// The reverse-DNS subsystem string shared by every Kinora-native log.
    public static let subsystem = "local.kinora.glass"

    /// JS <-> native bridge marshalling.
    public static let bridge = Logger(subsystem: subsystem, category: "bridge")
    /// WKWebView host + navigation lifecycle.
    public static let shell = Logger(subsystem: subsystem, category: "shell")
    /// App lifecycle, URL events, notifications, dock.
    public static let app = Logger(subsystem: subsystem, category: "app")
    /// Window / scene management + state restoration.
    public static let window = Logger(subsystem: subsystem, category: "window")
    /// Token persistence (never logs the token itself).
    public static let auth = Logger(subsystem: subsystem, category: "auth")
}
