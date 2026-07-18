import Foundation
import SQLite3

public enum RepositoryError: Error, Sendable, Hashable, LocalizedError, CustomStringConvertible {
    case databaseUnavailable
    case incompatibleSchema
    case invalidRequest
    case corruptData
    case closed

    public var errorDescription: String? { description }

    public var description: String {
        switch self {
        case .databaseUnavailable: "Usage data is unavailable."
        case .incompatibleSchema: "Usage data uses an unsupported schema."
        case .invalidRequest: "The usage request is invalid."
        case .corruptData: "Usage data could not be read safely."
        case .closed: "The usage data reader is closed."
        }
    }
}

/// A read-only repository over canonical version-one and version-two ledgers.
///
/// The reference type intentionally does not conform to `Sendable`. Callers should
/// keep it in one actor/domain. Internally, a lock serializes access. Every read
/// owns a short-lived SQLite FULLMUTEX handle so collector checkpoints are never
/// pinned; deterministic `close()` permanently disables future reads.
public final class UsageRepository {
    private let lock = NSLock()
    private let now: () -> Date
    private let databaseURL: URL
    private var isClosed = false

    public init(databaseURL: URL, now: @escaping () -> Date = Date.init) throws {
        self.now = now
        self.databaseURL = databaseURL
        guard FileManager.default.fileExists(atPath: databaseURL.path) else {
            throw RepositoryError.databaseUnavailable
        }
        let opened = try openValidatedDatabase()
        sqlite3_close_v2(opened)
    }

    private func openValidatedDatabase() throws -> OpaquePointer {
        let opened = try Self.openDatabase(databaseURL, immutable: false)
        do {
            try validateSchema(opened)
            return opened
        } catch let error as RepositoryError {
            sqlite3_close_v2(opened)
            guard error == .corruptData, !Self.hasWALSidecars(databaseURL) else { throw error }

            // A normally closed WAL database is checkpointed before SQLite removes
            // its sidecars, but Apple SQLite cannot read that WAL-header main file
            // through a plain read-only handle. Immutable mode is safe only after
            // both sidecars are absent. Active writers retain the normal WAL path,
            // so each short transaction continues to observe refreshed revisions.
            let checkpointed = try Self.openDatabase(databaseURL, immutable: true)
            do {
                try validateSchema(checkpointed)
                return checkpointed
            } catch let fallbackError as RepositoryError {
                sqlite3_close_v2(checkpointed)
                throw fallbackError
            } catch {
                sqlite3_close_v2(checkpointed)
                throw RepositoryError.incompatibleSchema
            }
        } catch {
            sqlite3_close_v2(opened)
            throw RepositoryError.incompatibleSchema
        }
    }

    deinit { close() }

    public func close() {
        lock.lock()
        defer { lock.unlock() }
        isClosed = true
    }

    public func dataRevision() throws -> Int64 {
        try withReadTransaction { database in try currentRevision(database) }
    }

    public func compactSummary(on day: LocalDay) throws -> CompactSummary {
        try withReadTransaction { database in
            let revision = try currentRevision(database)
            let expectedScopes = try queryKnownScopes(database)
            let recordScopes = try queryScopes(
                database,
                sql: "SELECT DISTINCT provider_id,account_ref FROM daily_model_usage WHERE day=? ORDER BY provider_id,account_ref",
                bindings: [day.rawValue]
            )
            let coveredScopes = Set(try queryCoveredKeys(
                database, start: day, end: day, providerIDs: []
            ).keys.map(\.scope)).union(recordScopes)
            let isTodayComplete = !expectedScopes.isEmpty && expectedScopes.isSubset(of: coveredScopes)
            let todayTokens: Int64? = coveredScopes.isEmpty
                ? nil
                : try scalarInt64(
                    database,
                    sql: "SELECT COALESCE(SUM(total_tokens),0) FROM daily_model_usage WHERE day=?",
                    bindings: [day.rawValue]
                )
            let capacity = try queryCapacity(database, limit: nil)
            let health = try querySourceHealth(database, revision: revision)
            let updatedAt = try optionalScalarText(
                database,
                sql: """
                SELECT MAX(value) FROM (
                  SELECT MAX(imported_at) AS value FROM daily_model_usage
                  UNION ALL SELECT MAX(observed_at) FROM quota_state
                  UNION ALL SELECT MAX(last_attempt_at) FROM source_status
                )
                """
            )
            return CompactSummary(
                todayTokens: todayTokens,
                isTodayComplete: isTodayComplete,
                capacity: capacity,
                updatedAt: updatedAt,
                hasHealthIssues: health.hasIssues || capacity.contains { $0.stale || $0.state != "ok" },
                revision: revision
            )
        }
    }

    public func capacity(limit: Int?) throws -> [CapacityItem] {
        if let limit, !(1...1000).contains(limit) { throw RepositoryError.invalidRequest }
        return try withReadTransaction { database in
            let rows = try queryCapacity(database, limit: limit)
            _ = try currentRevision(database)
            return rows
        }
    }

