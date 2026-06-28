import XCTest
@testable import KinoraGlassKit

final class BridgeMessageTests: XCTestCase {
    func testDecodeSetToken() throws {
        let m = try BridgeMessage.decode(name: "setToken", body: ["token": "abc.def.ghi"])
        XCTAssertEqual(m, .setToken("abc.def.ghi"))
    }

    func testDecodeSetTokenMissingFieldThrows() {
        XCTAssertThrowsError(try BridgeMessage.decode(name: "setToken", body: [:])) { err in
            XCTAssertEqual(err as? BridgeMessage.DecodeError, .missingField("token"))
        }
    }

    func testDecodeSetTokenEmptyStringThrows() {
        XCTAssertThrowsError(try BridgeMessage.decode(name: "setToken", body: ["token": ""]))
    }

    func testDecodeClearToken() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "clearToken", body: nil), .clearToken)
    }

    func testDecodeGetToken() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "getToken", body: [:]), .getToken)
    }

    func testDecodeNotifyDefaults() throws {
        let m = try BridgeMessage.decode(name: "notify", body: [:])
        XCTAssertEqual(m, .notify(title: "Kinora", body: "", id: nil))
    }

    func testDecodeNotifyFull() throws {
        let m = try BridgeMessage.decode(
            name: "notify",
            body: ["title": "Ready", "body": "Your film is generating", "id": "shot-9"]
        )
        XCTAssertEqual(m, .notify(title: "Ready", body: "Your film is generating", id: "shot-9"))
    }

    func testDecodeBadgeFromInt() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "setBadge", body: ["count": 3]), .setBadge(3))
    }

    func testDecodeBadgeFromDouble() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "setBadge", body: ["count": 4.0]), .setBadge(4))
    }

    func testDecodeBadgeClampsNegative() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "setBadge", body: ["count": -2]), .setBadge(0))
    }

    func testDecodeOpenExternalHTTPS() throws {
        let m = try BridgeMessage.decode(name: "openExternal", body: ["url": "https://kinora.local/help"])
        XCTAssertEqual(m, .openExternal(URL(string: "https://kinora.local/help")!))
    }

    func testDecodeOpenExternalRejectsFileScheme() {
        XCTAssertThrowsError(try BridgeMessage.decode(name: "openExternal", body: ["url": "file:///etc/passwd"])) { err in
            XCTAssertEqual(err as? BridgeMessage.DecodeError, .badField("url"))
        }
    }

    func testDecodeOpenExternalRejectsJavascriptScheme() {
        XCTAssertThrowsError(try BridgeMessage.decode(name: "openExternal", body: ["url": "javascript:alert(1)"]))
    }

    func testDecodeReady() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "ready", body: nil), .ready)
    }

    func testDecodeLogLevels() throws {
        let warn = try BridgeMessage.decode(name: "log", body: ["level": "warn", "message": "hmm"])
        XCTAssertEqual(warn, .log(level: .warn, message: "hmm"))
        let dflt = try BridgeMessage.decode(name: "log", body: ["message": "x"])
        XCTAssertEqual(dflt, .log(level: .log, message: "x"))
    }

    func testDecodeImportFile() throws {
        XCTAssertEqual(try BridgeMessage.decode(name: "importFile", body: [:]), .importFile)
    }

    func testDecodeUnknownNameThrows() {
        XCTAssertThrowsError(try BridgeMessage.decode(name: "doSomethingEvil", body: [:])) { err in
            XCTAssertEqual(err as? BridgeMessage.DecodeError, .unknownName("doSomethingEvil"))
        }
    }

    func testAllContractNamesDecode() throws {
        // Every declared message name must be decodable with a minimal body — proves the
        // switch in `decode` stays exhaustive against the contract enum.
        for name in BridgeContract.MessageName.allCases {
            let body: [String: Any]
            switch name {
            case .setToken: body = ["token": "t"]
            case .openExternal: body = ["url": "https://x.dev"]
            default: body = [:]
            }
            XCTAssertNoThrow(try BridgeMessage.decode(name: name.rawValue, body: body), "name=\(name.rawValue)")
        }
    }
}
