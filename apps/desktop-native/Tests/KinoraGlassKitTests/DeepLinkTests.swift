import XCTest
@testable import KinoraGlassKit

final class DeepLinkTests: XCTestCase {
    func testParseBookPathForm() {
        let url = URL(string: "kinora://book/frog")!
        XCTAssertEqual(DeepLink.parse(url), .openBook(id: "frog"))
    }

    func testParseBookWithDashesAndDigits() {
        let url = URL(string: "kinora://book/the-great-gatsby-1925")!
        XCTAssertEqual(DeepLink.parse(url), .openBook(id: "the-great-gatsby-1925"))
    }

    func testParseOpenQueryForm() {
        let url = URL(string: "kinora://open?book=alice")!
        XCTAssertEqual(DeepLink.parse(url), .openBook(id: "alice"))
    }

    func testParseImportPath() {
        let url = URL(string: "kinora://import?path=/Users/x/Documents/book.pdf")!
        guard case .importFile(let fileURL)? = DeepLink.parse(url) else {
            return XCTFail("expected importFile")
        }
        XCTAssertEqual(fileURL.path, "/Users/x/Documents/book.pdf")
    }

    func testParseRoute() {
        let url = URL(string: "kinora://route?path=/library")!
        XCTAssertEqual(DeepLink.parse(url), .route("/library"))
    }

    func testParseHome() {
        XCTAssertEqual(DeepLink.parse(URL(string: "kinora://home")!), .home)
    }

    func testForeignSchemeReturnsNil() {
        XCTAssertNil(DeepLink.parse(URL(string: "https://example.com/book/frog")!))
        XCTAssertNil(DeepLink.parse(URL(string: "file:///tmp/x.pdf")!))
    }

    func testUnknownHostReturnsNil() {
        XCTAssertNil(DeepLink.parse(URL(string: "kinora://wat/123")!))
    }

    func testEmptyBookIdReturnsNil() {
        XCTAssertNil(DeepLink.parse(URL(string: "kinora://book/")!))
        XCTAssertNil(DeepLink.parse(URL(string: "kinora://open?book=")!))
    }

    func testRoundTripBook() {
        let link = DeepLink.openBook(id: "dune")
        let url = link.url
        XCTAssertNotNil(url)
        XCTAssertEqual(DeepLink.parse(url!), link)
    }

    func testRoundTripRoute() {
        let link = DeepLink.route("/settings")
        XCTAssertEqual(DeepLink.parse(link.url!), link)
    }

    func testFromDroppedPDF() {
        let url = URL(fileURLWithPath: "/tmp/My Book.pdf")
        XCTAssertEqual(DeepLink.from(droppedFile: url), .importFile(url))
    }

    func testFromDroppedEPUBCaseInsensitive() {
        let url = URL(fileURLWithPath: "/tmp/story.EPUB")
        XCTAssertEqual(DeepLink.from(droppedFile: url), .importFile(url))
    }

    func testFromDroppedUnsupportedTypeReturnsNil() {
        XCTAssertNil(DeepLink.from(droppedFile: URL(fileURLWithPath: "/tmp/x.txt")))
        XCTAssertNil(DeepLink.from(droppedFile: URL(fileURLWithPath: "/tmp/x.mp4")))
    }

    func testCaseInsensitiveScheme() {
        XCTAssertEqual(DeepLink.parse(URL(string: "KINORA://book/frog")!), .openBook(id: "frog"))
    }

    func testBookDeepLinkConvenience() {
        let book = Book.demo(id: "frog")!
        XCTAssertEqual(DeepLink.parse(book.deepLink!), .openBook(id: "frog"))
    }
}
