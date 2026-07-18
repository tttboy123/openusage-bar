import Foundation
import SQLite3

extension UsageRepository {
    func queryDailyUsage(
        _ database: OpaquePointer, sql: String, bindings: [String]
    ) throws -> [DailyUsage] {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            var rows: [DailyUsage] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let day = try LocalDay(requiredText(statement, 0))
                    let provider = try requiredText(statement, 1)
                    let account = try requiredText(statement, 2)
                    let model = try requiredText(statement, 3)
                    let source = try requiredText(statement, 16)
                    guard Self.isStableID(source) else {
                        throw RepositoryError.corruptData
                    }
                    rows.append(DailyUsage(
                        day: day, providerID: provider, accountRef: account, modelID: model,
                        inputTokens: try requiredInt64(statement, 4),
                        outputTokens: try requiredInt64(statement, 5),
                        cacheReadTokens: try requiredInt64(statement, 6),
                        cacheCreationTokens: try requiredInt64(statement, 7),
                        reasoningTokens: try optionalInt64(statement, 8),
                        totalTokens: try requiredInt64(statement, 9),
                        costAmount: try optionalText(statement, 10),
                        costCurrency: try optionalText(statement, 11),
                        costBasis: try optionalText(statement, 12),
                        quality: try requiredText(statement, 13),
                        importedAt: try requiredText(statement, 14),
                        revision: try requiredInt64(statement, 15),
                        recordID: "daily:\(day.rawValue):\(provider):\(account):\(model)",
                        sourceID: source
                    ))
                case SQLITE_DONE: return rows
                default: throw RepositoryError.corruptData
                }
            }
        }
    }

    func queryDailyCosts(
        _ database: OpaquePointer, sql: String, bindings: [String]
    ) throws -> [DailyCost] {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            var rows: [DailyCost] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let day = try LocalDay(requiredText(statement, 0))
                    let provider = try requiredText(statement, 1)
                    let account = try requiredText(statement, 2)
                    let costKind = try requiredText(statement, 3)
                    let currency = try requiredText(statement, 4)
                    let amount = try requiredText(statement, 5)
                    let basis = try requiredText(statement, 6)
                    let quality = try requiredText(statement, 7)
                    let importedAt = try requiredText(statement, 8)
                    let revision = try requiredInt64(statement, 9)
                    guard Self.isStableID(provider),
                          account.isEmpty || Self.isStableID(account),
                          costKind == "actual",
                          Self.isStableID(currency), currency == currency.uppercased(),
                          (3...8).contains(currency.utf8.count),
                          amount.utf8.count <= 128,
                          let decimal = Decimal(
                            string: amount, locale: Locale(identifier: "en_US_POSIX")
                          ), decimal >= 0,
                          Self.isStableID(basis), Self.isStableID(quality),
                          (try? parseTimestamp(importedAt)) != nil,
                          revision > 0
                    else { throw RepositoryError.corruptData }
                    rows.append(DailyCost(
                        day: day, providerID: provider, accountRef: account,
                        costKind: costKind, currency: currency, amount: amount,
                        basis: basis, quality: quality, importedAt: importedAt,
                        revision: revision,
                        recordID: "cost:\(day.rawValue):\(provider):\(account):\(costKind):\(currency)"
                    ))
                case SQLITE_DONE: return rows
                default: throw RepositoryError.corruptData
                }
            }
        }
    }

    func queryKnownScopes(_ database: OpaquePointer) throws -> Set<ProviderScope> {
        try queryScopes(
            database,
            sql: """
            SELECT DISTINCT provider_id,account_ref FROM daily_coverage
            UNION SELECT DISTINCT provider_id,account_ref FROM daily_model_usage
            UNION SELECT DISTINCT provider_id,'' FROM source_status WHERE source_id=?
            ORDER BY provider_id,account_ref
            """,
            bindings: ["openusage.daily"]
        )
    }

    func queryScopes(
        _ database: OpaquePointer, sql: String, bindings: [String]
    ) throws -> Set<ProviderScope> {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            var scopes = Set<ProviderScope>()
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    scopes.insert(ProviderScope(
                        providerID: try requiredText(statement, 0),
                        accountRef: try requiredText(statement, 1)
                    ))
                case SQLITE_DONE: return scopes
                default: throw RepositoryError.corruptData
                }
            }
        }
    }

    func queryCoveredKeys(
        _ database: OpaquePointer, start: LocalDay, end: LocalDay, providerIDs: Set<String>
    ) throws -> [CoverageKey: String] {
        let version = try scalarInt64(database, sql: "PRAGMA user_version")
        let sourceExpression = version >= 3 ? "source_id" : "'legacy'"
        var sql = "SELECT day,provider_id,account_ref,\(sourceExpression) FROM daily_coverage WHERE day>=? AND day<=?"
        var bindings = [start.rawValue, end.rawValue]
        if !providerIDs.isEmpty {
            sql += " AND provider_id IN (\(Self.placeholders(providerIDs.count)))"
            bindings.append(contentsOf: providerIDs.sorted())
        }
        sql += " ORDER BY day,provider_id,account_ref"
        return try withStatement(database, sql: sql, bindings: bindings) { statement in
            var keys: [CoverageKey: String] = [:]
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let provider = try requiredText(statement, 1)
                    let account = try requiredText(statement, 2)
                    let source = try requiredText(statement, 3)
                    guard Self.isStableID(provider),
                          account.isEmpty || Self.isStableID(account),
                          Self.isStableID(source)
                    else { throw RepositoryError.corruptData }
                    keys[CoverageKey(
                        day: try LocalDay(requiredText(statement, 0)),
                        scope: ProviderScope(
                            providerID: provider,
                            accountRef: account
                        )
                    )] = source
                case SQLITE_DONE: return keys
                default: throw RepositoryError.corruptData
                }
            }
        }
    }

    func queryCostCoveredKeys(
        _ database: OpaquePointer, sql: String, bindings: [String]
    ) throws -> Set<CoverageKey> {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            var keys = Set<CoverageKey>()
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let provider = try requiredText(statement, 1)
                    let account = try requiredText(statement, 2)
                    guard Self.isStableID(provider),
                          account.isEmpty || Self.isStableID(account)
                    else { throw RepositoryError.corruptData }
                    keys.insert(CoverageKey(
                        day: try LocalDay(requiredText(statement, 0)),
                        scope: ProviderScope(providerID: provider, accountRef: account)
                    ))
                case SQLITE_DONE: return keys
                default: throw RepositoryError.corruptData
                }
            }
        }
    }

    func queryCapacity(_ database: OpaquePointer, limit: Int?) throws -> [CapacityItem] {
        let version = try scalarInt64(database, sql: "PRAGMA user_version")
        let descriptors = Dictionary(
            uniqueKeysWithValues: try queryProviderInstances(database).map { ($0.providerID, $0.descriptor) }
        )
        let allRows: [CapacityItem] = try withStatement(
            database,
            sql: "SELECT * FROM quota_state ORDER BY provider_id,account_ref,quota_name,record_id"
        ) { statement in
            var rows: [CapacityItem] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let observed = try requiredText(statement, 1)
                    rows.append(CapacityItem(
                        recordID: try requiredText(statement, 0),
                        providerID: try requiredText(statement, 2),
                        accountRef: try requiredText(statement, 3),
                        quotaName: try requiredText(statement, 4),
                        unit: try requiredText(statement, 5),
                        used: try optionalText(statement, 6),
                        limit: try optionalText(statement, 7),
                        remaining: try optionalText(statement, 8),
                        remainingRatio: try optionalDouble(statement, 9),
                        resetsAt: try optionalText(statement, 10),
                        periodStart: try optionalText(statement, 11),
                        periodEnd: try optionalText(statement, 12),
                        observedAt: observed,
                        freshnessSeconds: max(0, Int64(now().timeIntervalSince(try parseTimestamp(observed)))),
                        state: try requiredText(statement, 13),
                        quality: try requiredText(statement, 14),
                        stale: try requiredBool(statement, 15),
                        revision: try requiredInt64(statement, 16),
                        sourceID: version >= 5
                            ? try stableQuotaIdentifier(requiredText(statement, 18)) : "current.quota",
                        quotaWindow: version >= 5
                            ? try stableQuotaIdentifier(requiredText(statement, 19)) : "subscription",
                        appliesTo: version >= 5
                            ? try quotaAppliesTo(
                                kind: requiredText(statement, 20),
                                modelIDsJSON: requiredText(statement, 21)
                            ) : .conservativeAccount,
                        providerDescriptor: descriptors[try requiredText(statement, 2)]
                    ))
                case SQLITE_DONE: return rows
                default: throw RepositoryError.corruptData
                }
            }
        }
        let grouped = Dictionary(grouping: allRows) { ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef) }
        let selected = grouped.values.compactMap { windows in windows.min(by: Self.windowOrder) }
        let sorted = CapacityViewModel.sorted(selected)
        return limit.map { Array(sorted.prefix($0)) } ?? sorted
    }

    func queryProviderInstances(_ database: OpaquePointer) throws -> [ProviderInstanceRecord] {
        let exists = try scalarInt64(
            database,
            sql: "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='provider_instances'"
        ) == 1
        guard exists else { return [] }
        let expected = [
            SchemaColumn.optional("provider_id", "TEXT", primaryKey: 1),
            .required("family_id", "TEXT"), .required("display_name", "TEXT"),
            .required("category", "TEXT"), .required("credential_source", "TEXT"),
            .required("source_kind", "TEXT"), .required("observed_at", "TEXT"),
            .required("revision", "INTEGER"), .required("payload_hash", "TEXT"),
        ]
        guard (try? queryColumnSignature(database, table: "provider_instances")) == expected else {
            return []
        }
        do {
            return try withStatement(
                database,
                sql: "SELECT provider_id,family_id,display_name,category,credential_source,source_kind,observed_at,revision FROM provider_instances ORDER BY provider_id"
            ) { statement in
                var rows: [ProviderInstanceRecord] = []
                while true {
                    switch sqlite3_step(statement) {
                    case SQLITE_ROW:
                        let providerID = try requiredText(statement, 0)
                        let familyID = try requiredText(statement, 1)
                        let displayName = try requiredText(statement, 2)
                        let rawCategory = try requiredText(statement, 3)
                        let credentialSource = try requiredText(statement, 4)
                        let sourceKind = try requiredText(statement, 5)
                        let observedAt = try requiredText(statement, 6)
                        let revision = try requiredInt64(statement, 7)
                        guard Self.isStableID(providerID), Self.isStableID(familyID),
                              Self.isSafeDisplayName(displayName),
                              Self.isStableID(credentialSource), Self.isStableID(sourceKind),
                              revision >= 0, (try? parseTimestamp(observedAt)) != nil,
                              let category = Self.providerCategory(rawCategory),
                              Self.isCanonicalIdentityBinding(
                                  providerID: providerID, familyID: familyID,
                                  category: category, credentialSource: credentialSource,
                                  sourceKind: sourceKind
                              )
                        else { throw RepositoryError.corruptData }
                        rows.append(ProviderInstanceRecord(
                            providerID: providerID, familyID: familyID, displayName: displayName,
                            category: category, credentialSource: credentialSource,
                            sourceKind: sourceKind, observedAt: observedAt, revision: revision
                        ))
                    case SQLITE_DONE: return rows
                    default: throw RepositoryError.corruptData
                    }
                }
            }
        } catch {
            return []
        }
    }

    func querySourceHealth(_ database: OpaquePointer, revision: Int64) throws -> SourceHealth {
        let sources: [SourceHealthItem] = try withStatement(
            database,
            sql: "SELECT * FROM source_status ORDER BY provider_id,source_id"
        ) { statement in
            var rows: [SourceHealthItem] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    let state = try requiredText(statement, 2)
                    let staleAt = try optionalText(statement, 5)
                    let isElapsed = try staleAt.map { try parseTimestamp($0) <= now() } ?? false
                    rows.append(SourceHealthItem(
                        providerID: try requiredText(statement, 0),
                        sourceID: try requiredText(statement, 1),
                        state: state,
                        effectiveState: isElapsed ? "stale" : state,
                        lastAttemptAt: try requiredText(statement, 3),
                        lastSuccessAt: try optionalText(statement, 4),
                        staleAt: staleAt,
                        errorCode: try optionalText(statement, 6)
                    ))
                case SQLITE_DONE: return rows
                default: throw RepositoryError.corruptData
                }
            }
        }
        return SourceHealth(
            sources: sources,
            hasIssues: sources.contains { $0.effectiveState != "ok" },
            revision: revision
        )
    }

}

struct CoverageKey: Hashable {
    let day: LocalDay
    let scope: ProviderScope
}
