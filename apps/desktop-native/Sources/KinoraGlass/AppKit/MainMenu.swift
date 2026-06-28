import SwiftUI
import KinoraGlassKit

/// The native menu bar + keyboard shortcuts, expressed as SwiftUI `Commands`. Actions
/// post `DeepLink`s onto the shared bus so the reading-room/renderer handles them,
/// keeping menu wiring declarative and out of the views.
struct KinoraCommands: Commands {
    let deepLinks: DeepLinkBus
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        // App menu addition.
        CommandGroup(replacing: .appInfo) {
            Button("About Kinora") { showAbout() }
        }

        // File menu — import + new window.
        CommandGroup(replacing: .newItem) {
            Button("New Window") { openWindow(id: "shell") }
                .keyboardShortcut("n", modifiers: [.command])
            Button("Import Book…") { importBook() }
                .keyboardShortcut("o", modifiers: [.command])
            Divider()
            Button("Open Library") { deepLinks.send(.route("/library")) }
                .keyboardShortcut("l", modifiers: [.command, .shift])
        }

        // Reading menu — playhead + mode controls (mirror §5.3/§5.4).
        CommandMenu("Reading") {
            Button("Viewer Mode") { deepLinks.send(.route("/viewer")) }
                .keyboardShortcut("1", modifiers: [.command])
            Button("Director Mode") { deepLinks.send(.route("/director")) }
                .keyboardShortcut("2", modifiers: [.command])
            Divider()
            Button("Previous Page") { deepLinks.send(.route("/page/prev")) }
                .keyboardShortcut(.leftArrow, modifiers: [.command])
            Button("Next Page") { deepLinks.send(.route("/page/next")) }
                .keyboardShortcut(.rightArrow, modifiers: [.command])
            Button("Play / Pause") { deepLinks.send(.route("/playpause")) }
                .keyboardShortcut(.space, modifiers: [])
            Divider()
            Button("Add Director Comment…") { deepLinks.send(.route("/director/comment")) }
                .keyboardShortcut("k", modifiers: [.command])
            Button("Shot Timeline") { deepLinks.send(.route("/director/timeline")) }
                .keyboardShortcut("t", modifiers: [.command, .shift])
            Button("Canon Editor") { deepLinks.send(.route("/director/canon")) }
                .keyboardShortcut("e", modifiers: [.command, .shift])
        }

        // View menu — immersive reading + chrome toggles.
        CommandGroup(after: .toolbar) {
            Button("Toggle Immersive Reading") { toggleImmersive() }
                .keyboardShortcut("f", modifiers: [.command, .control])
            Button("Toggle Sidebar") { deepLinks.send(.route("/toggle-sidebar")) }
                .keyboardShortcut("s", modifiers: [.command, .control])
        }

        // Help.
        CommandGroup(replacing: .help) {
            Button("Kinora Help") {
                deepLinks.send(.route("/help"))
            }
        }
    }

    private func importBook() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = ImportTypes.contentTypes
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            deepLinks.send(.importFile(url))
        }
    }

    private func toggleImmersive() {
        NSApp.keyWindow?.toggleFullScreen(nil)
    }

    private func showAbout() {
        let alert = NSAlert()
        alert.messageText = "Kinora"
        alert.informativeText = """
        The native macOS Liquid Glass shell.
        Version \(BridgeContract.shellVersion)

        Real Liquid Glass (NSGlassEffectView / .glassEffect) on the macOS 26+ SDK —
        the genuine material Electron cannot render.
        """
        alert.runModal()
    }
}