    /// Reads the optional provider identity ledger without creating or migrating it.
    /// Older ledgers and partial forward-compatible tables safely expose no instances.
    public func providerInstances() throws -> [ProviderInstanceRecord] {
        try withReadTransaction { database in
            try queryProviderInstances(database)
        }
    }

    public func activity(
        from start: LocalDay,
        to end: LocalDay,
        providerIDs: Set<String> = [],
        modelIDs: Set<String> = []
    ) throws -> ActivityDataset {
        guard start <= end,
              let startDate = start.utcDate,
              let endDate = end.utcDate,
              let rangeDays = Calendar.utc.dateComponents([.day], from: startDate, to: endDate).day,
              rangeDays + 1 <= 731,
              providerIDs.allSatisfy(Self.isStableID),
              modelIDs.allSatisfy(Self.isStableID)
        else { throw RepositoryError.invalidRequest }

        return try withReadTransaction { database in
            let version = try scalarInt64(database, sql: "PRAGMA user_version")
            var clauses = ["day>=?", "day<=?"]
            var bindings = [start.rawValue, end.rawValue]
            if !providerIDs.isEmpty {
                clauses.append("provider_id IN (\(Self.placeholders(providerIDs.count)))")
                bindings.append(contentsOf: providerIDs.sorted())
            }
            if !modelIDs.isEmpty {
                clauses.append("model_id IN (\(Self.placeholders(modelIDs.count)))")
                bindings.append(contentsOf: modelIDs.sorted())
            }
            let records = try queryDailyUsage(
                database,
                sql: """
                SELECT day,provider_id,account_ref,model_id,input_tokens,output_tokens,
                  cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,
                  cost_amount,cost_currency,cost_basis,quality,imported_at,revision,
                  \(version >= 3 ? "source_id" : "'legacy'")
                FROM daily_model_usage WHERE \(clauses.joined(separator: " AND "))
                ORDER BY day,provider_id,account_ref,model_id
                """,
                bindings: bindings
            )
            var scopes = try queryKnownScopes(database)
            if !providerIDs.isEmpty {
                scopes = Set(scopes.filter { providerIDs.contains($0.providerID) })
                for providerID in providerIDs where !scopes.contains(where: { $0.providerID == providerID }) {
                    scopes.insert(ProviderScope(providerID: providerID, accountRef: ""))
                }
            }
            let covered = try queryCoveredKeys(
                database, start: start, end: end,
                providerIDs: providerIDs
            )
            var coverage: [CoverageDay] = []
            var current = startDate
            while current <= endDate {
                guard let day = LocalDay(date: current) else { throw RepositoryError.corruptData }
                for scope in scopes.sorted(by: Self.scopeOrder) {
                    coverage.append(CoverageDay(
                        day: day, providerID: scope.providerID, accountRef: scope.accountRef,
                        isCovered: covered[CoverageKey(day: day, scope: scope)] != nil,
                        sourceID: covered[CoverageKey(day: day, scope: scope)]
                    ))
                }
                guard let next = Calendar.utc.date(byAdding: .day, value: 1, to: current) else {
                    throw RepositoryError.corruptData
                }
                current = next
            }
            return ActivityDataset(
                records: records, coverage: coverage, knownScopes: scopes,
                revision: try currentRevision(database)
            )
        }
    }

