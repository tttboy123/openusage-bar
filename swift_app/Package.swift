// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "OpenUsageBar",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "UsageCore", targets: ["UsageCore"]),
        .executable(name: "OpenUsageBar", targets: ["OpenUsageBar"]),
        .executable(name: "OpenUsageActivity", targets: ["OpenUsageActivity"]),
    ],
    targets: [
        .target(name: "UsageCore"),
        .executableTarget(name: "OpenUsageBar", dependencies: ["UsageCore"]),
        .executableTarget(name: "OpenUsageActivity", dependencies: ["UsageCore"]),
        .testTarget(name: "UsageCoreTests", dependencies: ["UsageCore"]),
        .testTarget(name: "OpenUsageBarTests", dependencies: ["OpenUsageBar", "UsageCore"]),
        .testTarget(name: "OpenUsageActivityTests", dependencies: ["OpenUsageActivity", "UsageCore"]),
    ],
    swiftLanguageModes: [.v6]
)
