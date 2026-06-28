import XCTest
@testable import KinoraGlassKit

final class WindowStateTests: XCTestCase {
    func testRoundTrip() {
        let s = WindowState(
            id: "w1",
            bookID: "frog",
            mode: .director,
            immersive: true,
            frame: .init(x: 10, y: 20, width: 1200, height: 800)
        )
        let data = s.encoded()
        XCTAssertNotNil(data)
        XCTAssertEqual(WindowState.decoded(from: data!), s)
    }

    func testRegistryUpsertReplaces() {
        var reg = WindowStateRegistry()
        reg.upsert(WindowState(id: "w1", bookID: "a"))
        reg.upsert(WindowState(id: "w1", bookID: "b"))
        XCTAssertEqual(reg.windows.count, 1)
        XCTAssertEqual(reg.windows.first?.bookID, "b")
    }

    func testRegistryUpsertAppends() {
        var reg = WindowStateRegistry()
        reg.upsert(WindowState(id: "w1"))
        reg.upsert(WindowState(id: "w2"))
        XCTAssertEqual(reg.windows.count, 2)
    }

    func testRegistryRemove() {
        var reg = WindowStateRegistry()
        reg.upsert(WindowState(id: "w1"))
        reg.upsert(WindowState(id: "w2"))
        reg.remove(id: "w1")
        XCTAssertEqual(reg.windows.map(\.id), ["w2"])
    }

    func testRegistryRoundTrip() {
        var reg = WindowStateRegistry()
        reg.upsert(WindowState(id: "w1", bookID: "frog", mode: .viewer))
        reg.upsert(WindowState(id: "w2", mode: .shelf))
        let data = reg.encoded()!
        XCTAssertEqual(WindowStateRegistry.decoded(from: data), reg)
    }
}

final class ShellSettingsTests: XCTestCase {
    func testDefaults() {
        let store = ShellSettingsStore(store: InMemoryKeyValueStore())
        let s = store.load()
        XCTAssertEqual(s.glassIntensity, .regular)
        XCTAssertTrue(s.reopenLastBook)
        XCTAssertNil(s.lastBookID)
    }

    func testSaveLoadRoundTrip() {
        let kv = InMemoryKeyValueStore()
        let store = ShellSettingsStore(store: kv)
        var s = ShellSettings()
        s.glassIntensity = .prominent
        s.reopenLastBook = false
        s.showActivityStrip = false
        s.lastBookID = "dune"
        store.save(s)

        let loaded = ShellSettingsStore(store: kv).load()
        XCTAssertEqual(loaded, s)
    }

    func testPartialSeedFallsBackToDefaults() {
        let kv = InMemoryKeyValueStore(seed: ["kinora.shell.glassIntensity": "clear"])
        let s = ShellSettingsStore(store: kv).load()
        XCTAssertEqual(s.glassIntensity, .clear)
        XCTAssertTrue(s.reopenLastBook) // default preserved
    }
}
