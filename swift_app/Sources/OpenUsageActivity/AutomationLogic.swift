import Foundation
import UsageCore

enum AutomationFailureState: Sendable, Equatable {
    case unavailable
    case timedOut
    case schemaMismatch
    case responseTooLarge
    case invalidResponse
}

struct AutomationCommands: Sendable, Equatable {
    let curl: String
    let helper: String
}

struct AutomationLoadedState: Sendable, Equatable {
    let health: LocalAPIHealth
    let schema: LocalAPISchema
    let snapshot: LocalAPIResourceSnapshot
    let preview: String
    let commands: AutomationCommands
}

enum AutomationPresentation {
    static func failure(_ error: LocalAPIClientError) -> AutomationFailureState {
        switch error {
        case .unavailable: .unavailable
        case .timedOut: .timedOut
        case .schemaMismatch: .schemaMismatch
        case .responseTooLarge: .responseTooLarge
        case .invalidResponse: .invalidResponse
        }
    }

    static func commands(socketURL: URL, helperURL: URL) -> AutomationCommands {
        AutomationCommands(
            curl: "curl --unix-socket \(quote(socketURL.path)) http://localhost/v1/snapshot",
            helper: "\(quote(helperURL.path)) snapshot --format json --offline"
        )
    }

    static func snapshotPreview(_ snapshot: LocalAPIResourceSnapshot) -> String {
        let object: [String: Any] = [
            "schemaVersion": snapshot.schemaVersion,
            "dataRevision": snapshot.dataRevision,
            "generatedAt": snapshot.generatedAt,
            "localDay": snapshot.localDay,
            "summary": [
                "todayTokens": snapshot.todayTokens.map { $0 as Any } ?? NSNull(),
                "modelCount": snapshot.modelCount,
                "coveredDayCount": snapshot.coveredDayCount,
            ],
            "quotaWindowCount": snapshot.quotaWindowCount,
            "providerCount": snapshot.providerCount,
            "sourceCount": snapshot.sourceCount,
        ]
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(
                  withJSONObject: object, options: [.prettyPrinted, .sortedKeys]
              )
        else { return "{}" }
        return String(decoding: data, as: UTF8.self)
    }

    static func helperURL(
        activityBundleURL: URL = Bundle.main.bundleURL,
        executableURL: URL? = Bundle.main.executableURL
    ) -> URL {
        let helpers = activityBundleURL.pathExtension.lowercased() == "app"
            ? activityBundleURL.deletingLastPathComponent()
            : (executableURL ?? activityBundleURL).deletingLastPathComponent()
        return helpers
            .appendingPathComponent("OpenUsage Provider Settings.app")
            .appendingPathComponent("Contents/MacOS/OpenUsage Provider Settings")
    }

    private static func quote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }
}
