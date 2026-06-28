// swift-tools-version:6.0
import PackageDescription

// Kinora's native macOS shell: hosts the React renderer in a WKWebView behind a
// real Liquid Glass surface (NSGlassEffectView on macOS 26+, NSVisualEffectView
// fallback below). Built with the Command Line Tools / Xcode 26 toolchain:
//   swift run --package-path apps/desktop-native KinoraGlass
//   swift test --package-path apps/desktop-native
// Shell mode wants the renderer dev server on :5173 (make app-desktop-dev); with
// no server reachable the app falls back to its self-contained showcase UI.
//
// Three targets:
//   • KinoraGlassKit       — pure, platform-light, *testable* logic (bridge,
//                            token store, deep-links, view-models, endpoint).
//   • KinoraGlass          — the @main AppKit/SwiftUI executable shell.
//   • KinoraGlassKitTests  — XCTest suite over the kit (SwiftPM can only unit-test
//                            a library target, never an executable with @main).
let package = Package(
    name: "KinoraGlass",
    platforms: [.macOS("26.0")],
    targets: [
        .target(
            name: "KinoraGlassKit",
            path: "Sources/KinoraGlassKit"
        ),
        .executableTarget(
            name: "KinoraGlass",
            dependencies: ["KinoraGlassKit"],
            path: "Sources/KinoraGlass",
            resources: [.copy("Resources")]
        ),
        .testTarget(
            name: "KinoraGlassKitTests",
            dependencies: ["KinoraGlassKit"],
            path: "Tests/KinoraGlassKitTests"
        ),
    ]
)
