import Foundation
import UsageCore

enum AutomationFailureState: Sendable, Equatable {
    case unavailable
    case timedOut
    case schemaMismatch
    case responseTooLarge
    case invalidResponse
}

struct AutomationLoadedState: Sendable, Equatable {
    let health: LocalAPIHealth
    let schema: LocalAPISchema
    let snapshot: LocalAPIResourceSnapshot
    let preview: String
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
            "balanceCount": snapshot.balances.count,
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

}
