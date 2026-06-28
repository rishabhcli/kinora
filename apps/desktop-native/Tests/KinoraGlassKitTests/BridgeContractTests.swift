import XCTest
@testable import KinoraGlassKit

/// Guards the JS shim against drift from the Swift contract: every `MessageName` the
/// native side handles must appear in the generated `window.kinora` shim, and the shim
/// must define the public surface the renderer relies on. These are string-contains
/// checks — cheap, but they catch a renamed handler that would silently no-op.
final class BridgeContractTests: XCTestCase {
    private let shim = BridgeContract.userScriptSource()

    func testShimSetsNativeFlag() {
        XCTAssertTrue(shim.contains("__KINORA_NATIVE__"), "must set the Electron-parity flag")
    }

    func testShimDefinesKinoraObject() {
        XCTAssertTrue(shim.contains("window.kinora"))
    }

    func testShimExposesPublicSurface() {
        for member in ["getToken", "setToken", "clearToken", "openBook", "onOpenBook",
                       "notify", "setBadge", "openExternal", "importFile", "ready",
                       "onImport", "onRoute"] {
            XCTAssertTrue(shim.contains(member + ":"), "shim missing kinora.\(member)")
        }
    }

    func testEveryMessageNameIsPostedBySomePath() {
        // Each declared handler name must be referenced by the shim, else native
        // registers a handler the JS never posts to (dead handler).
        for name in BridgeContract.MessageName.allCases {
            XCTAssertTrue(shim.contains("\"\(name.rawValue)\""),
                          "shim never posts to handler '\(name.rawValue)'")
        }
    }

    func testShimReferencesNativeDispatchHooks() {
        XCTAssertTrue(shim.contains("__kinoraDispatchOpenBook"))
        XCTAssertTrue(shim.contains("__kinoraDispatchImport"))
        XCTAssertTrue(shim.contains("__kinoraDispatchRoute"))
        XCTAssertEqual(BridgeContract.openBookJSFunction, "window.__kinoraDispatchOpenBook")
        XCTAssertEqual(BridgeContract.importJSFunction, "window.__kinoraDispatchImport")
        XCTAssertEqual(BridgeContract.routeJSFunction, "window.__kinoraDispatchRoute")
    }

    func testShimUsesWebTokenKey() {
        XCTAssertTrue(shim.contains(BridgeContract.webTokenKey))
        XCTAssertEqual(BridgeContract.webTokenKey, "kinora.token", "must match apps/desktop/src/lib/api.ts")
    }

    func testShimIsIdempotentGuarded() {
        XCTAssertTrue(shim.contains("__kinoraBridgeInstalled"), "must guard against double-injection")
    }

    func testVersionInterpolated() {
        let custom = BridgeContract.userScriptSource(version: "9.9.9")
        XCTAssertTrue(custom.contains("9.9.9"))
    }

    func testPlatformConstant() {
        XCTAssertEqual(BridgeContract.platform, "macos-native")
        XCTAssertTrue(shim.contains("macos-native"))
    }
}