    public func dailyCosts(
        from start: LocalDay,
        to end: LocalDay,
        providerIDs: Set<String> = [],
        currencies: Set<String> = []
    ) throws -> DailyCostDataset {
        guard start <= end,
              let startDate = start.utcDate,
              let endDate = end.utcDate,
              let rangeDays = Calendar.utc.dateComponents(
                [.day], from: startDate, to: endDate
              ).day,
              rangeDays + 1 <= 731,
              providerIDs.allSatisfy(Self.isStableID),
              currencies.allSatisfy({
                  Self.isStableID($0) && $0 == $0.uppercased()
              })
        else { throw RepositoryError.invalidRequest }

        return try withReadTransaction { database in
            let revision = try currentRevision(database)
            let version = try scalarInt64(database, sql: "PRAGMA user_version")
            guard version >= 2 else {
                return DailyCostDataset(
                    records: [], coverage: [], knownScopes: [], revision: revision
                )
            }
            var clauses = ["day>=?", "day<=?"]
            var bindings = [start.rawValue, end.rawValue]
            if !providerIDs.isEmpty {
                clauses.append("provider_id IN (\(Self.placeholders(providerIDs.count)))")
                bindings.append(contentsOf: providerIDs.sorted())
            }
            if !currencies.isEmpty {
                clauses.append("currency IN (\(Self.placeholders(currencies.count)))")
                bindings.append(contentsOf: currencies.sorted())
            }
            let records = try queryDailyCosts(
                database,
                sql: "SELECT * FROM daily_costs WHERE \(clauses.joined(separator: " AND ")) ORDER BY day,provider_id,account_ref,cost_kind,currency",
                bindings: bindings
            )
            var scopes = try queryScopes(
                database,
                sql: """
                SELECT DISTINCT provider_id,account_ref FROM daily_cost_coverage
                UNION SELECT DISTINCT provider_id,account_ref FROM daily_costs
                ORDER BY provider_id,account_ref
                """,
                bindings: []
            )
            if !providerIDs.isEmpty {
                scopes = Set(scopes.filter { providerIDs.contains($0.providerID) })
                for providerID in providerIDs
                where !scopes.contains(where: { $0.providerID == providerID }) {
                    scopes.insert(ProviderScope(providerID: providerID, accountRef: ""))
                }
            }
            var coverageSQL = "SELECT day,provider_id,account_ref FROM daily_cost_coverage WHERE day>=? AND day<=?"
            var coverageBindings = [start.rawValue, end.rawValue]
            if !providerIDs.isEmpty {
                coverageSQL += " AND provider_id IN (\(Self.placeholders(providerIDs.count)))"
                coverageBindings.append(contentsOf: providerIDs.sorted())
            }
            coverageSQL += " ORDER BY day,provider_id,account_ref"
            let covered = try queryCostCoveredKeys(
                database, sql: coverageSQL, bindings: coverageBindings
            )
            var coverage: [CostCoverageDay] = []
            var current = startDate
            while current <= endDate {
                guard let day = LocalDay(date: current) else {
                    throw RepositoryError.corruptData
                }
                for scope in scopes.sorted(by: Self.scopeOrder) {
                    coverage.append(CostCoverageDay(
                        day: day, providerID: scope.providerID,
                        accountRef: scope.accountRef,
                        isCovered: covered.contains(CoverageKey(day: day, scope: scope))
                    ))
                }
                guard let next = Calendar.utc.date(byAdding: .day, value: 1, to: current) else {
                    throw RepositoryError.corruptData
                }
                current = next
            }
            return DailyCostDataset(
                records: records, coverage: coverage,
                knownScopes: scopes, revision: revision
            )
        }
    }

    public func sourceHealth() throws -> SourceHealth {
        try withReadTransaction { database in
            try querySourceHealth(database, revision: currentRevision(database))
        }
    }

    public func quotaHistory(
        providerID: String? = nil,
        accountRef: String? = nil,
        limit: Int = 1_000
    ) throws -> [QuotaHistoryItem] {
        guard (1...1_000).contains(limit),
              providerID.map(Self.isStableID) ?? true,
              accountRef.map(Self.isStableID) ?? true
        else { throw RepositoryError.invalidRequest }

        return try withReadTransaction { database in
            let version = try scalarInt64(database, sql: "PRAGMA user_version")
            let scopeColumns = version >= 5
                ? ",source_id,quota_window,applies_to_kind,applies_to_model_ids"
                : ",'current.quota' AS source_id,'subscription' AS quota_window,"
                    + "'account' AS applies_to_kind,'[]' AS applies_to_model_ids"
            var clauses: [String] = []
            var bindings: [String] = []
            if let providerID {
                clauses.append("provider_id=?")
                bindings.append(providerID)
            }
            if let accountRef {
                clauses.append("account_ref=?")
                bindings.append(accountRef)
            }
            let whereClause = clauses.isEmpty ? "" : " WHERE " + clauses.joined(separator: " AND ")
            let sql = """
                SELECT * FROM (
                  SELECT snapshot_id,record_id,observed_at,provider_id,account_ref,quota_name,payload_json\(scopeColumns)
                  FROM quota_snapshots\(whereClause)
                  ORDER BY observed_at DESC,snapshot_id DESC LIMIT \(limit)
                ) ORDER BY observed_at,snapshot_id
                """
            let rows: [QuotaHistoryItem] = try withStatement(database, sql: sql, bindings: bindings) { statement in
                var result: [QuotaHistoryItem] = []
                while true {
                    switch sqlite3_step(statement) {
                    case SQLITE_ROW:
                        let payload = try quotaDisplayPayload(try requiredText(statement, 6))
                        result.append(QuotaHistoryItem(
                            snapshotID: try requiredInt64(statement, 0),
                            recordID: try requiredText(statement, 1),
                            observedAt: try requiredText(statement, 2),
                            providerID: try requiredText(statement, 3),
                            accountRef: try requiredText(statement, 4),
                            quotaName: try requiredText(statement, 5),
                            remainingRatio: payload.remainingRatio,
                            resetsAt: payload.resetsAt,
                            state: payload.state,
                            stale: payload.stale,
                            sourceID: try stableQuotaIdentifier(requiredText(statement, 7)),
                            quotaWindow: try stableQuotaIdentifier(requiredText(statement, 8)),
                            appliesTo: try quotaAppliesTo(
                                kind: requiredText(statement, 9),
                                modelIDsJSON: requiredText(statement, 10)
                            )
                        ))
                    case SQLITE_DONE: return result
                    default: throw RepositoryError.corruptData
                    }
                }
            }
            _ = try currentRevision(database)
            return rows
        }
    }

