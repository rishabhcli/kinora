import Foundation

/// The book model shared by both faces of the app — the self-contained showcase
/// (bundled demo films) and the WKWebView shell (where a book is just an id we
/// deep-link the renderer to). Deliberately small: the native shell is *not* a
/// backend client, so it carries only what it needs to render a shelf offline
/// and to address a book by id for `openBook`.
public struct Book: Identifiable, Hashable, Sendable, Codable {
    public let id: String
    public let title: String
    public let author: String
    /// ISBN used to resolve an Open Library cover thumbnail.
    public let isbn: String
    /// Bundled demo-film resource base name (e.g. `film-01`), showcase-only.
    public let film: String

    public init(id: String, title: String, author: String, isbn: String, film: String) {
        self.id = id
        self.title = title
        self.author = author
        self.isbn = isbn
        self.film = film
    }

    /// Open Library cover (large). `nil` only if the ISBN is malformed.
    public var coverURL: URL? {
        URL(string: "https://covers.openlibrary.org/b/isbn/\(isbn)-L.jpg")
    }

    /// The `kinora://book/<id>` deep link that addresses this book in the renderer.
    public var deepLink: URL? {
        DeepLink.openBook(id: id).url
    }
}

extension Book {
    /// The bundled demo catalogue. Kept here (in the kit) so both the showcase UI
    /// and tests can reference the same canonical list.
    public static let demoCatalogue: [Book] = [
        .init(id: "frog", title: "The Frog-King", author: "Brothers Grimm", isbn: "9780525559474", film: "film-01"),
        .init(id: "alice", title: "Alice in Wonderland", author: "Lewis Carroll", isbn: "9780553213454", film: "film-02"),
        .init(id: "pride", title: "Pride and Prejudice", author: "Jane Austen", isbn: "9780141439518", film: "film-03"),
        .init(id: "gatsby", title: "The Great Gatsby", author: "F. Scott Fitzgerald", isbn: "9780743273565", film: "film-04"),
        .init(id: "atomic", title: "Atomic Habits", author: "James Clear", isbn: "9780735211292", film: "film-02"),
        .init(id: "sapiens", title: "Sapiens", author: "Yuval Noah Harari", isbn: "9780062316097", film: "film-03"),
        .init(id: "dune", title: "Dune", author: "Frank Herbert", isbn: "9780441172719", film: "film-04"),
    ]

    /// Look a demo book up by id (used by deep-link resolution + tests).
    public static func demo(id: String) -> Book? {
        demoCatalogue.first { $0.id == id }
    }
}
