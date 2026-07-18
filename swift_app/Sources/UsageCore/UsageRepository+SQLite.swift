import Foundation
import SQLite3

extension UsageRepository {
    func withReadTransaction<T>(_ body: (OpaquePointer) throws -> T) throws -> T {
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

    func withStatement<T>(
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

    func currentRevision(_ database: OpaquePointer) throws -> Int64 {
        try scalarInt64(database, sql: "SELECT COALESCE(MAX(change_seq),0) FROM change_log")
    }

    func scalarInt64(
        _ database: OpaquePointer, sql: String, bindings: [String] = []
    ) throws -> Int64 {
        try withStatement(database, sql: sql, bindings: bindings) { statement in
            guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.corruptData }
            let value = try requiredInt64(statement, 0)
            guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.corruptData }
            return value
        }
    }

    func optionalScalarText(_ database: OpaquePointer, sql: String) throws -> String? {
        try withStatement(database, sql: sql) { statement in
            guard sqlite3_step(statement) == SQLITE_ROW else { throw RepositoryError.corruptData }
            let value = try optionalText(statement, 0)
            guard sqlite3_step(statement) == SQLITE_DONE else { throw RepositoryError.corruptData }
            return value
        }
    }

    func requiredText(_ statement: OpaquePointer, _ index: Int32) throws -> String {
        guard sqlite3_column_type(statement, index) == SQLITE_TEXT,
              let pointer = sqlite3_column_text(statement, index)
        else { throw RepositoryError.corruptData }
        return String(cString: pointer)
    }

    func optionalText(_ statement: OpaquePointer, _ index: Int32) throws -> String? {
        if sqlite3_column_type(statement, index) == SQLITE_NULL { return nil }
        return try requiredText(statement, index)
    }

    func requiredInt64(_ statement: OpaquePointer, _ index: Int32) throws -> Int64 {
        guard sqlite3_column_type(statement, index) == SQLITE_INTEGER else {
            throw RepositoryError.corruptData
        }
        return sqlite3_column_int64(statement, index)
    }

    func optionalInt64(_ statement: OpaquePointer, _ index: Int32) throws -> Int64? {
        if sqlite3_column_type(statement, index) == SQLITE_NULL { return nil }
        return try requiredInt64(statement, index)
    }

    func optionalDouble(_ statement: OpaquePointer, _ index: Int32) throws -> Double? {
        let type = sqlite3_column_type(statement, index)
        if type == SQLITE_NULL { return nil }
        guard type == SQLITE_FLOAT || type == SQLITE_INTEGER else { throw RepositoryError.corruptData }
        let value = sqlite3_column_double(statement, index)
        guard value.isFinite else { throw RepositoryError.corruptData }
        return value
    }

    func requiredBool(_ statement: OpaquePointer, _ index: Int32) throws -> Bool {
        let value = try requiredInt64(statement, index)
        guard value == 0 || value == 1 else { throw RepositoryError.corruptData }
        return value == 1
    }

    func parseTimestamp(_ value: String) throws -> Date {
        let fractional = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        if let date = try? Date(value, strategy: fractional) { return date }
        if let date = try? Date(value, strategy: .iso8601) { return date }
        throw RepositoryError.corruptData
    }

    static func canonicalTimestamp(_ date: Date) -> String {
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

    static let sqliteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    static let stableID = try! NSRegularExpression(pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    static func isStableID(_ value: String) -> Bool {
        stableID.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    static func isSafeDisplayName(_ value: String) -> Bool {
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

    static func isCanonicalIdentityBinding(
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

    static func matches(_ regex: NSRegularExpression, _ value: String) -> Bool {
        regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
    }

    static func matches(
        in value: String, regex: NSRegularExpression
    ) -> [NSTextCheckingResult] {
        regex.matches(in: value, range: NSRange(value.startIndex..., in: value))
    }

    static func privateLabelRegex(_ pattern: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
    }

    static let privateLabelPatterns = [
        privateLabelRegex(#"[A-Z0-9._%+-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?"#),
        privateLabelRegex(#"(?<![A-Z0-9._-])(?:/(?!\s)[^\s()]+|~[/\\](?!\s)[^\s()]+|[A-Z]:[/\\](?!\s)[^\s()]+)"#),
        privateLabelRegex(#"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|cookie|access[_ -]?token|refresh[_ -]?token|token|username|user|account(?:_email)?|email|attributes?|raw_attrs?|response_body|body)\s*[:=]"#),
        privateLabelRegex(#"(?:bearer\s+)[A-Z0-9._~+/=-]+|eyJ[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}|(?:sk-|ghp_|xox[baprs]-|AIza)[A-Z0-9_-]{16,}"#),
        privateLabelRegex(#"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|cookie|access[_ -]?token|refresh[_ -]?token|token)\s+[A-Z0-9._~+/=-]{20,}(?=\s|$)"#),
        privateLabelRegex(#"(?:^|\s)[\[{]\s*[\"']"#),
    ]
    static let opaqueKeyPattern = privateLabelRegex(#"[A-Z0-9_~+/=-]{28,}"#)

    static func providerCategory(_ value: String) -> ProviderProductCategory? {
        switch value {
        case "subscription": .subscription
        case "api": .api
        case "local_tool": .localTool
        default: nil
        }
    }

    static func placeholders(_ count: Int) -> String {
        Array(repeating: "?", count: count).joined(separator: ",")
    }

    static func openDatabase(_ url: URL, immutable: Bool) throws -> OpaquePointer {
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

    static func hasWALSidecars(_ url: URL) -> Bool {
        let path = url.path
        return FileManager.default.fileExists(atPath: path + "-wal")
            || FileManager.default.fileExists(atPath: path + "-shm")
    }


    static func scopeOrder(_ lhs: ProviderScope, _ rhs: ProviderScope) -> Bool {
        lhs.providerID == rhs.providerID ? lhs.accountRef < rhs.accountRef : lhs.providerID < rhs.providerID
    }

    static func windowOrder(_ lhs: CapacityItem, _ rhs: CapacityItem) -> Bool {
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
    }}
