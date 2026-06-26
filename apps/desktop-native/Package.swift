// swift-tools-version:6.0
import PackageDescription

// Kinora's native macOS shell: hosts the React renderer in a WKWebView behind a
// real Liquid Glass surface (NSGlassEffectView on macOS 26+, NSVisualEffectView
// fallback below). Built with the Command Line Tools (no Xcode project needed):
//   swift run --package-path apps/desktop-native
// Requires the renderer dev server on :5173 (make app-desktop-dev).
let package = Package(
    name: "KinoraGlass",
    platforms: [.macOS("26.0")],
    targets: [
        .executableTarget(
            name: "KinoraGlass",
            path: "Sources/KinoraGlass",
            resources: [.copy("Resources")]
        )
    ]
)
