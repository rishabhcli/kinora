import XCTest
@testable import KinoraGlassKit

final class TokenStoreTests: XCTestCase {
    func testSaveAndRead() {
        let store = TokenStore(backend: InMemoryKeychainBackend())
        XCTAssertNil(store.token)
        store.save("jwt-123")
        XCTAssertEqual(store.token, "jwt-123")
    }

    func testOverwrite() {
        let store = TokenStore(backend: InMemoryKeychainBackend())
        store.save("a")
        store.save("b")
        XCTAssertEqual(store.token, "b")
    }

    func testDelete() {
        let store = TokenStore(backend: InMemoryKeychainBackend())
        store.save("x")
        store.delete()
        XCTAssertNil(store.token)
    }

    func testSavingEmptyClears() {
        let store = TokenStore(backend: InMemoryKeychainBackend())
        store.save("x")
        store.save("")
        XCTAssertNil(store.token)
    }

    func testIsolatedByAccount() {
        let backend = InMemoryKeychainBackend()
        let a = TokenStore(backend: backend, account: "a")
        let b = TokenStore(backend: backend, account: "b")
        a.save("tokenA")
        XCTAssertNil(b.token)
        XCTAssertEqual(a.token, "tokenA")
    }
}
