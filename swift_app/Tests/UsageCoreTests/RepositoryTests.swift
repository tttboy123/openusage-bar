import Dispatch
import Foundation
import Testing
@testable import UsageCore

@Suite("Read-only SQLite repository")
struct RepositoryTests {
    private let now = Date(timeIntervalSince1970: 1_784_020_200) // 2026-07-14T09:10:00Z

    @Test("Provider instances preserve explicit family identity and old ledgers stay compatible")
    func providerInstanceLedger() throws {
        let current = try SQLiteFixture(
            providerInstancesSQL: SQLiteFixture.productionProviderInstances
        )
        let repository = try UsageRepository(databaseURL: current.databaseURL, now: { now })
        defer { repository.close() }
        let instances = try repository.providerInstances()
        #expect(instances.map(\.providerID) == [
            "copilot", "gemini_cli", "minimax-foo", "minimax-main", "ollama", "opencode",
            "qwen_cli", "step-plan-cn",
        ])
        #expect(instances.first { $0.providerID == "minimax-main" }?.familyID == "minimax")
        #expect(instances.first { $0.providerID == "step-plan-cn" }?.familyID == "step_plan")
        #expect(instances.first { $0.providerID == "minimax-foo" }?.category == .api)

        let old = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_state VALUES
              ('minimax-foo.daily','2026-07-14T08:00:00Z','minimax-foo','','daily','tokens',
               NULL,NULL,'50',0.5,NULL,NULL,NULL,'ok','live',0,1,'generic-old');
            """)
        let oldRepository = try UsageRepository(databaseURL: old.databaseURL, now: { now })
        defer { oldRepository.close() }
        #expect(try oldRepository.providerInstances().isEmpty)
        let oldCapacity = try oldRepository.capacity(limit: nil)
        #expect(oldCapacity.first { $0.providerID == "codex" }?.providerDescriptor.familyID == "codex")
        #expect(oldCapacity.first { $0.providerID == "minimax-foo" }?.providerDescriptor.category == .api)
        #expect(oldCapacity.first { $0.providerID == "minimax-foo" }?.providerDescriptor.familyID == "minimax-foo")
    }

    @Test("Malformed or partial provider identity tables fail safely without schema mutation")
    func malformedProviderInstanceLedger() throws {
        let partial = try SQLiteFixture(extraSQL: """
            CREATE TABLE provider_instances(provider_id TEXT PRIMARY KEY, family_id TEXT NOT NULL);
            INSERT INTO provider_instances VALUES('codex','codex');
            """)
        let before = try Data(contentsOf: partial.databaseURL)
        let repository = try UsageRepository(databaseURL: partial.databaseURL, now: { now })
        defer { repository.close() }
        #expect(try repository.providerInstances().isEmpty)
        #expect(try Data(contentsOf: partial.databaseURL) == before)

        let malformed = try SQLiteFixture(
            providerInstancesSQL: SQLiteFixture.productionProviderInstances.replacingOccurrences(
                of: "'api','api_key'", with: "'not_a_category','api_key'"
            )
        )
        let malformedRepository = try UsageRepository(databaseURL: malformed.databaseURL, now: { now })
        defer { malformedRepository.close() }
        #expect(try malformedRepository.providerInstances().isEmpty)
    }

    @Test("Tampered family category and source bindings fall back without reaching UI facts")
    func tamperedProviderBindings() throws {
        let invalidRows = [
            SQLiteFixture.providerInstanceRow(
                familyID: "ollama", displayName: "Forged Codex",
                category: "local_tool"
            ),
            SQLiteFixture.providerInstanceRow(
                providerID: "minimax-main", familyID: "minimax",
                displayName: "Forged MiniMax", category: "api",
                credentialSource: "minimax_builtin_api", sourceKind: "builtin_api"
            ),
            SQLiteFixture.providerInstanceRow(
                providerID: "minimax-main", familyID: "minimax",
                displayName: "Forged Source", category: "subscription",
                credentialSource: "openusage", sourceKind: "generic_https"
            ),
        ]
        for row in invalidRows {
            let fixture = try SQLiteFixture(providerInstancesSQL: row)
            let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
            #expect(try repository.providerInstances().isEmpty)
            let capacity = try repository.capacity(limit: nil)
            #expect(!String(describing: capacity).contains("Forged"))
            repository.close()
        }
    }

    @Test("Private-looking display labels are rejected while ordinary product labels remain valid")
    func privateProviderLabels() throws {
        let unsafeLabels = [
            "owner@example.com",
            "/Users/test/.config/provider.json",
            "token=sk-review-secret",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature123",
            "AKIAIOSFODNN7EXAMPLE1234567890",
            #"{"token":"sk-review-secret"}"#,
        ]
        for label in unsafeLabels {
            let fixture = try SQLiteFixture(
                providerInstancesSQL: SQLiteFixture.providerInstanceRow(displayName: label)
            )
            let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
            let instances = try repository.providerInstances()
            #expect(instances.isEmpty)
            #expect(!String(describing: instances).contains(label))
            #expect(try repository.capacity(limit: nil).first { $0.providerID == "codex" }?
                .providerDescriptor.displayName == "Codex")
            repository.close()
        }

        let safe = try SQLiteFixture(
            providerInstancesSQL: SQLiteFixture.providerInstanceRow(displayName: "Codex Team 2")
        )
        let safeRepository = try UsageRepository(databaseURL: safe.databaseURL, now: { now })
        defer { safeRepository.close() }
        #expect(try safeRepository.providerInstances().map(\.displayName) == ["Codex Team 2"])
    }

    @Test("LocalDay is strict and comparable")
    func localDayValidation() throws {
        let july2 = try LocalDay("2026-07-02")
        let july3 = try LocalDay("2026-07-03")
        #expect(july2.description == "2026-07-02")
        #expect(july2 < july3)
        #expect(throws: LocalDayError.self) { try LocalDay("2026-7-2") }
        #expect(throws: LocalDayError.self) { try LocalDay("2026-02-30") }
    }

    @Test("Exact canonical fields, nulls, coverage, and revision are preserved")
    func exactFixtureRead() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let data = try repository.activity(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )
        #expect(data.revision == 1842)
        #expect(data.records.count == 1)
        let row = try #require(data.records.first)
        let july2 = try LocalDay("2026-07-02")
        #expect(row.day == july2)
        #expect(row.providerID == "codex")
        #expect(row.accountRef == "")
        #expect(row.modelID == "gpt-5.5")
        #expect(row.inputTokens == 40_000_000)
        #expect(row.outputTokens == 4_200_000)
        #expect(row.cacheReadTokens == 30_000_000)
        #expect(row.cacheCreationTokens == 0)
        #expect(row.reasoningTokens == nil)
        #expect(row.totalTokens == 74_200_000)
        #expect(row.costAmount == "4.237100")
        #expect(row.costCurrency == "USD")
        #expect(row.costBasis == "price_table_estimated")
        #expect(row.quality == "derived")
        #expect(row.revision == 7)
        #expect(row.recordID == "daily:2026-07-02:codex::gpt-5.5")
        #expect(try repository.dataRevision() == 1842)
        #expect(data.coverage.map(\.isCovered) == [
            true, false, false,
            true, false, false,
            true, false, false,
        ])
        #expect(data.knownScopes.contains(ProviderScope(providerID: "stepfun", accountRef: "")))
    }

    @Test("Version three exposes usage and coverage provenance")
    func sourceProvenance() throws {
        let fixture = try SQLiteFixture(
            userVersion: 3,
            extraSQL: """
            UPDATE daily_model_usage SET source_id='openai.organization.usage';
            UPDATE daily_coverage SET source_id='openusage.daily';
            """
        )
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let data = try repository.activity(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )
        #expect(data.records.first?.sourceID == "openai.organization.usage")
        #expect(data.coverage.filter(\.isCovered).allSatisfy {
            $0.sourceID == "openusage.daily"
        })
        #expect(data.coverage.filter { !$0.isCovered }.allSatisfy {
            $0.sourceID == nil
        })

        let costs = try repository.dailyCosts(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )
        #expect(costs.records.map(\.amount) == ["0", "12.34"])

        let malformed = try SQLiteFixture(
            userVersion: 3,
            extraSQL: "UPDATE daily_model_usage SET source_id='bad/source';"
        )
        let malformedRepository = try UsageRepository(
            databaseURL: malformed.databaseURL, now: { now }
        )
        defer { malformedRepository.close() }
        #expect(throws: RepositoryError.corruptData) {
            try malformedRepository.activity(
                from: LocalDay("2026-07-02"), to: LocalDay("2026-07-02")
            )
        }
    }

    @Test("Version two costs preserve decimals and distinguish known zero from missing")
    func dailyCosts() throws {
        let fixture = try SQLiteFixture(userVersion: 2)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let data = try repository.dailyCosts(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )
        #expect(data.revision == 1842)
        #expect(data.records.map(\.amount) == ["0", "12.34"])
        #expect(data.records.map(\.currency) == ["USD", "USD"])
        #expect(data.records.map(\.costKind) == ["actual", "actual"])
        #expect(data.records.map(\.recordID) == [
            "cost:2026-07-01:openai::actual:USD",
            "cost:2026-07-02:openai::actual:USD",
        ])
        #expect(data.coverage.map(\.isCovered) == [true, true, true])
        #expect(data.knownScopes == [ProviderScope(providerID: "openai", accountRef: "")])

        let unknown = try repository.dailyCosts(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-01"),
            providerIDs: ["unknown"]
        )
        #expect(unknown.records.isEmpty)
        #expect(unknown.coverage.map(\.isCovered) == [false])
    }

    @Test("Cost source attempts without rows or coverage do not become known scopes")
    func costSourceStatusDoesNotInventCoverage() throws {
        let fixture = try SQLiteFixture(
            userVersion: 2,
            extraSQL: """
            INSERT INTO source_status VALUES(
              'ghost','openusage.costs.daily','temporarily_unavailable',
              '2026-07-14T09:00:00Z',NULL,NULL,'command_failed');
            """
        )
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let data = try repository.dailyCosts(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )

        #expect(data.knownScopes == [ProviderScope(providerID: "openai", accountRef: "")])
        #expect(!data.coverage.contains { $0.providerID == "ghost" })
    }

    @Test("Version one remains readable with an empty native cost dataset")
    func versionOneCosts() throws {
        let fixture = try SQLiteFixture(userVersion: 1)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let data = try repository.dailyCosts(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-03")
        )
        #expect(data.records.isEmpty)
        #expect(data.coverage.isEmpty)
        #expect(data.knownScopes.isEmpty)
        #expect(data.revision == 1842)
    }

    @Test("Capacity chooses the most urgent window and preserves stale facts")
    func capacitySemantics() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let capacity = try repository.capacity(limit: nil)

        #expect(capacity.map(\.providerID) == ["minimax", "codex"])
        let minimax = try #require(capacity.first)
        #expect(minimax.recordID == "minimax.five-hour")
        #expect(minimax.remainingRatio == 0.18)
        #expect(minimax.used == "82")
        #expect(minimax.limit == "100")
        #expect(minimax.remaining == "18")
        #expect(minimax.state == "ok")
        #expect(minimax.stale == false)
        let codex = try #require(capacity.last)
        #expect(codex.remainingRatio == nil)
        #expect(codex.stale)
    }

    @Test("Capacity groups by provider account, includes zero, and limits after urgency sort")
    func capacityZeroAndAccounts() throws {
        let fixture = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_state VALUES
              ('kiro.monthly','2026-07-14T08:00:00Z','kiro','','monthly','requests',
               NULL,NULL,'0',0.0,NULL,NULL,NULL,'ok','live',0,1,'kiro-zero'),
              ('kiro.weekly','2026-07-14T08:00:00Z','kiro','','weekly','requests',
               NULL,NULL,'90',0.9,NULL,NULL,NULL,'ok','live',0,1,'kiro-high'),
              ('minimax.team','2026-07-14T08:00:00Z','minimax','team','monthly','tokens',
               NULL,NULL,'5',0.05,NULL,NULL,NULL,'ok','live',0,1,'team-low');
            """)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let rows = try repository.capacity(limit: 2)
        #expect(rows.map(\.recordID) == ["kiro.monthly", "minimax.team"])
        #expect(rows.map(\.remainingRatio) == [0.0, 0.05])
    }

    @Test("Source health applies stale-at strictly and keeps stored facts")
    func sourceHealthSemantics() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let health = try repository.sourceHealth()

        #expect(health.revision == 1842)
        #expect(health.hasIssues)
        let cursor = try #require(health.sources.first { $0.providerID == "cursor" })
        #expect(cursor.state == "ok")
        #expect(cursor.effectiveState == "stale")
        #expect(cursor.staleAt == "2026-07-14T07:05:00Z")
        let codex = try #require(health.sources.first { $0.providerID == "codex" })
        #expect(codex.effectiveState == "ok")
    }

    @Test("Quota history is bounded chronological and exposes only canonical display facts")
    func quotaHistory() throws {
        let fixture = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES
              ('minimax.five-hour','2026-07-14T06:00:00Z','minimax','acct_hash','5-hour',
               '{"remaining_ratio":0.42,"state":"ok","stale":false,"token":"must-not-escape"}','a'),
              ('minimax.five-hour','2026-07-14T07:00:00Z','minimax','acct_hash','5-hour',
               '{"remaining_ratio":0.31,"state":"ok","stale":false,"cookie":"must-not-escape"}','b'),
              ('minimax.five-hour','2026-07-14T08:00:00Z','minimax','acct_hash','5-hour',
               '{"remaining_ratio":0.18,"state":"stale","stale":true,"payload":"must-not-escape"}','c');
            """)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let rows = try repository.quotaHistory(providerID: "minimax", accountRef: "acct_hash", limit: 2)
        #expect(rows.map(\.observedAt) == ["2026-07-14T07:00:00Z", "2026-07-14T08:00:00Z"])
        #expect(rows.map(\.remainingRatio) == [0.31, 0.18])
        #expect(rows.map(\.state) == ["ok", "stale"])
        #expect(rows.map(\.stale) == [false, true])
        #expect(String(describing: rows).contains("must-not-escape") == false)
        #expect(throws: RepositoryError.self) {
            try repository.quotaHistory(providerID: "bad' OR 1=1", limit: 2)
        }
        #expect(throws: RepositoryError.self) {
            try repository.quotaHistory(limit: 0)
        }
    }

    @Test("Production quota history accepts exact zero and one ratios without treating them as booleans")
    func quotaHistoryBoundaryRatios() throws {
        let fixture = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES
              ('cursor.monthly','2026-07-14T07:00:00Z','cursor','','monthly',
               '{"remaining_ratio":0.0,"state":"ok","stale":false}','zero'),
              ('step.weekly','2026-07-14T08:00:00Z','stepfun','','weekly',
               '{"remaining_ratio":1.0,"state":"ok","stale":false}','one');
            """)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let rows = try repository.quotaHistory(
            providerIDs: ["cursor", "stepfun"],
            observedAtOrAfter: "2026-07-14T00:00:00Z",
            observedBefore: "2026-07-15T00:00:00Z",
            perSeriesLimit: 10
        )
        #expect(rows.items.map(\.remainingRatio) == [0.0, 1.0])
    }

    @Test("Quota history carries the authoritative reset timestamp from snapshot payloads")
    func quotaHistoryResetTimestamp() throws {
        let fixture = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES
              ('minimax.five-hour','2026-07-14T07:00:00Z','minimax','','5-hour',
               '{"remaining_ratio":0.31,"resets_at":"2026-07-14T10:00:00Z","state":"ok","stale":false}','reset-a'),
              ('minimax.five-hour','2026-07-14T10:01:00Z','minimax','','5-hour',
               '{"remaining_ratio":0.99,"resets_at":"2026-07-14T15:00:00Z","state":"ok","stale":false}','reset-b');
            """)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }

        let rows = try repository.quotaHistory(providerID: "minimax")

        #expect(rows.map(\.resetsAt) == ["2026-07-14T10:00:00Z", "2026-07-14T15:00:00Z"])
    }

    @Test("Quota history filters before a fair per-series bound and reports partial history")
    func quotaHistoryFairBound() throws {
        let fixture = try SQLiteFixture(extraSQL: """
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES ('a.weekly','2026-07-14T00:00:00.000000Z','a','','weekly',
              '{"remaining_ratio":0.8,"state":"ok","stale":false}','a-only');
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES ('a.monthly','2026-07-15T00:00:00.000000Z','a','','monthly',
              '{"remaining_ratio":0.9,"state":"ok","stale":false}','a-exclusive-end');
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            ) VALUES
              ('a.micro-before','2026-07-14T00:00:00.123455Z','micro','','micro-before',
               '{"remaining_ratio":0.1,"state":"ok","stale":false}','micro-before'),
              ('a.micro-exact','2026-07-14T00:00:00.123456Z','micro','','micro-exact',
               '{"remaining_ratio":0.2,"state":"ok","stale":false}','micro-exact'),
              ('a.micro-end','2026-07-14T00:00:00.123457Z','micro','','micro-end',
               '{"remaining_ratio":0.3,"state":"ok","stale":false}','micro-end');
            WITH RECURSIVE counter(value) AS (
              SELECT 0 UNION ALL SELECT value + 1 FROM counter WHERE value < 999
            )
            INSERT INTO quota_snapshots(
              record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
            )
            SELECT 'b.five-hour',strftime('%Y-%m-%dT%H:%M:%SZ','2026-07-14T00:00:00Z','+' || value || ' minutes'),
              'b','','5-hour','{"remaining_ratio":0.5,"state":"ok","stale":false}','b-' || value
            FROM counter;
            """)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let selected = try repository.quotaHistory(
            providerIDs: ["a"], observedAtOrAfter: "2026-07-14T00:00:00Z",
            observedBefore: "2026-07-15T00:00:00Z", perSeriesLimit: 10
        )
        #expect(selected.items.map(\.providerID) == ["a"])
        #expect(!selected.isTruncated)
        let canonicalFractional = try repository.quotaHistory(
            providerIDs: ["a"], observedAtOrAfter: "2026-07-14T00:00:00.000000Z",
            observedBefore: "2026-07-15T00:00:00.000000Z", perSeriesLimit: 10
        )
        #expect(canonicalFractional.items == selected.items)
        let microsecondWindow = try repository.quotaHistory(
            providerIDs: ["micro"], observedAtOrAfter: "2026-07-14T00:00:00.123456Z",
            observedBefore: "2026-07-14T00:00:00.123457Z", perSeriesLimit: 10
        )
        #expect(microsecondWindow.items.map(\.recordID) == ["a.micro-exact"])
        let equivalentOffset = try repository.quotaHistory(
            providerIDs: ["a"], observedAtOrAfter: "2026-07-14T08:00:00+08:00",
            observedBefore: "2026-07-15T08:00:00+08:00", perSeriesLimit: 10
        )
        #expect(equivalentOffset.items == selected.items)

        let all = try repository.quotaHistory(
            providerIDs: ["a", "b"], observedAtOrAfter: "2026-07-14T00:00:00Z",
            observedBefore: "2026-07-15T00:00:00Z", perSeriesLimit: 10
        )
        #expect(all.items.count == 11)
        #expect(all.items.contains { $0.providerID == "a" })
        #expect(all.items.count { $0.providerID == "b" } == 10)
        #expect(all.isTruncated)
        #expect(all.truncatedSeriesIDs == ["b||b.five-hour|5-hour"])
        #expect(!all.truncatedSeriesIDs.contains("a||a.weekly|weekly"))
        #expect(throws: RepositoryError.self) {
            try repository.quotaHistory(
                providerIDs: ["../private"], observedAtOrAfter: "2026-07-14T00:00:00Z",
                observedBefore: "2026-07-15T00:00:00Z", perSeriesLimit: 10
            )
        }
        #expect(throws: RepositoryError.self) {
            try repository.quotaHistory(
                providerIDs: ["a"], observedAtOrAfter: "invalid",
                observedBefore: "2026-07-15T00:00:00Z", perSeriesLimit: 10
            )
        }
    }

    @Test("Stale-at equality is unhealthy")
    func staleAtEquality() throws {
        let fixture = try SQLiteFixture()
        let exact = Date(timeIntervalSince1970: 1_784_012_700) // 2026-07-14T07:05:00Z
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { exact })
        defer { repository.close() }
        let cursor = try #require(try repository.sourceHealth().sources.first { $0.providerID == "cursor" })
        #expect(cursor.effectiveState == "stale")
    }

    @Test("Compact summary distinguishes empty missing partial covered zero and active days")
    func compactSummaryCoverage() throws {
        let emptyFixture = try SQLiteFixture(extraSQL: "DELETE FROM daily_model_usage; DELETE FROM daily_coverage; DELETE FROM source_status;")
        let empty = try UsageRepository(databaseURL: emptyFixture.databaseURL, now: { now })
        #expect(try empty.compactSummary(on: LocalDay("2026-07-04")).todayTokens == nil)
        #expect(!(try empty.compactSummary(on: LocalDay("2026-07-04")).isTodayComplete))
        empty.close()

        let missingFixture = try SQLiteFixture(extraSQL: "DELETE FROM daily_model_usage; DELETE FROM daily_coverage WHERE provider_id != 'codex'; DELETE FROM source_status WHERE provider_id != 'codex';")
        let missing = try UsageRepository(databaseURL: missingFixture.databaseURL, now: { now })
        #expect(try missing.compactSummary(on: LocalDay("2026-07-04")).todayTokens == nil)
        #expect(!(try missing.compactSummary(on: LocalDay("2026-07-04")).isTodayComplete))
        missing.close()

        let partialFixture = try SQLiteFixture(extraSQL: "DELETE FROM daily_coverage WHERE provider_id = 'stepfun'; DELETE FROM source_status WHERE provider_id = 'stepfun';")
        let partial = try UsageRepository(databaseURL: partialFixture.databaseURL, now: { now })
        let partialSummary = try partial.compactSummary(on: LocalDay("2026-07-02"))
        #expect(partialSummary.todayTokens == 74_200_000)
        #expect(!partialSummary.isTodayComplete)
        partial.close()

        let zeroFixture = try SQLiteFixture(extraSQL: "DELETE FROM daily_model_usage; DELETE FROM daily_coverage WHERE provider_id != 'codex'; DELETE FROM source_status;")
        let zero = try UsageRepository(databaseURL: zeroFixture.databaseURL, now: { now })
        #expect(try zero.compactSummary(on: LocalDay("2026-07-02")).todayTokens == 0)
        #expect(try zero.compactSummary(on: LocalDay("2026-07-02")).isTodayComplete)
        zero.close()

        let activeFixture = try SQLiteFixture(extraSQL: "DELETE FROM daily_coverage WHERE provider_id = 'stepfun'; DELETE FROM source_status;")
        let active = try UsageRepository(databaseURL: activeFixture.databaseURL, now: { now })
        let summary = try active.compactSummary(on: LocalDay("2026-07-02"))
        #expect(summary.todayTokens == 74_200_000)
        #expect(summary.isTodayComplete)
        #expect(summary.capacity.map(\.providerID) == ["minimax", "codex"])
        #expect(summary.updatedAt == "2026-07-14T08:00:00Z")
        #expect(summary.hasHealthIssues)
        #expect(summary.revision == 1842)
        active.close()
    }

    @Test("Provider and model filters are bound and ordered")
    func preparedFilters() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        let selected = try repository.activity(
            from: LocalDay("2026-07-02"), to: LocalDay("2026-07-02"),
            providerIDs: ["codex"], modelIDs: ["gpt-5.5"]
        )
        #expect(selected.records.map(\.modelID) == ["gpt-5.5"])
        #expect(throws: RepositoryError.self) {
            try repository.activity(
                from: LocalDay("2026-07-02"), to: LocalDay("2026-07-02"),
                providerIDs: ["codex' OR 1=1 --"]
            )
        }
    }

    @Test("Bounds and stable IDs reject invalid requests")
    func invalidInputs() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        defer { repository.close() }
        #expect(throws: RepositoryError.self) { try repository.capacity(limit: 0) }
        #expect(throws: RepositoryError.self) { try repository.capacity(limit: 1001) }
        #expect(throws: RepositoryError.self) {
            try repository.activity(from: LocalDay("2026-07-03"), to: LocalDay("2026-07-02"))
        }
        #expect(throws: RepositoryError.self) {
            try repository.activity(from: LocalDay("2024-01-01"), to: LocalDay("2026-07-02"))
        }
        let maximum = try repository.activity(
            from: LocalDay("2024-07-02"), to: LocalDay("2026-07-02")
        )
        #expect(maximum.coverage.count == 731 * 3)
    }

    @Test("Missing and newer databases fail with sanitized typed errors")
    func sanitizedErrors() throws {
        let missing = FileManager.default.temporaryDirectory
            .appendingPathComponent("private-account-\(UUID().uuidString)/secret.sqlite")
        do {
            _ = try UsageRepository(databaseURL: missing)
            Issue.record("missing database unexpectedly opened")
        } catch let error as RepositoryError {
            #expect(error == .databaseUnavailable)
            #expect(!error.localizedDescription.contains(missing.path))
            #expect(!String(describing: error).contains("private-account"))
        }

        let newer = try SQLiteFixture(userVersion: 4)
        do {
            _ = try UsageRepository(databaseURL: newer.databaseURL)
            Issue.record("newer schema unexpectedly opened")
        } catch let error as RepositoryError {
            #expect(error == .incompatibleSchema)
            #expect(!error.localizedDescription.contains(newer.databaseURL.path))
        }

        let incompatible = try SQLiteFixture(extraSQL: "DROP TABLE source_status;")
        #expect(throws: RepositoryError.self) {
            try UsageRepository(databaseURL: incompatible.databaseURL)
        }
        let nullableAccount = try SQLiteFixture(extraSQL: """
            ALTER TABLE daily_coverage RENAME TO old_daily_coverage;
            CREATE TABLE daily_coverage(
              day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT DEFAULT '',
              imported_at TEXT NOT NULL, PRIMARY KEY(day,provider_id,account_ref));
            INSERT INTO daily_coverage SELECT * FROM old_daily_coverage;
            DROP TABLE old_daily_coverage;
            """)
        #expect(throws: RepositoryError.self) {
            try UsageRepository(databaseURL: nullableAccount.databaseURL)
        }
        let malformedV2 = try SQLiteFixture(
            userVersion: 2, extraSQL: "DROP TABLE daily_cost_coverage;"
        )
        #expect(throws: RepositoryError.self) {
            try UsageRepository(databaseURL: malformedV2.databaseURL)
        }
    }

    @Test("Malformed dynamic SQLite types fail safely")
    func malformedTypes() throws {
        let fixture = try SQLiteFixture(malformedToken: true)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL)
        defer { repository.close() }
        #expect(throws: RepositoryError.self) {
            try repository.activity(from: LocalDay("2026-07-02"), to: LocalDay("2026-07-02"))
        }
    }

    @Test("Canonical indexes and AUTOINCREMENT declarations are required")
    func physicalSchemaContracts() throws {
        let mutations = [
            "DROP INDEX change_log_record_revision_unique;",
            """
            DROP INDEX quota_snapshot_record_time;
            CREATE INDEX quota_snapshot_record_time ON quota_snapshots(record_id, observed_at ASC);
            """,
            """
            DROP INDEX quota_snapshot_provider_account_time;
            CREATE INDEX quota_snapshot_provider_account_time
              ON quota_snapshots(provider_id, account_ref, observed_at DESC, snapshot_id DESC)
              WHERE observed_at IS NOT NULL;
            """,
            """
            DROP INDEX quota_snapshot_record_time;
            DROP INDEX quota_snapshot_provider_account_time;
            ALTER TABLE quota_snapshots RENAME TO old_quota_snapshots;
            CREATE TABLE quota_snapshots(
              snapshot_id INTEGER PRIMARY KEY, record_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
              account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL,
              payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL);
            INSERT INTO quota_snapshots SELECT * FROM old_quota_snapshots;
            DROP TABLE old_quota_snapshots;
            CREATE INDEX quota_snapshot_record_time ON quota_snapshots(record_id, observed_at DESC);
            CREATE INDEX quota_snapshot_provider_account_time
              ON quota_snapshots(provider_id, account_ref, observed_at DESC, snapshot_id DESC);
            """,
            """
            DROP INDEX change_log_record_revision_unique;
            ALTER TABLE change_log RENAME TO old_change_log;
            CREATE TABLE change_log(
              change_seq INTEGER PRIMARY KEY, record_type TEXT NOT NULL,
              record_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
              changed_at TEXT NOT NULL, payload_json TEXT, payload_hash TEXT NOT NULL);
            INSERT INTO change_log SELECT * FROM old_change_log;
            DROP TABLE old_change_log;
            CREATE UNIQUE INDEX change_log_record_revision_unique
              ON change_log(record_type,record_id,revision);
            """,
        ]
        for mutation in mutations {
            let fixture = try SQLiteFixture(extraSQL: mutation)
            do {
                _ = try UsageRepository(databaseURL: fixture.databaseURL)
                Issue.record("non-canonical physical schema unexpectedly opened")
            } catch let error as RepositoryError {
                #expect(error == .incompatibleSchema)
            }
        }
    }

    @Test("AUTOINCREMENT text in CHECK literals or comments cannot spoof the target column")
    func autoincrementSpoofResistance() throws {
        let mutations = [
            """
            DROP INDEX quota_snapshot_record_time;
            DROP INDEX quota_snapshot_provider_account_time;
            ALTER TABLE quota_snapshots RENAME TO old_quota_snapshots;
            CREATE TABLE quota_snapshots(
              snapshot_id INTEGER PRIMARY KEY, record_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
              account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL,
              payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
              CHECK('SNAPSHOT_ID INTEGER PRIMARY KEY AUTOINCREMENT' != ''));
            INSERT INTO quota_snapshots SELECT * FROM old_quota_snapshots;
            DROP TABLE old_quota_snapshots;
            CREATE INDEX quota_snapshot_record_time ON quota_snapshots(record_id, observed_at DESC);
            CREATE INDEX quota_snapshot_provider_account_time
              ON quota_snapshots(provider_id, account_ref, observed_at DESC, snapshot_id DESC);
            """,
            """
            DROP INDEX change_log_record_revision_unique;
            ALTER TABLE change_log RENAME TO old_change_log;
            CREATE TABLE change_log(
              change_seq INTEGER PRIMARY KEY, record_type TEXT NOT NULL,
              record_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
              changed_at TEXT NOT NULL, payload_json TEXT,
              payload_hash TEXT NOT NULL /* CHANGE_SEQ INTEGER PRIMARY KEY AUTOINCREMENT */);
            INSERT INTO change_log SELECT * FROM old_change_log;
            DROP TABLE old_change_log;
            CREATE UNIQUE INDEX change_log_record_revision_unique
              ON change_log(record_type,record_id,revision);
            """,
        ]
        for mutation in mutations {
            let fixture = try SQLiteFixture(extraSQL: mutation)
            do {
                _ = try UsageRepository(databaseURL: fixture.databaseURL)
                Issue.record("AUTOINCREMENT decoy unexpectedly satisfied the schema contract")
            } catch let error as RepositoryError {
                #expect(error == .incompatibleSchema)
            }
        }
    }

    @Test("Repository reads do not mutate the ledger")
    func readOnly() throws {
        let fixture = try SQLiteFixture()
        let before = try Data(contentsOf: fixture.databaseURL)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        _ = try repository.dataRevision()
        _ = try repository.compactSummary(on: LocalDay("2026-07-02"))
        repository.close()
        let after = try Data(contentsOf: fixture.databaseURL)
        #expect(before == after)
    }

    @Test("A normally closed production WAL ledger is readable without mutation")
    func productionWALCompatibility() throws {
        let fixture = try PythonActivityStoreFixture()
        let before = try Data(contentsOf: fixture.databaseURL)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        #expect(try repository.dataRevision() == 2)
        let activity = try repository.activity(
            from: LocalDay("2026-07-02"), to: LocalDay("2026-07-02")
        )
        #expect(activity.records.map(\.totalTokens) == [9])
        #expect(activity.coverage.map(\.isCovered) == [true])
        repository.close()
        #expect(try Data(contentsOf: fixture.databaseURL) == before)
    }

    @Test("An active WAL writer refresh is visible, then the closed ledger reopens")
    func activeWALRefreshAndReopen() throws {
        let fixture = try PythonActiveActivityStoreFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        #expect(try repository.dataRevision() == 2)
        try fixture.refresh()
        #expect(try repository.dataRevision() == 4)
        try fixture.closeWriter()
        repository.close()

        let reopened = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        #expect(try reopened.dataRevision() == 4)
        let activity = try reopened.activity(
            from: LocalDay("2026-07-01"), to: LocalDay("2026-07-02")
        )
        #expect(activity.records.map(\.totalTokens) == [1, 2])
        reopened.close()
    }

    @Test("An immutable reader follows later active and checkpointed writer lifecycles")
    func immutableReaderFollowsWriterLifecycle() throws {
        let fixture = try PythonActivityStoreFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        #expect(try repository.dataRevision() == 2)
        let initialMain = try Data(contentsOf: fixture.databaseURL)

        let activeWriter = try PythonRestartedWriter(
            databaseURL: fixture.databaseURL, day: "2026-07-03", tokens: 3
        )
        #expect(FileManager.default.fileExists(atPath: fixture.databaseURL.path + "-wal"))
        #expect(try repository.dataRevision() == 4)
        try activeWriter.close()
        #expect(!FileManager.default.fileExists(atPath: fixture.databaseURL.path + "-wal"))
        #expect(try repository.dataRevision() == 4)
        let firstCheckpoint = try Data(contentsOf: fixture.databaseURL)
        #expect(firstCheckpoint != initialMain)

        let closedWriter = try PythonRestartedWriter(
            databaseURL: fixture.databaseURL, day: "2026-07-04", tokens: 4
        )
        try closedWriter.close()
        #expect(!FileManager.default.fileExists(atPath: fixture.databaseURL.path + "-wal"))
        #expect(try Data(contentsOf: fixture.databaseURL) != firstCheckpoint)
        #expect(try repository.dataRevision() == 6)
        let activity = try repository.activity(
            from: LocalDay("2026-07-02"), to: LocalDay("2026-07-04")
        )
        #expect(activity.records.map(\.totalTokens) == [9, 3, 4])
        repository.close()
    }

    @Test("A failed read releases its WAL handle and the repository remains reusable")
    func failedReadReleasesHandle() throws {
        let fixture = try PythonActivityStoreFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        let writer = try PythonRestartedWriter(
            databaseURL: fixture.databaseURL, day: "2026-07-03", tokens: 3, malformed: true
        )
        #expect(throws: RepositoryError.self) {
            try repository.activity(from: LocalDay("2026-07-03"), to: LocalDay("2026-07-03"))
        }
        #expect(try repository.dataRevision() == 4)
        try writer.close()
        #expect(!FileManager.default.fileExists(atPath: fixture.databaseURL.path + "-wal"))
        #expect(try repository.dataRevision() == 4)
        repository.close()
    }

    @Test("Repeated and concurrent reads serialize safely without mutating the ledger")
    func repeatedConcurrentReads() throws {
        let fixture = try SQLiteFixture()
        let before = try Data(contentsOf: fixture.databaseURL)
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        let box = SendableRepositoryBox(repository)
        let results = LockedRevisions()
        DispatchQueue.concurrentPerform(iterations: 24) { _ in
            results.append(Result { try box.repository.dataRevision() })
        }
        #expect(results.values.count == 24)
        #expect(results.values.allSatisfy { (try? $0.get()) == 1842 })
        for _ in 0..<10 { #expect(try repository.dataRevision() == 1842) }
        repository.close()
        #expect(try Data(contentsOf: fixture.databaseURL) == before)
    }

    @Test("Explicit close is permanent even if a writer later appears")
    func closeNeverResurrects() throws {
        let fixture = try PythonActivityStoreFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL, now: { now })
        repository.close()
        let writer = try PythonRestartedWriter(
            databaseURL: fixture.databaseURL, day: "2026-07-03", tokens: 3
        )
        #expect(throws: RepositoryError.self) { try repository.dataRevision() }
        try writer.close()
        #expect(throws: RepositoryError.self) { try repository.dataRevision() }
    }

    @Test("Closed repositories fail with a typed error")
    func deterministicClose() throws {
        let fixture = try SQLiteFixture()
        let repository = try UsageRepository(databaseURL: fixture.databaseURL)
        repository.close()
        #expect(throws: RepositoryError.self) { try repository.dataRevision() }
        repository.close()
    }
}

private final class SendableRepositoryBox: @unchecked Sendable {
    let repository: UsageRepository
    init(_ repository: UsageRepository) { self.repository = repository }
}

private final class LockedRevisions: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [Result<Int64, Error>] = []

    var values: [Result<Int64, Error>] {
        lock.withLock { storage }
    }

    func append(_ result: Result<Int64, Error>) {
        lock.withLock { storage.append(result) }
    }
}
