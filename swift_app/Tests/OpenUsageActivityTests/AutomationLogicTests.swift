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
            quotaWindowCount: 4, providerCount: 5, sourceCount: 6
        )
        let preview = AutomationPresentation.snapshotPreview(snapshot)
        #expect(preview.contains(#""dataRevision" : 42"#))
        #expect(preview.contains(#""providerCount" : 5"#))
        #expect(!preview.lowercased().contains("account"))
        #expect(!preview.lowercased().contains("credential"))
        #expect(!preview.lowercased().contains("token\""))
    }

    @Test("Copy commands are exact quoted read-only commands")
    func commands() {
        let socket = URL(fileURLWithPath: "/Users/test user/openusage.sock")
        let helper = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings")
        let commands = AutomationPresentation.commands(socketURL: socket, helperURL: helper)
        #expect(commands.curl == "curl --unix-socket '/Users/test user/openusage.sock' http://localhost/v1/snapshot")
        #expect(commands.helper.hasSuffix(" snapshot --format json --offline"))
        #expect(!commands.curl.contains("Authorization"))
        #expect(!commands.helper.contains("provider-mutate"))
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
