import Foundation
import Testing
@testable import UsageCore

@Suite("Python and Swift ledger contract")
struct CrossLanguageContractTests {
    @Test("Python-produced version four facts are read identically by Swift")
    func pythonLedgerMatchesSwiftFacts() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("OpenUsageCrossLanguage-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let database = directory.appendingPathComponent("ledger.sqlite3")
        let expectedURL = directory.appendingPathComponent("expected.json")
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = [
            root.appendingPathComponent("scripts/generate_cross_language_fixture.py").path,
            "--database", database.path, "--expected", expectedURL.path,
        ]
        let errorPipe = Pipe()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errorPipe
        try process.run()
        process.waitUntilExit()
        #expect(process.terminationStatus == 0)

        let expected = try #require(
            try JSONSerialization.jsonObject(with: Data(contentsOf: expectedURL))
                as? [String: Any]
        )
        let now = try #require(ISO8601DateFormatter().date(from: "2026-07-18T01:00:00Z"))
        let repository = try UsageRepository(databaseURL: database, now: { now })
        defer { repository.close() }
        let summary = try repository.compactSummary(on: LocalDay("2026-07-18"))
        let quota = try #require(repository.capacity(limit: nil).first)
        let provider = try #require(repository.providerInstances().first)
        let health = try repository.sourceHealth()
        let expectedSummary = try #require(expected["summary"] as? [String: Any])
        let expectedQuota = try #require((expected["quotaWindows"] as? [[String: Any]])?.first)
        let expectedProvider = try #require((expected["providers"] as? [[String: Any]])?.first)
        let expectedSource = try #require((expected["sources"] as? [[String: Any]])?.first)
        let revision = try repository.dataRevision()

        #expect(revision == Int64(expected["dataRevision"] as! Int))
        #expect(summary.todayTokens == Int64(expectedSummary["todayTokens"] as! Int))
        #expect(quota.recordID == expectedQuota["recordId"] as? String)
        #expect(quota.remainingRatio == expectedQuota["remainingRatio"] as? Double)
        #expect(quota.resetsAt == expectedQuota["resetsAt"] as? String)
        #expect(provider.providerID == expectedProvider["providerId"] as? String)
        #expect(provider.familyID == expectedProvider["familyId"] as? String)
        #expect(health.sources.first?.providerID == expectedSource["providerId"] as? String)
        #expect(health.sources.first?.state == expectedSource["state"] as? String)
        #expect(health.revision == Int64(expected["dataRevision"] as! Int))
    }
}
