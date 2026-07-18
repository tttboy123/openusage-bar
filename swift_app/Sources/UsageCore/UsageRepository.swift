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
    let lock = NSLock()
    let now: () -> Date
    let databaseURL: URL
    var isClosed = false

    public init(databaseURL: URL, now: @escaping () -> Date = Date.init) throws {
        self.now = now
        self.databaseURL = databaseURL
        guard FileManager.default.fileExists(atPath: databaseURL.path) else {
            throw RepositoryError.databaseUnavailable
        }
        let opened = try openValidatedDatabase()
        sqlite3_close_v2(opened)
    }

    func openValidatedDatabase() throws -> OpaquePointer {
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

    func quotaAppliesTo(
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

    func stableQuotaIdentifier(_ value: String) throws -> String {
        guard Self.isStableID(value) else { throw RepositoryError.corruptData }
        return value
    }
}
