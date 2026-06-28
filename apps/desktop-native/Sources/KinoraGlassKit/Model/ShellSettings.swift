import Foundation

/// Persisted shell preferences. These are *shell* concerns (window chrome, the
/// glass material intensity, whether to auto-open the last book) — distinct from the
/// renderer's own in-page settings, which live in the web app's localStorage.
///
/// Backed by an injectable `KeyValueStore` so the type is unit-testable with an
/// in-memory store and production-backed by `UserDefaults`.
public struct ShellSettings: Equatable, Sendable {
    /// Liquid-glass material intensity preference for the native chrome.
    public enum GlassIntensity: String, CaseIterable, Sendable, Codable {
        case regular, clear, prominent
    }

    public var glassIntensity: GlassIntensity
    /// Reopen the last-read book automatically on launch.
    public var reopenLastBook: Bool
    /// Show the live crew-activity strip in the native chrome.
    public var showActivityStrip: Bool
    /// Honour the system Reduce Motion setting for shell transitions.
    public var respectReduceMotion: Bool
    /// The last book id opened (for `reopenLastBook` + the Dock/recents menu).
    public var lastBookID: String?

    public init(
        glassIntensity: GlassIntensity = .regular,
        reopenLastBook: Bool = true,
        showActivityStrip: Bool = true,
        respectReduceMotion: Bool = true,
        lastBookID: String? = nil
    ) {
        self.glassIntensity = glassIntensity
        self.reopenLastBook = reopenLastBook
        self.showActivityStrip = showActivityStrip
        self.respectReduceMotion = respectReduceMotion
        self.lastBookID = lastBookID
    }
}

// MARK: - Persistence

/// Minimal key/value abstraction so settings persistence is testable without
/// touching the real user-defaults database.
public protocol KeyValueStore: AnyObject {
    func string(forKey key: String) -> String?
    func bool(forKey key: String) -> Bool
    func object(forKey key: String) -> Any?
    func set(_ value: Any?, forKey key: String)
}

/// A thread-confined in-memory store for tests.
public final class InMemoryKeyValueStore: KeyValueStore, @unchecked Sendable {
    private var storage: [String: Any] = [:]
    private let lock = NSLock()

    public init(seed: [String: Any] = [:]) { storage = seed }

    public func string(forKey key: String) -> String? {
        lock.lock(); defer { lock.unlock() }
        return storage[key] as? String
    }
    public func bool(forKey key: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return (storage[key] as? Bool) ?? false
    }
    public func object(forKey key: String) -> Any? {
        lock.lock(); defer { lock.unlock() }
        return storage[key]
    }
    public func set(_ value: Any?, forKey key: String) {
        lock.lock(); defer { lock.unlock() }
        if let value { storage[key] = value } else { storage.removeValue(forKey: key) }
    }
}

/// Loads/saves `ShellSettings` against a `KeyValueStore`. The key prefix keeps
/// the shell's keys from colliding with anything else in the defaults domain.
public struct ShellSettingsStore {
    private let prefix = "kinora.shell."
    private let store: KeyValueStore

    public init(store: KeyValueStore) { self.store = store }

    private func key(_ suffix: String) -> String { prefix + suffix }

    public func load() -> ShellSettings {
        var s = ShellSettings()
        if let raw = store.string(forKey: key("glassIntensity")),
           let v = ShellSettings.GlassIntensity(rawValue: raw) {
            s.glassIntensity = v
        }
        if store.object(forKey: key("reopenLastBook")) != nil {
            s.reopenLastBook = store.bool(forKey: key("reopenLastBook"))
        }
        if store.object(forKey: key("showActivityStrip")) != nil {
            s.showActivityStrip = store.bool(forKey: key("showActivityStrip"))
        }
        if store.object(forKey: key("respectReduceMotion")) != nil {
            s.respectReduceMotion = store.bool(forKey: key("respectReduceMotion"))
        }
        s.lastBookID = store.string(forKey: key("lastBookID"))
        return s
    }

    public func save(_ s: ShellSettings) {
        store.set(s.glassIntensity.rawValue, forKey: key("glassIntensity"))
        store.set(s.reopenLastBook, forKey: key("reopenLastBook"))
        store.set(s.showActivityStrip, forKey: key("showActivityStrip"))
        store.set(s.respectReduceMotion, forKey: key("respectReduceMotion"))
        store.set(s.lastBookID, forKey: key("lastBookID"))
    }
}

#if canImport(Foundation)
extension UserDefaults: KeyValueStore {}
#endif
