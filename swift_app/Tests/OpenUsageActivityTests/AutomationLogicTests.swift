import Foundation
import Testing
@testable import OpenUsageActivity
@testable import UsageCore

@Suite("Automation presentation")
struct AutomationLogicTests {
    @Test("Snapshot preview exposes scheduler facts without identities or credentials")
    func sanitizedPreview() {
        let snapshot = LocalAPIResourceSnapshot(
            schemaVersion: "1.0", dataRevision: 42,
            generatedAt: "2026-07-18T02:00:00Z", localDay: "2026-07-18",
            todayTokens: 123, modelCount: 3, coveredDayCount: 1,
            balances: [],
            quotaWindowCount: 4, providerCount: 5, sourceCount: 6
        )
        let preview = AutomationPresentation.snapshotPreview(snapshot)
        #expect(preview.contains(#""dataRevision" : 42"#))
        #expect(preview.contains(#""providerCount" : 5"#))
        #expect(preview.contains(#""balanceCount" : 0"#))
        #expect(!preview.lowercased().contains("account"))
        #expect(!preview.lowercased().contains("credential"))
        #expect(!preview.lowercased().contains("token\""))
    }

    @Test("Loaded state carries only safe aggregate Automation facts")
    func loadedStateCarriesOnlySafeFacts() {
        let health = LocalAPIHealth(
            schemaVersion: "1.0", dataRevision: 42,
            generatedAt: "2026-07-18T02:00:00Z", ok: true, status: "ok"
        )
        let schema = LocalAPISchema(
            schemaVersion: "1.0", dataRevision: 42,
            generatedAt: "2026-07-18T02:00:00Z", routes: ["/v1/snapshot"]
        )
        let snapshot = LocalAPIResourceSnapshot(
            schemaVersion: "1.0", dataRevision: 42,
            generatedAt: "2026-07-18T02:00:00Z", localDay: "2026-07-18",
            todayTokens: 123, modelCount: 3, coveredDayCount: 1,
            balances: [],
            quotaWindowCount: 4, providerCount: 5, sourceCount: 6
        )
        let loaded = AutomationLoadedState(
            health: health, schema: schema, snapshot: snapshot,
            preview: AutomationPresentation.snapshotPreview(snapshot)
        )
        let visible = String(describing: loaded)
        for forbidden in [
            "socket", "localhost", "curl", "unix-socket", "helper",
            "OpenUsage Provider Settings", "/Users/", "/Applications/",
        ] {
            #expect(!visible.localizedCaseInsensitiveContains(forbidden))
        }
    }

    @Test("Failures become stable sanitized states")
    func failures() {
        #expect(AutomationPresentation.failure(.unavailable) == .unavailable)
        #expect(AutomationPresentation.failure(.schemaMismatch) == .schemaMismatch)
        #expect(AutomationPresentation.failure(.timedOut) == .timedOut)
        #expect(AutomationPresentation.failure(.responseTooLarge) == .responseTooLarge)
        #expect(AutomationPresentation.failure(.invalidResponse) == .invalidResponse)
    }
}
