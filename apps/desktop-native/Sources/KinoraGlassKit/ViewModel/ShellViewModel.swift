import Foundation
import Observation

/// The shell's state machine, as an `@Observable` so SwiftUI views bind to it.
///
/// Lifecycle:
///   `.idle` → `.connecting(endpoint)` → `.live(endpoint)`            (renderer reachable)
///                                     ↘ `.fallback(reason)`           (showcase UI)
///
/// The view model owns the *decision* (which endpoint, when to fall back, the queued
/// deep links) so it can be unit-tested without a WKWebView. The AppKit/SwiftUI layer
/// observes `phase` + `pendingDeepLinks` and performs the actual loads/navigations.
@MainActor
@Observable
public final class ShellViewModel {
    public enum Phase: Equatable, Sendable {
        case idle
        case connecting(RendererEndpoint)
        case live(RendererEndpoint)
        case fallback(reason: String)
    }

    public private(set) var phase: Phase = .idle
    /// The endpoint we resolved to load (nil in pure showcase).
    public private(set) var endpoint: RendererEndpoint = .showcase
    /// Deep links that arrived before the renderer was ready; flushed on `markReady`.
    public private(set) var pendingDeepLinks: [DeepLink] = []
    /// The renderer signalled `ready()`.
    public private(set) var rendererReady = false
    /// Number of failed reachability attempts (drives the retry/backoff messaging).
    public private(set) var connectAttempts = 0

    /// The book the shell most recently asked the renderer to open (drives recents).
    public private(set) var lastOpenedBookID: String?

    private let maxConnectAttempts: Int

    public init(maxConnectAttempts: Int = 3) {
        self.maxConnectAttempts = maxConnectAttempts
    }

    // MARK: - Endpoint lifecycle

    /// Begin connecting to a resolved endpoint. Showcase endpoints short-circuit to
    /// fallback (there is nothing to connect to).
    public func begin(endpoint: RendererEndpoint) {
        self.endpoint = endpoint
        connectAttempts = 0
        rendererReady = false
        switch endpoint {
        case .showcase:
            phase = .fallback(reason: "no renderer configured")
        case .devServer, .bundled:
            phase = .connecting(endpoint)
        }
    }

    /// Reachability succeeded — the page loaded.
    public func markLive() {
        guard endpoint.isWeb else { return }
        phase = .live(endpoint)
    }

    /// A connection attempt failed. After `maxConnectAttempts` we fall back to the
    /// showcase so the window is never blank; before that we stay in `.connecting`
    /// (the caller schedules a retry).
    /// - Returns: `true` if the caller should retry, `false` if we fell back.
    @discardableResult
    public func markConnectFailure(_ reason: String) -> Bool {
        connectAttempts += 1
        if connectAttempts >= maxConnectAttempts {
            phase = .fallback(reason: reason)
            return false
        }
        phase = .connecting(endpoint)
        return true
    }

    /// Manually drop to the showcase (e.g. user chose "Use Showcase" in the overlay).
    public func forceFallback(reason: String = "showcase requested") {
        phase = .fallback(reason: reason)
    }

    /// The renderer mounted + called `ready()`. Flush queued deep links.
    /// - Returns: the deep links to dispatch now (in arrival order).
    @discardableResult
    public func markReady() -> [DeepLink] {
        rendererReady = true
        let flushed = pendingDeepLinks
        pendingDeepLinks.removeAll()
        return flushed
    }

    // MARK: - Deep links

    /// Enqueue or immediately surface a deep link depending on readiness.
    /// - Returns: the link to dispatch now, or `nil` if it was queued for later.
    public func enqueue(_ link: DeepLink) -> DeepLink? {
        if case .openBook(let id) = link { lastOpenedBookID = id }
        guard rendererReady, phase.isLive else {
            pendingDeepLinks.append(link)
            return nil
        }
        return link
    }

    /// Record a book the shell explicitly opened (deep-link or showcase tap).
    public func noteOpenedBook(_ id: String) { lastOpenedBookID = id }

    // MARK: - Derived

    /// True while we should display the connection overlay.
    public var isConnecting: Bool {
        if case .connecting = phase { return true }
        return false
    }

    /// True when the showcase UI should be shown instead of the web view.
    public var showsShowcase: Bool {
        if case .fallback = phase { return true }
        return false
    }
}

extension ShellViewModel.Phase {
    var isLive: Bool { if case .live = self { return true }; return false }
}
