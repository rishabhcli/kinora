import Foundation
#if canImport(Security)
import Security
#endif

/// Persistence for the renderer's bearer token, native side.
///
/// The renderer's own source of truth is `localStorage["kinora.token"]` (`api.ts`).
/// We mirror it into the macOS **Keychain** so the token survives a localStorage
/// clear and is available to native features (e.g. a future "open last book"
/// background fetch) without the web layer mounting. The bridge is non-invasive:
/// it never pushes a Keychain token *into* the page; the page asks via `getToken`
/// and decides.
///
/// `KeychainBackend` is injected so the store is fully unit-testable with an
/// in-memory backend (no entitlement / no real Keychain access in CI).
public protocol KeychainBackend: AnyObject, Sendable {
    func read(account: String) -> String?
    func write(_ value: String, account: String)
    func delete(account: String)
}

public final class TokenStore: @unchecked Sendable {
    private let backend: KeychainBackend
    private let account: String

    public init(backend: KeychainBackend, account: String = "kinora.bearer") {
        self.backend = backend
        self.account = account
    }

    public var token: String? { backend.read(account: account) }

    public func save(_ token: String) {
        guard !token.isEmpty else { delete(); return }
        backend.write(token, account: account)
        Log.auth.debug("token persisted (len redacted)")
    }

    public func delete() {
        backend.delete(account: account)
        Log.auth.debug("token cleared")
    }
}

// MARK: - In-memory backend (tests)

public final class InMemoryKeychainBackend: KeychainBackend, @unchecked Sendable {
    private var storage: [String: String] = [:]
    private let lock = NSLock()
    public init() {}
    public func read(account: String) -> String? {
        lock.lock(); defer { lock.unlock() }; return storage[account]
    }
    public func write(_ value: String, account: String) {
        lock.lock(); defer { lock.unlock() }; storage[account] = value
    }
    public func delete(account: String) {
        lock.lock(); defer { lock.unlock() }; storage.removeValue(forKey: account)
    }
}

// MARK: - Real Keychain backend (macOS generic-password items)

#if canImport(Security)
/// Generic-password Keychain backend. Stores under one service so items are easy to
/// audit / wipe. Reads/writes are upsert (delete-then-add) to keep the code simple
/// and avoid `SecItemUpdate` attribute juggling.
public final class SystemKeychainBackend: KeychainBackend, @unchecked Sendable {
    private let service: String
    public init(service: String = "local.kinora.glass") { self.service = service }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    public func read(account: String) -> String? {
        var q = baseQuery(account: account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func write(_ value: String, account: String) {
        delete(account: account)
        var q = baseQuery(account: account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(q as CFDictionary, nil)
        if status != errSecSuccess {
            Log.auth.error("keychain write failed: \(status, privacy: .public)")
        }
    }

    public func delete(account: String) {
        SecItemDelete(baseQuery(account: account) as CFDictionary)
    }
}
#endif
