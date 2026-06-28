import AppKit
import UserNotifications
import KinoraGlassKit

/// `NSApplicationDelegate` owning the OS integration points the SwiftUI lifecycle does
/// not cover cleanly:
///   • `kinora://` URL-scheme events (deep links from the browser / other apps);
///   • file open / "Open With" (PDF/EPUB) → import intent;
///   • notification authorization + foreground presentation + tap routing;
///   • dock-tile menu of recent books.
///
/// All OS events are normalised into `DeepLink`s and posted onto `DeepLinkBus.shared`,
/// which the active shell scene observes. This keeps the OS-plumbing here and the
/// routing decisions in the testable kit.
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Notifications: request authorization; present even when foreground.
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if let error { Log.app.error("notif auth error: \(error.localizedDescription, privacy: .public)") }
            else { Log.app.info("notif auth granted=\(granted)") }
        }

        // URL-scheme events (kinora://…). SwiftUI's onOpenURL covers most, but registering
        // here too guarantees we catch links that arrive before a scene is on screen.
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleURLEvent(_:withReply:)),
            forEventClass: AEEventClass(kInternetEventClass),
            andEventID: AEEventID(kAEGetURL)
        )

        NSApp.appearance = NSAppearance(named: .darkAqua)

        // Install the reading-controls Touch Bar app-wide (inert without TB hardware).
        NSApp.touchBar = KinoraTouchBarProvider.shared.makeTouchBar()

        Log.app.info("Kinora native shell launched")
    }

    // MARK: - URL scheme

    @objc func handleURLEvent(_ event: NSAppleEventDescriptor, withReply reply: NSAppleEventDescriptor) {
        guard let raw = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
              let url = URL(string: raw) else { return }
        Task { @MainActor in
            if !DeepLinkBus.shared.send(url: url) {
                Log.app.error("unrecognised deep link: \(raw, privacy: .public)")
            }
        }
    }

    // SwiftUI also funnels openURL/openFile through here on older paths.
    func application(_ application: NSApplication, open urls: [URL]) {
        Task { @MainActor in
            for url in urls {
                if url.isFileURL { _ = DeepLinkBus.shared.sendDroppedFile(url) }
                else { _ = DeepLinkBus.shared.send(url: url) }
            }
        }
    }

    func application(_ sender: NSApplication, openFile filename: String) -> Bool {
        let url = URL(fileURLWithPath: filename)
        Task { @MainActor in _ = DeepLinkBus.shared.sendDroppedFile(url) }
        return true
    }

    // MARK: - Notifications

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // A notification id of the form "book:<id>" deep-links to that book on tap.
        let id = response.notification.request.identifier
        if id.hasPrefix("book:") {
            let bookID = String(id.dropFirst("book:".count))
            Task { @MainActor in DeepLinkBus.shared.send(.openBook(id: bookID)) }
        }
        completionHandler()
    }

    // MARK: - Dock menu (recent books)

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        let store = ShellSettingsStore(store: UserDefaults.standard)
        if let last = store.load().lastBookID {
            let title = Book.demo(id: last)?.title ?? last
            let item = NSMenuItem(title: "Open \(title)", action: #selector(openRecent(_:)), keyEquivalent: "")
            item.representedObject = last
            item.target = self
            menu.addItem(item)
        }
        let importItem = NSMenuItem(title: "Import a Book…", action: #selector(importBook), keyEquivalent: "")
        importItem.target = self
        menu.addItem(importItem)
        return menu
    }

    @objc private func openRecent(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        Task { @MainActor in DeepLinkBus.shared.send(.openBook(id: id)) }
    }

    @objc private func importBook() {
        Task { @MainActor in
            let panel = NSOpenPanel()
            panel.allowedContentTypes = ImportTypes.contentTypes
            panel.allowsMultipleSelection = false
            if panel.runModal() == .OK, let url = panel.url {
                _ = DeepLinkBus.shared.sendDroppedFile(url)
            }
        }
    }

    // Keep the app running with no windows so dock interactions still work, but quit on
    // last-window-close to match a single-document feel by default.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
