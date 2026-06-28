import XCTest
@testable import KinoraGlassKit

final class RendererEndpointTests: XCTestCase {
    func testDefaultsToDevServerWhenNothingElse() {
        let e = RendererEndpoint.resolve(environment: [:], bundledIndexURL: nil)
        XCTAssertEqual(e, .devServer(RendererEndpoint.defaultDevURL))
    }

    func testPrefersBundledOverDevWhenNoOverride() {
        let bundled = URL(fileURLWithPath: "/Apps/Kinora.app/Contents/Resources/dist/index.html")
        let e = RendererEndpoint.resolve(environment: [:], bundledIndexURL: bundled)
        XCTAssertEqual(e, .bundled(bundled))
    }

    func testEnvOverrideHTTP() {
        let e = RendererEndpoint.resolve(
            environment: ["KINORA_RENDERER_URL": "http://localhost:4000"],
            bundledIndexURL: URL(fileURLWithPath: "/x/index.html")
        )
        XCTAssertEqual(e, .devServer(URL(string: "http://localhost:4000")!))
    }

    func testEnvOverrideFileBecomesBundled() {
        let e = RendererEndpoint.resolve(
            environment: ["KINORA_RENDERER_URL": "file:///custom/index.html"],
            bundledIndexURL: nil
        )
        XCTAssertEqual(e, .bundled(URL(string: "file:///custom/index.html")!))
    }

    func testEnvOverrideShowcaseKeyword() {
        let e = RendererEndpoint.resolve(
            environment: ["KINORA_RENDERER_URL": "showcase"],
            bundledIndexURL: URL(fileURLWithPath: "/x/index.html")
        )
        XCTAssertEqual(e, .showcase)
    }

    func testBlankOverrideIgnored() {
        let e = RendererEndpoint.resolve(environment: ["KINORA_RENDERER_URL": "   "], bundledIndexURL: nil)
        XCTAssertEqual(e, .devServer(RendererEndpoint.defaultDevURL))
    }

    func testIsWeb() {
        XCTAssertTrue(RendererEndpoint.devServer(RendererEndpoint.defaultDevURL).isWeb)
        XCTAssertTrue(RendererEndpoint.bundled(URL(fileURLWithPath: "/x")).isWeb)
        XCTAssertFalse(RendererEndpoint.showcase.isWeb)
    }

    func testDefaultDevPortMatchesViteConfig() {
        // apps/desktop/vite.config.ts pins server.port = 5173.
        XCTAssertEqual(RendererEndpoint.defaultDevURL.port, 5173)
    }
}
