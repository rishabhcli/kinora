import Foundation

/// Per-window restorable state, persisted across launches (NSWindow state restoration
/// + our own snapshot). Multi-window: each window restores the book it had open, its
/// reading mode, and frame. `Codable` so it round-trips through `UserDefaults`/a JSON
/// blob in the restoration handler.
public struct WindowState: Codable, Equatable, Sendable {
    /// The reading surface mode (mirrors the renderer's Viewer/Director switch, §5.2).
    public enum Mode: String, Codable, Sendable { case shelf, viewer, director }

    /// Stable identifier for the window (used to key restoration + the recents menu).
    public var id: String
    /// The book open in this window, if any.
    public var bookID: String?
    /// Viewer vs Director (vs the shelf).
    public var mode: Mode
    /// Whether the window was in the immersive full-screen reading mode.
    public var immersive: Bool
    /// Last frame, in screen points (x, y, w, h). Optional so a fresh window centres.
    public var frame: Frame?

    public struct Frame: Codable, Equatable, Sendable {
        public var x, y, width, height: Double
        public init(x: Double, y: Double, width: Double, height: Double) {
            self.x = x; self.y = y; self.width = width; self.height = height
        }
    }

    public init(
        id: String = UUID().uuidString,
        bookID: String? = nil,
        mode: Mode = .shelf,
        immersive: Bool = false,
        frame: Frame? = nil
    ) {
        self.id = id
        self.bookID = bookID
        self.mode = mode
        self.immersive = immersive
        self.frame = frame
    }

    // MARK: - Codable round-trip helpers (used by restoration handlers + tests)

    public func encoded() -> Data? {
        try? JSONEncoder().encode(self)
    }

    public static func decoded(from data: Data) -> WindowState? {
        try? JSONDecoder().decode(WindowState.self, from: data)
    }
}

/// A small registry of open windows' states, persisted as a whole so the app can
/// restore *every* window on relaunch (true multi-window restoration). Pure + testable.
public struct WindowStateRegistry: Codable, Equatable, Sendable {
    public private(set) var windows: [WindowState]

    public init(windows: [WindowState] = []) { self.windows = windows }

    public mutating func upsert(_ state: WindowState) {
        if let i = windows.firstIndex(where: { $0.id == state.id }) {
            windows[i] = state
        } else {
            windows.append(state)
        }
    }

    public mutating func remove(id: String) {
        windows.removeAll { $0.id == id }
    }

    public func encoded() -> Data? { try? JSONEncoder().encode(self) }
    public static func decoded(from data: Data) -> WindowStateRegistry? {
        try? JSONDecoder().decode(WindowStateRegistry.self, from: data)
    }
}
