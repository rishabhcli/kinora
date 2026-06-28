import AppKit
import KinoraGlassKit

/// A reading-controls `NSTouchBar` for Macs with a Touch Bar. Mirrors the §5.3/§5.4
/// surface: play/pause, page nav, and the Viewer/Director switch. Actions post the same
/// `DeepLink`s the menu + command bar use, so all three input paths converge on one
/// routing seam.
///
/// Wired by setting it as the app/window's `touchBar`. On hardware without a Touch Bar
/// this is inert (no cost), so it is safe to always install.
@MainActor
final class KinoraTouchBarProvider: NSObject, NSTouchBarDelegate {
    static let shared = KinoraTouchBarProvider()

    private enum ItemID {
        static let playPause = NSTouchBarItem.Identifier("local.kinora.touchbar.playpause")
        static let prev = NSTouchBarItem.Identifier("local.kinora.touchbar.prev")
        static let next = NSTouchBarItem.Identifier("local.kinora.touchbar.next")
        static let mode = NSTouchBarItem.Identifier("local.kinora.touchbar.mode")
        static let comment = NSTouchBarItem.Identifier("local.kinora.touchbar.comment")
    }

    func makeTouchBar() -> NSTouchBar {
        let bar = NSTouchBar()
        bar.delegate = self
        bar.defaultItemIdentifiers = [
            ItemID.prev, ItemID.playPause, ItemID.next,
            .flexibleSpace, ItemID.mode, ItemID.comment,
        ]
        return bar
    }

    func touchBar(_ touchBar: NSTouchBar, makeItemForIdentifier id: NSTouchBarItem.Identifier) -> NSTouchBarItem? {
        switch id {
        case ItemID.playPause:
            return button(id, symbol: "playpause.fill") { DeepLinkBus.shared.send(.route("/playpause")) }
        case ItemID.prev:
            return button(id, symbol: "chevron.left") { DeepLinkBus.shared.send(.route("/page/prev")) }
        case ItemID.next:
            return button(id, symbol: "chevron.right") { DeepLinkBus.shared.send(.route("/page/next")) }
        case ItemID.mode:
            let item = NSCustomTouchBarItem(identifier: id)
            let seg = NSSegmentedControl(
                labels: ["Viewer", "Director"],
                trackingMode: .selectOne,
                target: self,
                action: #selector(modeChanged(_:))
            )
            seg.selectedSegment = 0
            item.view = seg
            return item
        case ItemID.comment:
            return button(id, symbol: "text.bubble") { DeepLinkBus.shared.send(.route("/director/comment")) }
        default:
            return nil
        }
    }

    private func button(_ id: NSTouchBarItem.Identifier, symbol: String, action: @escaping () -> Void) -> NSCustomTouchBarItem {
        let item = NSCustomTouchBarItem(identifier: id)
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        let handler = ButtonHandler(action: action)
        let btn = NSButton(image: image ?? NSImage(), target: handler, action: #selector(ButtonHandler.fire))
        objc_setAssociatedObject(btn, Unmanaged.passUnretained(btn).toOpaque(), handler, .OBJC_ASSOCIATION_RETAIN)
        item.view = btn
        return item
    }

    @objc private func modeChanged(_ sender: NSSegmentedControl) {
        let route = sender.selectedSegment == 0 ? "/viewer" : "/director"
        DeepLinkBus.shared.send(.route(route))
    }
}

/// Retains a closure so a Touch Bar button target/action can call Swift code.
@MainActor
private final class ButtonHandler: NSObject {
    let action: () -> Void
    init(action: @escaping () -> Void) { self.action = action }
    @objc func fire() { action() }
}
