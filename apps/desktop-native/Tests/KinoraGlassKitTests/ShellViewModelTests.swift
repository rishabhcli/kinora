import XCTest
@testable import KinoraGlassKit

@MainActor
final class ShellViewModelTests: XCTestCase {
    func testIdleAtStart() {
        let vm = ShellViewModel()
        XCTAssertEqual(vm.phase, .idle)
    }

    func testBeginDevServerEntersConnecting() {
        let vm = ShellViewModel()
        let e = RendererEndpoint.devServer(RendererEndpoint.defaultDevURL)
        vm.begin(endpoint: e)
        XCTAssertEqual(vm.phase, .connecting(e))
        XCTAssertTrue(vm.isConnecting)
    }

    func testBeginShowcaseEntersFallback() {
        let vm = ShellViewModel()
        vm.begin(endpoint: .showcase)
        XCTAssertTrue(vm.showsShowcase)
    }

    func testMarkLiveTransitions() {
        let vm = ShellViewModel()
        let e = RendererEndpoint.devServer(RendererEndpoint.defaultDevURL)
        vm.begin(endpoint: e)
        vm.markLive()
        XCTAssertEqual(vm.phase, .live(e))
    }

    func testConnectFailureRetriesThenFallsBack() {
        let vm = ShellViewModel(maxConnectAttempts: 3)
        vm.begin(endpoint: .devServer(RendererEndpoint.defaultDevURL))
        XCTAssertTrue(vm.markConnectFailure("timeout"))  // attempt 1, retry
        XCTAssertTrue(vm.markConnectFailure("timeout"))  // attempt 2, retry
        XCTAssertFalse(vm.markConnectFailure("timeout")) // attempt 3, fall back
        XCTAssertTrue(vm.showsShowcase)
    }

    func testForceFallback() {
        let vm = ShellViewModel()
        vm.begin(endpoint: .devServer(RendererEndpoint.defaultDevURL))
        vm.forceFallback()
        XCTAssertTrue(vm.showsShowcase)
    }

    func testDeepLinkQueuedBeforeReady() {
        let vm = ShellViewModel()
        vm.begin(endpoint: .devServer(RendererEndpoint.defaultDevURL))
        let dispatched = vm.enqueue(.openBook(id: "frog"))
        XCTAssertNil(dispatched, "should queue while not ready")
        XCTAssertEqual(vm.pendingDeepLinks, [.openBook(id: "frog")])
        XCTAssertEqual(vm.lastOpenedBookID, "frog")
    }

    func testDeepLinkDispatchedWhenLiveAndReady() {
        let vm = ShellViewModel()
        let e = RendererEndpoint.devServer(RendererEndpoint.defaultDevURL)
        vm.begin(endpoint: e)
        vm.markLive()
        vm.markReady()
        let dispatched = vm.enqueue(.openBook(id: "alice"))
        XCTAssertEqual(dispatched, .openBook(id: "alice"))
        XCTAssertTrue(vm.pendingDeepLinks.isEmpty)
    }

    func testMarkReadyFlushesQueue() {
        let vm = ShellViewModel()
        let e = RendererEndpoint.devServer(RendererEndpoint.defaultDevURL)
        vm.begin(endpoint: e)
        vm.markLive()
        _ = vm.enqueue(.openBook(id: "frog"))
        _ = vm.enqueue(.route("/library"))
        let flushed = vm.markReady()
        XCTAssertEqual(flushed, [.openBook(id: "frog"), .route("/library")])
        XCTAssertTrue(vm.rendererReady)
        XCTAssertTrue(vm.pendingDeepLinks.isEmpty)
    }

    func testQueuedWhenReadyButNotLive() {
        // ready can fire on a page that has not been marked live yet — still queue.
        let vm = ShellViewModel()
        vm.begin(endpoint: .devServer(RendererEndpoint.defaultDevURL))
        vm.markReady()
        let dispatched = vm.enqueue(.openBook(id: "x"))
        XCTAssertNil(dispatched)
    }
}
