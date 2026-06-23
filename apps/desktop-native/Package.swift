// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "KinoraGlass",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "KinoraGlass",
            path: "Sources/KinoraGlass"
        )
    ]
)
