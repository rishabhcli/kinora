import Foundation
import Combine
import KinoraGlassKit

/// A tiny main-actor event bus carrying `DeepLink`s from the AppKit layer
/// (URL-scheme events, file-drop, menu actions) into the SwiftUI shell scene.
///
/// AppKit owns the OS integration points; the SwiftUI `ShellContainerView` subscribes
/// to `publisher` and routes each link through the `ShellViewModel`. A `PassthroughSubject`
/// is the right shape: links are transient events, not retained state.
@MainActor
final class DeepLinkBus: ObservableObject {
    private let subject = PassthroughSubject<DeepLink, Never>()
    var publisher: AnyPublisher<DeepLink, Never> { subject.eraseToAnyPublisher() }

    /// Shared bus the AppDelegate posts into and the active scene observes.
    static let shared = DeepLinkBus()

    func send(_ link: DeepLink) { subject.send(link) }

    /// Convenience: parse a raw URL and forward if it is a recognised `kinora://` link.
    @discardableResult
    func send(url: URL) -> Bool {
        guard let link = DeepLink.parse(url) else { return false }
        subject.send(link)
        return true
    }

    /// Convenience: forward a dropped/opened file as an import intent.
    @discardableResult
    func sendDroppedFile(_ url: URL) -> Bool {
        guard let link = DeepLink.from(droppedFile: url) else { return false }
        subject.send(link)
        return true
    }
}