    public func quotaHistory(
        providerIDs: Set<String>,
        observedAtOrAfter: String,
        observedBefore: String,
        perSeriesLimit: Int = 100
    ) throws -> QuotaHistoryResult {
        guard providerIDs.count <= 256,
              providerIDs.allSatisfy(Self.isStableID),
              (1...1_000).contains(perSeriesLimit),
              let start = try? parseTimestamp(observedAtOrAfter),
              let end = try? parseTimestamp(observedBefore),
              start < end
        else { throw RepositoryError.invalidRequest }
        guard !providerIDs.isEmpty else {
            return QuotaHistoryResult(
                items: [], truncatedSeriesIDs: [], perSeriesLimit: perSeriesLimit
            )
        }

        return try withReadTransaction { database in
            let version = try scalarInt64(database, sql: "PRAGMA user_version")
            let scopeColumns = version >= 5
                ? ",source_id,quota_window,applies_to_kind,applies_to_model_ids"
                : ",'current.quota' AS source_id,'subscription' AS quota_window,"
                    + "'account' AS applies_to_kind,'[]' AS applies_to_model_ids"
            let orderedProviders = providerIDs.sorted()
            let bindings = orderedProviders + [
                Self.canonicalTimestamp(start), Self.canonicalTimestamp(end),
            ]
            let sql = """
                WITH ranked AS (
                  SELECT snapshot_id,record_id,observed_at,provider_id,account_ref,quota_name,payload_json\(scopeColumns),
                    ROW_NUMBER() OVER (
                      PARTITION BY provider_id,account_ref,record_id,quota_name
                      ORDER BY observed_at DESC,snapshot_id DESC
                    ) AS series_rank
                  FROM quota_snapshots
                  WHERE provider_id IN (\(Self.placeholders(orderedProviders.count)))
                    AND observed_at>=? AND observed_at<?
                )
                SELECT snapshot_id,record_id,observed_at,provider_id,account_ref,quota_name,payload_json,
                  source_id,quota_window,applies_to_kind,applies_to_model_ids,series_rank
                FROM ranked WHERE series_rank<=\(perSeriesLimit + 1)
                ORDER BY observed_at,snapshot_id
                """
            let result: QuotaHistoryResult = try withStatement(database, sql: sql, bindings: bindings) { statement in
                var items: [QuotaHistoryItem] = []
                var truncatedSeriesIDs: Set<String> = []
                while true {
                    switch sqlite3_step(statement) {
                    case SQLITE_ROW:
                        let rank = try requiredInt64(statement, 11)
                        if rank > perSeriesLimit {
                            truncatedSeriesIDs.insert([
                                try requiredText(statement, 3),
                                try requiredText(statement, 4),
                                try requiredText(statement, 1),
                                try requiredText(statement, 5),
                            ].joined(separator: "|"))
                            continue
                        }
                        let payload = try quotaDisplayPayload(try requiredText(statement, 6))
                        items.append(QuotaHistoryItem(
                            snapshotID: try requiredInt64(statement, 0),
                            recordID: try requiredText(statement, 1),
                            observedAt: try requiredText(statement, 2),
                            providerID: try requiredText(statement, 3),
                            accountRef: try requiredText(statement, 4),
                            quotaName: try requiredText(statement, 5),
                            remainingRatio: payload.remainingRatio,
                            resetsAt: payload.resetsAt,
                            state: payload.state,
                            stale: payload.stale,
                            sourceID: try stableQuotaIdentifier(requiredText(statement, 7)),
                            quotaWindow: try stableQuotaIdentifier(requiredText(statement, 8)),
                            appliesTo: try quotaAppliesTo(
                                kind: requiredText(statement, 9),
                                modelIDsJSON: requiredText(statement, 10)
                            )
                        ))
                    case SQLITE_DONE:
                        return QuotaHistoryResult(
                            items: items, truncatedSeriesIDs: truncatedSeriesIDs,
                            perSeriesLimit: perSeriesLimit
                        )
                    default: throw RepositoryError.corruptData
                    }
                }
            }
            _ = try currentRevision(database)
            return result
        }
    }

