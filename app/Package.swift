// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ReviewBotApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "ReviewBotApp", path: "Sources/ReviewBotApp")
    ]
)
