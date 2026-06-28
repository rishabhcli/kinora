import XCTest
@testable import KinoraGlassKit

/// A recording `BridgeHost` so the router's dispatch can be asserted without WebKit/AppKit.
@MainActor
final class SpyBridgeHost: BridgeHost {
    var persisted: [String] = []
    var cleared = 0
    var tokenToReturn: String?
    var notifications: [(String, String, String?)] = []
    var badges: [Int] = []
    var opened: [URL] = []
    var readyCount = 0
    var logs: [(BridgeMessage.LogLevel, String)] = []
    var importPanelResult: String?
    var importPanelPresented = 0

    func persistToken(_ token: String) { persisted.append(token) }
    func clearPersistedToken() { cleared += 1 }
    func currentToken() -> String? { tokenToReturn }
    func postNotification(title: String, body: String, id: String?) { notifications.append((title, body, id)) }
    func setDockBadge(_ count: Int) { badges.append(count) }
    func openExternal(_ url: URL) { opened.append(url) }
    func rendererBecameReady() { readyCount += 1 }
    func rendererLog(level: BridgeMessage.LogLevel, message: String) { logs.append((level, message)) }
    func presentImportPanel() -> String? { importPanelPresented += 1; return importPanelResult }
}

@MainActor
final class BridgeRouterTests: XCTestCase {
    let router = BridgeRouter()

    func testSetTokenPersists() {
        let host = SpyBridgeHost()
        let reply = router.route(.setToken("jwt"), host: host)
        XCTAssertNil(reply)
        XCTAssertEqual(host.persisted, ["jwt"])
    }

    func testClearToken() {
        let host = SpyBridgeHost()
        router.route(.clearToken, host: host)
        XCTAssertEqual(host.cleared, 1)
    }

    func testGetTokenReturnsHostToken() {
        let host = SpyBridgeHost()
        host.tokenToReturn = "stored"
        XCTAssertEqual(router.route(.getToken, host: host) as? String, "stored")
    }

    func testGetTokenNilWhenAbsent() {
        let host = SpyBridgeHost()
        XCTAssertNil(router.route(.getToken, host: host))
    }

    func testNotify() {
        let host = SpyBridgeHost()
        router.route(.notify(title: "T", body: "B", id: "1"), host: host)
        XCTAssertEqual(host.notifications.count, 1)
        XCTAssertEqual(host.notifications[0].0, "T")
        XCTAssertEqual(host.notifications[0].2, "1")
    }

    func testBadge() {
        let host = SpyBridgeHost()
        router.route(.setBadge(7), host: host)
        XCTAssertEqual(host.badges, [7])
    }

    func testOpenExternal() {
        let host = SpyBridgeHost()
        let url = URL(string: "https://kinora.local")!
        router.route(.openExternal(url), host: host)
        XCTAssertEqual(host.opened, [url])
    }

    func testReady() {
        let host = SpyBridgeHost()
        router.route(.ready, host: host)
        XCTAssertEqual(host.readyCount, 1)
    }

    func testLog() {
        let host = SpyBridgeHost()
        router.route(.log(level: .error, message: "boom"), host: host)
        XCTAssertEqual(host.logs.count, 1)
        XCTAssertEqual(host.logs[0].0, .error)
    }

    func testImportFileReturnsPath() {
        let host = SpyBridgeHost()
        host.importPanelResult = "/tmp/x.pdf"
        let reply = router.route(.importFile, host: host)
        XCTAssertEqual(reply as? String, "/tmp/x.pdf")
        XCTAssertEqual(host.importPanelPresented, 1)
    }

    func testImportFileCancelReturnsNil() {
        let host = SpyBridgeHost()
        host.importPanelResult = nil
        XCTAssertNil(router.route(.importFile, host: host))
    }

    /// End-to-end: decode a raw JS payload, then route it.
    func testDecodeThenRouteSetToken() throws {
        let host = SpyBridgeHost()
        let msg = try BridgeMessage.decode(name: "setToken", body: ["token": "e2e"])
        router.route(msg, host: host)
        XCTAssertEqual(host.persisted, ["e2e"])
    }
}