    private func quotaDisplayPayload(
        _ json: String
    ) throws -> (remainingRatio: Double?, resetsAt: String?, state: String, stale: Bool) {
        guard let data = json.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any],
              let state = payload["state"] as? String,
              let stale = payload["stale"] as? Bool
        else { throw RepositoryError.corruptData }
        let remainingRatio: Double?
        if payload["remaining_ratio"] is NSNull || payload["remaining_ratio"] == nil {
            remainingRatio = nil
        } else if let value = payload["remaining_ratio"] as? NSNumber,
                  CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite,
                  (0...1).contains(value.doubleValue) {
            remainingRatio = value.doubleValue
        } else {
            throw RepositoryError.corruptData
        }
        let resetsAt: String?
        if payload["resets_at"] is NSNull || payload["resets_at"] == nil {
            resetsAt = nil
        } else if let value = payload["resets_at"] as? String,
                  value.utf8.count <= 64,
                  (try? parseTimestamp(value)) != nil {
            resetsAt = value
        } else {
            throw RepositoryError.corruptData
        }
        return (remainingRatio, resetsAt, state, stale)
    }

    private func quotaAppliesTo(
        kind: String, modelIDsJSON: String
    ) throws -> QuotaAppliesTo {
        guard let scopeKind = QuotaScopeKind(rawValue: kind),
              let data = modelIDsJSON.data(using: .utf8),
              let modelIDs = try JSONSerialization.jsonObject(with: data) as? [String],
              modelIDs.allSatisfy(Self.isStableID),
              modelIDs == modelIDs.sorted(),
              Set(modelIDs).count == modelIDs.count,
              (scopeKind == .model ? !modelIDs.isEmpty : modelIDs.isEmpty)
        else { throw RepositoryError.corruptData }
        return QuotaAppliesTo(kind: scopeKind, modelIDs: modelIDs)
    }

    private func stableQuotaIdentifier(_ value: String) throws -> String {
        guard Self.isStableID(value) else { throw RepositoryError.corruptData }
        return value
    }

    private func validateSchema(_ database: OpaquePointer) throws {
        let version = try scalarInt64(database, sql: "PRAGMA user_version")
        guard (1...5).contains(version) else { throw RepositoryError.incompatibleSchema }
        let expected = GeneratedActivitySchema.expectedTables(version: version)
        for (table, signature) in expected {
            let actual = try queryColumnSignature(database, table: table)
            guard actual == signature else {
                throw RepositoryError.incompatibleSchema
            }
        }
        try validateIndexes(database, includeCosts: version >= 2)
        try validateAutoincrement(database)
    }

    private func validateIndexes(
        _ database: OpaquePointer, includeCosts: Bool
    ) throws {
        let expected = GeneratedActivitySchema.expectedIndexes(includeCosts: includeCosts)
        for contract in expected {
            let properties: (unique: Bool, partial: Bool) = try withStatement(
                database,
                sql: "SELECT \"unique\",partial FROM pragma_index_list(?) WHERE name=?",
                bindings: [contract.table, contract.name]
            ) { statement in
                guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.incompatibleSchema }
                let value = (try requiredBool(statement, 0), try requiredBool(statement, 1))
                guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.incompatibleSchema }
                return value
            }
            guard properties.unique == contract.unique, properties.partial == contract.partial else {
                throw RepositoryError.incompatibleSchema
            }
            let terms: [IndexTerm] = try withStatement(
                database,
                sql: "SELECT name,desc FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno",
                bindings: [contract.name]
            ) { statement in
                var values: [IndexTerm] = []
                while true {
                    switch sqlite3_step(statement) {
                    case SQLITE_ROW:
                        values.append(IndexTerm(
                            name: try requiredText(statement, 0),
                            descending: try requiredBool(statement, 1)
                        ))
                    case SQLITE_DONE: return values
                    default: throw RepositoryError.incompatibleSchema
                    }
                }
            }
            guard terms == contract.terms else { throw RepositoryError.incompatibleSchema }
        }
    }

    private func validateAutoincrement(_ database: OpaquePointer) throws {
        let contracts = GeneratedActivitySchema.autoincrementColumns
        for (table, column) in contracts {
            let definition = try withStatement(
                database,
                sql: "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                bindings: ["table", table]
            ) { statement in
                guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.incompatibleSchema }
                let value = try requiredText(statement, 0)
                guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.incompatibleSchema }
                return value
            }
            guard SQLiteDDLParser.hasCanonicalAutoincrementColumn(
                in: definition, column: column
            ) else { throw RepositoryError.incompatibleSchema }
        }
    }

    private func queryColumnSignature(_ database: OpaquePointer, table: String) throws -> [SchemaColumn] {
        try withStatement(
            database,
            sql: "SELECT name,type,\"notnull\",dflt_value,pk FROM pragma_table_info(?) ORDER BY cid",
            bindings: [table]
        ) { statement in
            var result: [SchemaColumn] = []
            while true {
                switch sqlite3_step(statement) {
                case SQLITE_ROW:
                    result.append(SchemaColumn(
                        name: try requiredText(statement, 0),
                        type: try requiredText(statement, 1).uppercased(),
                        required: try requiredBool(statement, 2),
                        defaultValue: try optionalText(statement, 3),
                        primaryKey: Int(try requiredInt64(statement, 4))
                    ))
                case SQLITE_DONE: return result
                default: throw RepositoryError.incompatibleSchema
                }
            }
        }
    }

    private func queryDailyUsage(
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

    private func queryDailyCosts(
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

    private func queryKnownScopes(_ database: OpaquePointer) throws -> Set<ProviderScope> {
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

    private func queryScopes(
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

    private func queryCoveredKeys(
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

    private func queryCostCoveredKeys(
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

    private func queryCapacity(_ database: OpaquePointer, limit: Int?) throws -> [CapacityItem] {
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

    private func queryProviderInstances(_ database: OpaquePointer) throws -> [ProviderInstanceRecord] {
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

    private func querySourceHealth(_ database: OpaquePointer, revision: Int64) throws -> SourceHealth {
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

    private func withReadTransaction<T>(_ body: (OpaquePointer) throws -> T) throws -> T {
        lock.lock()
        defer { lock.unlock() }
        guard !isClosed else { throw RepositoryError.closed }
        let handle = try openValidatedDatabase()
        defer { sqlite3_close_v2(handle) }
        guard sqlite3_exec(handle, "BEGIN DEFERRED", nil, nil, nil) == SQLITE_OK else {
            throw RepositoryError.corruptData
        }
        do {
            let value = try body(handle)
            guard sqlite3_exec(handle, "COMMIT", nil, nil, nil) == SQLITE_OK else {
                sqlite3_exec(handle, "ROLLBACK", nil, nil, nil)
                throw RepositoryError.corruptData
            }
            return value
        } catch let error as RepositoryError {
            sqlite3_exec(handle, "ROLLBACK", nil, nil, nil)
            throw error
        } catch {
            sqlite3_exec(handle, "ROLLBACK", nil, nil, nil)
            throw RepositoryError.corruptData
        }
    }

    private func withStatement<T>(
        _ database: OpaquePointer,
        sql: String,
        bindings: [String] = [],
        body: (OpaquePointer) throws -> T
    ) throws -> T {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw RepositoryError.corruptData
        }
        defer { sqlite3_finalize(statement) }
        for (index, value) in bindings.enumerated() {
            guard sqlite3_bind_text(statement, Int32(index + 1), value, -1, Self.sqliteTransient) == SQLITE_OK else {
                throw RepositoryError.corruptData
            }
        }
        return try body(statement)
    }

    private func currentRevision(_ database: OpaquePointer) throws -> Int64 {
        try scalarInt64(database, sql: "SELECT COALESCE(MAX(change_seq),0) FROM change_log")
    }

    private func scalarInt64(
        _ database: OpaquePointer, sql: String, bindings: [String] = []
    ) throws -> Int64 {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.corruptData }
            let value = try requiredInt64(statement, 0)
            guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.corruptData }
            return value
        }
    }

    private func optionalScalarText(_ database: OpaquePointer, sql: String) throws -> String? {
        try withStatement(database, sql: sql) { statement in
            guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.corruptData }
            let value = try optionalText(statement, 0)
            guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.corruptData }
            return value
        }
    }

    private func requiredText(_ statement: OpaquePointer, _ index: Int32) throws -> String {
        guard sqlite3_column_type(statement, index) == SQLITE_TEXT,
              let pointer = sqlite3_column_text(statement, index)
        else { throw RepositoryError.corruptData }
        return String(cString: pointer)
    }

    private func optionalText(_ statement: OpaquePointer, _ index: Int32) throws -> String? {
        if sqlite3_column_type(statement, index) == SQLITE_NULL { return nil }
        return try requiredText(statement, index)
    }

    private func requiredInt64(_ statement: OpaquePointer, _ index: Int32) throws -> Int64 {
        guard sqlite3_column_type(statement, index) == SQLITE_INTEGER else {
            throw RepositoryError.corruptData
        }
        return sqlite3_column_int64(statement, index)
    }

    private func optionalInt64(_ statement: OpaquePointer, _ index: Int32) throws -> Int64? {
        if sqlite3_column_type(statement, index) == SQLITE_NULL { return nil }
        return try requiredInt64(statement, index)
    }

    private func optionalDouble(_ statement: OpaquePointer, _ index: Int32) throws -> Double? {
        let type = sqlite3_column_type(statement, index)
        if type == SQLITE_NULL { return nil }
        guard type == SQLITE_FLOAT || type == SQLITE_INTEGER else { throw RepositoryError.corruptData }
        let value = sqlite3_column_double(statement, index)
        guard value.isFinite else { throw RepositoryError.corruptData }
        return value
    }

    private func requiredBool(_ statement: OpaquePointer, _ index: Int32) throws -> Bool {
        let value = try requiredInt64(statement, index)
        guard value == 0 || value == 1 else { throw RepositoryError.corruptData }
        return value == 1
    }

    private func parseTimestamp(_ value: String) throws -> Date {
        let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        if let date = try? Date(value, strategy: fractional) { return date }
        if let date = try? Date(value, strategy: .iso8601) { return date }
        throw RepositoryError.corruptData
    }

    private static func canonicalTimestamp(_ date: Date) -> String {
        var wholeSeconds = floor(date.timeIntervalSince1970)
        var microseconds = Int(
            ((date.timeIntervalSince1970 - wholeSeconds) * 1_000_000).rounded()
        )
        if microseconds == 1_000_000 {
            wholeSeconds += 1
            microseconds = 0
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let seconds = formatter.string(from: Date(timeIntervalSince1970: wholeSeconds))
        return "\(seconds).\(String(format: "%06d", microseconds))Z"
    }

    private static let sqliteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    private static let stableID = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    private static func isStableID(_ value: String) -> Bool {
        stableID.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    private static func isSafeDisplayName(_ value: String) -> Bool {
        guard !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              value == value.trimmingCharacters(in: .whitespacesAndNewlines),
              value.count <= 128,
              !value.unicodeScalars.contains(where: { $0.value < 32 })
        else { return false }
        guard !privateLabelPatterns.contains(where: { matches($0, value) }) else { return false }
        return matches(in: value, regex: opaqueKeyPattern).allSatisfy { match in
            let candidate = (value as NSString).substring(with: match.range)
            return !(candidate.contains(where: \Character.isLetter)
                && candidate.contains(where: \Character.isNumber))
        }
    }

    private static func isCanonicalIdentityBinding(
        providerID: String, familyID: String, category: ProviderProductCategory,
        credentialSource: String, sourceKind: String
    ) -> Bool {
        let known = GeneratedProviderCatalog.families
        let source = ProviderIdentitySource(
            credentialSource: credentialSource, sourceKind: sourceKind
        )
        if let family = known[familyID] {
            if known[providerID] != nil && providerID != familyID { return false }
            return category == family.category && family.acceptedIdentitySources.contains(source)
        }
        guard providerID == familyID else { return false }
        if credentialSource == "openusage", sourceKind == "openusage" {
            return category == .api
        }
        return credentialSource == "api_key" && sourceKind == "generic_https"
            && (category == .api || category == .subscription)
    }

    private static func matches(_ regex: NSRegularExpression, _ value: String) -> Bool {
        regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    private static func matches(
        in value: String, regex: NSRegularExpression
    ) -> [NSTextCheckingResult] {
        regex.matches(in: value, range: NSRange(value.startIndex..., in: value))
    }

    private static func privateLabelRegex(_ pattern: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
    }

    private static let privateLabelPatterns = [
        privateLabelRegex(#"[A-Z0-9._%+-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?"#),
        privateLabelRegex(#"(?<![A-Z0-9._-])(?:/(?!\s)[^\s()]+|~[/\\](?!\s)[^\s()]+|[A-Z]:[/\\](?!\s)[^\s()]+)"#),
        privateLabelRegex(#"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|cookie|access[_ -]?token|refresh[_ -]?token|token|username|user|account(?:_email)?|email|attributes?|raw_attrs?|response_body|body)\s*[:=]"#),
        privateLabelRegex(#"(?:bearer\s+)[A-Z0-9._~+/=-]+|eyJ[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}|(?:sk-|ghp_|xox[baprs]-|AIza)[A-Z0-9_-]{16,}"#),
        privateLabelRegex(#"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|cookie|access[_ -]?token|refresh[_ -]?token|token)\s+[A-Z0-9._~+/=-]{20,}(?=\s|$)"#),
        privateLabelRegex(#"(?:^|\s)[\[{]\s*[\"']"#),
    ]
    private static let opaqueKeyPattern = privateLabelRegex(#"[A-Z0-9_~+/=-]{28,}"#)

    private static func providerCategory(_ value: String) -> ProviderProductCategory? {
        switch value {
        case "subscription": .subscription
        case "api": .api
        case "local_tool": .localTool
        default: nil
        }
    }

    private static func placeholders(_ count: Int) -> String {
        Array(repeating: "?", count: count).joined(separator: ",")
    }

    private static func openDatabase(_ url: URL, immutable: Bool) throws -> OpaquePointer {
        var opened: OpaquePointer?
        let filename: String
        let flags: Int32
        if immutable {
            filename = url.absoluteString + "?mode=ro&immutable=1"
            flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX | SQLITE_OPEN_URI
        } else {
            filename = url.path
            flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX
        }
        guard sqlite3_open_v2(filename, &opened, flags, nil) == SQLITE_OK, let opened else {
            if opened != nil { sqlite3_close_v2(opened) }
            throw RepositoryError.databaseUnavailable
        }
        return opened
    }

    private static func hasWALSidecars(_ url: URL) -> Bool {
        let path = url.path
        return FileManager.default.fileExists(atPath: path + "-wal")
            || FileManager.default.fileExists(atPath: path + "-shm")
    }


    private static func scopeOrder(_ lhs: ProviderScope, _ rhs: ProviderScope) -> Bool {
        lhs.providerID == rhs.providerID ? lhs.accountRef < rhs.accountRef : lhs.providerID < rhs.providerID
    }

    private static func windowOrder(_ lhs: CapacityItem, _ rhs: CapacityItem) -> Bool {
        switch (lhs.remainingRatio, rhs.remainingRatio) {
        case let (left?, right?) where left != right: return left < right
        case (_?, nil): return true
        case (nil, _?): return false
        default:
            switch (lhs.resetsAt, rhs.resetsAt) {
            case let (left?, right?) where left != right: return left < right
            case (_?, nil): return true
            case (nil, _?): return false
            default:
                if lhs.quotaName != rhs.quotaName { return lhs.quotaName < rhs.quotaName }
                return lhs.recordID < rhs.recordID
            }
        }
    }
}

private struct CoverageKey: Hashable {
    let day: LocalDay
    let scope: ProviderScope
}

struct SchemaColumn: Equatable {
    let name: String
    let type: String
    let required: Bool
    let defaultValue: String?
    let primaryKey: Int

    static func required(
        _ name: String, _ type: String, defaultValue: String? = nil, primaryKey: Int = 0
    ) -> Self {
        Self(name: name, type: type, required: true, defaultValue: defaultValue, primaryKey: primaryKey)
    }

    static func optional(
        _ name: String, _ type: String, defaultValue: String? = nil, primaryKey: Int = 0
    ) -> Self {
        Self(name: name, type: type, required: false, defaultValue: defaultValue, primaryKey: primaryKey)
    }
}

struct IndexTerm: Equatable {
    let name: String
    let descending: Bool
}

struct ExpectedIndex {
    let name: String
    let table: String
    let unique: Bool
    let partial: Bool
    let terms: [IndexTerm]
}

private enum DDLToken: Equatable {
    case word(String)
    case symbol(Character)
}

private enum SQLiteDDLParser {
    static func hasCanonicalAutoincrementColumn(in sql: String, column: String) -> Bool {
        let tokens = tokenize(sql)
        guard let opening = tokens.firstIndex(of: .symbol("(")) else { return false }
        var depth = 1
        var entry: [DDLToken] = []
        var entries: [[DDLToken]] = []
        for token in tokens[tokens.index(after: opening)...] {
            switch token {
            case .symbol("("):
                depth += 1
                entry.append(token)
            case .symbol(")"):
                depth -= 1
                if depth == 0 {
                    if !entry.isEmpty { entries.append(entry) }
                    break
                }
                entry.append(token)
            case .symbol(",") where depth == 1:
                entries.append(entry)
                entry.removeAll(keepingCapacity: true)
            default:
                entry.append(token)
            }
            if depth == 0 { break }
        }
        let expected: [DDLToken] = [
            .word(column.uppercased()), .word("INTEGER"), .word("PRIMARY"),
            .word("KEY"), .word("AUTOINCREMENT"),
        ]
        return entries.contains { entry in
            entry.count >= expected.count && Array(entry.prefix(expected.count)) == expected
        }
    }

    private static func tokenize(_ sql: String) -> [DDLToken] {
        let characters = Array(sql)
        var tokens: [DDLToken] = []
        var word = ""
        var index = 0

        func appendWord() {
            if !word.isEmpty {
                tokens.append(.word(word.uppercased()))
                word.removeAll(keepingCapacity: true)
            }
        }

        while index < characters.count {
            let character = characters[index]
            let next = index + 1 < characters.count ? characters[index + 1] : nil
            if character.isWhitespace {
                appendWord()
                index += 1
            } else if character == "-", next == "-" {
                appendWord()
                index += 2
                while index < characters.count, characters[index] != "\n" { index += 1 }
            } else if character == "/", next == "*" {
                appendWord()
                index += 2
                while index + 1 < characters.count,
                      !(characters[index] == "*" && characters[index + 1] == "/") {
                    index += 1
                }
                index = min(characters.count, index + 2)
            } else if character == "'" {
                appendWord()
                index += 1
                while index < characters.count {
                    if characters[index] == "'" {
                        if index + 1 < characters.count, characters[index + 1] == "'" {
                            index += 2
                            continue
                        }
                        index += 1
                        break
                    }
                    index += 1
                }
            } else if character == "\"" || character == "`" || character == "[" {
                appendWord()
                let closing: Character = character == "[" ? "]" : character
                var identifier = ""
                index += 1
                while index < characters.count {
                    if characters[index] == closing {
                        if closing != "]", index + 1 < characters.count,
                           characters[index + 1] == closing {
                            identifier.append(closing)
                            index += 2
                            continue
                        }
                        index += 1
                        break
                    }
                    identifier.append(characters[index])
                    index += 1
                }
                tokens.append(.word(identifier.uppercased()))
            } else if character == "(" || character == ")" || character == "," {
                appendWord()
                tokens.append(.symbol(character))
                index += 1
            } else if character.isLetter || character.isNumber || character == "_" {
                word.append(character)
                index += 1
            } else {
                appendWord()
                tokens.append(.symbol(character))
                index += 1
            }
        }
        appendWord()
        return tokens
    }
}
