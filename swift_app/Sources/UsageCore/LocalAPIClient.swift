import Darwin
import Foundation

public enum LocalAPIClientError: Error, Sendable, Equatable {
    case unavailable
    case timedOut
    case responseTooLarge
    case invalidResponse
    case schemaMismatch
}

public struct LocalAPIHealth: Sendable, Hashable {
    public let schemaVersion: String
    public let dataRevision: UInt64
    public let generatedAt: String
    public let ok: Bool
    public let status: String
}

public struct LocalAPISchema: Sendable, Hashable {
    public let schemaVersion: String
    public let dataRevision: UInt64
    public let generatedAt: String
    public let routes: [String]
}

public struct LocalAPIResourceSnapshot: Sendable, Hashable {
    public let schemaVersion: String
    public let dataRevision: UInt64
    public let generatedAt: String
    public let localDay: String
    public let todayTokens: Int64?
    public let modelCount: Int
    public let coveredDayCount: Int
    public let quotaWindowCount: Int
    public let providerCount: Int
    public let sourceCount: Int
}

public struct LocalAPIDailyActivity: Sendable, Hashable {
    public let schemaVersion: String
    public let dataRevision: UInt64
    public let generatedAt: String
    public let records: [DailyUsage]
    public let coverage: [CoverageDay]
}

public protocol LocalAPIReading: Sendable {
    func health() async throws -> LocalAPIHealth
    func schema() async throws -> LocalAPISchema
    func snapshot(localDay: String?) async throws -> LocalAPIResourceSnapshot
}

public struct LocalAPIClient: LocalAPIReading, Sendable {
    public static let defaultSocketURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/openusage-bar/openusage.sock")

    private let socketURL: URL
    private let timeoutSeconds: Double
    private let maximumBodyBytes: Int

    public init(
        socketURL: URL = Self.defaultSocketURL,
        timeout: Duration = .seconds(3),
        maximumBodyBytes: Int = 1_048_576
    ) {
        self.socketURL = socketURL
        let components = timeout.components
        timeoutSeconds = max(
            0,
            Double(components.seconds)
                + Double(components.attoseconds) / 1_000_000_000_000_000_000
        )
        self.maximumBodyBytes = maximumBodyBytes
    }

    public func health() async throws -> LocalAPIHealth {
        let data = try await fetch("/v1/health")
        let wire = try decode(HealthWire.self, from: data)
        try requireSchema(wire.schemaVersion)
        return LocalAPIHealth(
            schemaVersion: wire.schemaVersion, dataRevision: wire.dataRevision,
            generatedAt: wire.generatedAt, ok: wire.health.ok,
            status: wire.health.status
        )
    }

    public func schema() async throws -> LocalAPISchema {
        let data = try await fetch("/v1/schema")
        let wire = try decode(SchemaWire.self, from: data)
        try requireSchema(wire.schemaVersion)
        guard wire.routes.count <= 128,
              wire.routes.allSatisfy({ $0.hasPrefix("/v1/") && $0.utf8.count <= 128 })
        else { throw LocalAPIClientError.invalidResponse }
        return LocalAPISchema(
            schemaVersion: wire.schemaVersion, dataRevision: wire.dataRevision,
            generatedAt: wire.generatedAt, routes: wire.routes
        )
    }

    public func snapshot(localDay: String?) async throws -> LocalAPIResourceSnapshot {
        let target: String
        if let localDay {
            guard localDay.range(
                of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression
            ) != nil else { throw LocalAPIClientError.invalidResponse }
            target = "/v1/snapshot?today=\(localDay)"
        } else {
            target = "/v1/snapshot"
        }
        let data = try await fetch(target)
        let wire = try decode(SnapshotWire.self, from: data)
        try requireSchema(wire.schemaVersion)
        guard wire.modelCount >= 0, wire.coveredDayCount >= 0,
              wire.todayTokens.map({ $0 >= 0 }) ?? true,
              wire.quotaWindows.count <= 10_000,
              wire.providers.count <= 10_000,
              wire.sources.count <= 10_000
        else { throw LocalAPIClientError.invalidResponse }
        return LocalAPIResourceSnapshot(
            schemaVersion: wire.schemaVersion, dataRevision: wire.dataRevision,
            generatedAt: wire.generatedAt, localDay: wire.localDay,
            todayTokens: wire.todayTokens, modelCount: wire.modelCount,
            coveredDayCount: wire.coveredDayCount,
            quotaWindowCount: wire.quotaWindows.count,
            providerCount: wire.providers.count, sourceCount: wire.sources.count
        )
    }

    public func dailyActivity(from start: String, to end: String) async throws -> LocalAPIDailyActivity {
        guard let startDay = LocalDay(rawValue: start),
              let endDay = LocalDay(rawValue: end),
              startDay <= endDay,
              let startDate = startDay.utcDate,
              let endDate = endDay.utcDate,
              let distance = Calendar.utc.dateComponents(
                [.day], from: startDate, to: endDate
              ).day,
              distance + 1 <= 731
        else { throw LocalAPIClientError.invalidResponse }

        let data = try await fetch("/v1/activity/daily?from=\(start)&to=\(end)")
        let wire = try decode(ActivityWire.self, from: data)
        try requireSchema(wire.schemaVersion)
        guard wire.rows.count <= 10_000, wire.coverage.count <= 100_000 else {
            throw LocalAPIClientError.invalidResponse
        }
        let records = try wire.rows.map { row -> DailyUsage in
            guard let day = LocalDay(rawValue: row.day),
                  row.inputTokens >= 0, row.outputTokens >= 0,
                  row.cacheReadTokens >= 0, row.cacheCreationTokens >= 0,
                  row.reasoningTokens.map({ $0 >= 0 }) ?? true,
                  row.totalTokens >= 0, row.revision > 0,
                  Self.safeText(row.providerID, maximumBytes: 128),
                  Self.safeText(row.accountRef ?? "", maximumBytes: 256, allowEmpty: true),
                  Self.safeText(row.modelID, maximumBytes: 256),
                  Self.safeText(row.quality, maximumBytes: 128),
                  Self.safeText(row.importedAt, maximumBytes: 64),
                  Self.safeText(row.recordID, maximumBytes: 512),
                  Self.safeText(row.sourceID, maximumBytes: 128)
            else { throw LocalAPIClientError.invalidResponse }
            return DailyUsage(
                day: day, providerID: row.providerID, accountRef: row.accountRef ?? "",
                modelID: row.modelID, inputTokens: row.inputTokens,
                outputTokens: row.outputTokens, cacheReadTokens: row.cacheReadTokens,
                cacheCreationTokens: row.cacheCreationTokens,
                reasoningTokens: row.reasoningTokens, totalTokens: row.totalTokens,
                costAmount: row.costAmount, costCurrency: row.costCurrency,
                costBasis: row.costBasis, quality: row.quality,
                importedAt: row.importedAt, revision: row.revision,
                recordID: row.recordID, sourceID: row.sourceID,
                tokenCountingConvention: row.tokenCountingConvention
            )
        }
        let coverage = try wire.coverage.map { item -> CoverageDay in
            guard let day = LocalDay(rawValue: item.day),
                  Self.safeText(item.providerID, maximumBytes: 128),
                  Self.safeText(item.accountRef ?? "", maximumBytes: 256, allowEmpty: true),
                  item.sourceID.map({ Self.safeText($0, maximumBytes: 128) }) ?? true
            else { throw LocalAPIClientError.invalidResponse }
            return CoverageDay(
                day: day, providerID: item.providerID, accountRef: item.accountRef ?? "",
                isCovered: item.covered, sourceID: item.sourceID
            )
        }
        return LocalAPIDailyActivity(
            schemaVersion: wire.schemaVersion, dataRevision: wire.dataRevision,
            generatedAt: wire.generatedAt, records: records, coverage: coverage
        )
    }

    private func fetch(_ target: String) async throws -> Data {
        guard timeoutSeconds > 0, maximumBodyBytes > 0 else {
            throw LocalAPIClientError.invalidResponse
        }
        return try await Task.detached(priority: .utility) {
            try UnixHTTPTransport(
                socketURL: socketURL, timeoutSeconds: timeoutSeconds,
                maximumBodyBytes: maximumBodyBytes
            ).get(target)
        }.value
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw LocalAPIClientError.invalidResponse }
    }

    private func requireSchema(_ value: String) throws {
        guard value == "1.0" else { throw LocalAPIClientError.schemaMismatch }
    }

    private static func safeText(
        _ value: String, maximumBytes: Int, allowEmpty: Bool = false
    ) -> Bool {
        (allowEmpty || !value.isEmpty)
            && value.utf8.count <= maximumBytes
            && value.unicodeScalars.allSatisfy { scalar in
                scalar.value >= 0x20 && scalar.value != 0x7f
            }
    }
}

private struct HealthWire: Decodable {
    struct State: Decodable { let ok: Bool; let status: String }
    let schemaVersion: String
    let dataRevision: UInt64
    let generatedAt: String
    let health: State
}

private struct SchemaWire: Decodable {
    let schemaVersion: String
    let dataRevision: UInt64
    let generatedAt: String
    let routes: [String]
}

private struct SnapshotWire: Decodable {
    struct Summary: Decodable {
        let todayTokens: Int64?
        let modelCount: Int
        let coveredDayCount: Int
    }
    struct Item: Decodable {}
    let schemaVersion: String
    let dataRevision: UInt64
    let generatedAt: String
    let localDay: String
    let summary: Summary
    let quotaWindows: [Item]
    let providers: [Item]
    let sources: [Item]
    var todayTokens: Int64? { summary.todayTokens }
    var modelCount: Int { summary.modelCount }
    var coveredDayCount: Int { summary.coveredDayCount }
}

private struct ActivityWire: Decodable {
    struct Row: Decodable {
        let day: String
        let providerID: String
        let accountRef: String?
        let modelID: String
        let inputTokens: Int64
        let outputTokens: Int64
        let cacheReadTokens: Int64
        let cacheCreationTokens: Int64
        let reasoningTokens: Int64?
        let totalTokens: Int64
        let costAmount: String?
        let costCurrency: String?
        let costBasis: String?
        let quality: String
        let importedAt: String
        let revision: Int64
        let recordID: String
        let sourceID: String
        let tokenCountingConvention: TokenCountingConvention

        enum CodingKeys: String, CodingKey {
            case day
            case providerID = "providerId"
            case accountRef
            case modelID = "modelId"
            case inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens
            case reasoningTokens, totalTokens, costAmount, costCurrency, costBasis
            case quality, importedAt, revision
            case recordID = "recordId"
            case sourceID = "sourceId"
            case tokenCountingConvention
        }

        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            day = try values.decode(String.self, forKey: .day)
            providerID = try values.decode(String.self, forKey: .providerID)
            accountRef = try values.decodeIfPresent(String.self, forKey: .accountRef)
            modelID = try values.decode(String.self, forKey: .modelID)
            inputTokens = try values.decode(Int64.self, forKey: .inputTokens)
            outputTokens = try values.decode(Int64.self, forKey: .outputTokens)
            cacheReadTokens = try values.decode(Int64.self, forKey: .cacheReadTokens)
            cacheCreationTokens = try values.decode(Int64.self, forKey: .cacheCreationTokens)
            reasoningTokens = try values.decodeIfPresent(Int64.self, forKey: .reasoningTokens)
            totalTokens = try values.decode(Int64.self, forKey: .totalTokens)
            costAmount = try values.decodeIfPresent(String.self, forKey: .costAmount)
            costCurrency = try values.decodeIfPresent(String.self, forKey: .costCurrency)
            costBasis = try values.decodeIfPresent(String.self, forKey: .costBasis)
            quality = try values.decode(String.self, forKey: .quality)
            importedAt = try values.decode(String.self, forKey: .importedAt)
            revision = try values.decode(Int64.self, forKey: .revision)
            recordID = try values.decode(String.self, forKey: .recordID)
            sourceID = try values.decode(String.self, forKey: .sourceID)
            tokenCountingConvention = try values.decodeIfPresent(
                TokenCountingConvention.self, forKey: .tokenCountingConvention
            ) ?? .unknown
        }
    }

    struct Coverage: Decodable {
        let day: String
        let providerID: String
        let accountRef: String?
        let covered: Bool
        let sourceID: String?

        enum CodingKeys: String, CodingKey {
            case day
            case providerID = "providerId"
            case accountRef, covered
            case sourceID = "sourceId"
        }
    }

    let schemaVersion: String
    let dataRevision: UInt64
    let generatedAt: String
    let rows: [Row]
    let coverage: [Coverage]
}

private struct UnixHTTPTransport: Sendable {
    let socketURL: URL
    let timeoutSeconds: Double
    let maximumBodyBytes: Int

    func get(_ target: String) throws -> Data {
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw LocalAPIClientError.unavailable }
        defer { close(descriptor) }
        var suppressBrokenPipe = Int32(1)
        guard setsockopt(
                  descriptor, SOL_SOCKET, SO_NOSIGPIPE,
                  &suppressBrokenPipe, socklen_t(MemoryLayout<Int32>.size)
              ) == 0,
              fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0,
              fcntl(descriptor, F_SETFL, O_NONBLOCK) == 0
        else { throw LocalAPIClientError.unavailable }
        let deadline = ProcessInfo.processInfo.systemUptime + timeoutSeconds
        try connect(descriptor, deadline: deadline)
        let request = Data(
            "GET \(target) HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nConnection: close\r\n\r\n".utf8
        )
        try writeAll(request, to: descriptor, deadline: deadline)
        return try readResponse(from: descriptor, deadline: deadline)
    }

    private func connect(_ descriptor: Int32, deadline: Double) throws {
        var address = try Self.address(path: socketURL.path)
        let addressLength = socklen_t(address.sun_len)
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(descriptor, $0, addressLength)
            }
        }
        if result == 0 { return }
        guard errno == EINPROGRESS else { throw LocalAPIClientError.unavailable }
        try wait(descriptor, events: Int16(POLLOUT), deadline: deadline)
        var socketError = Int32()
        var length = socklen_t(MemoryLayout<Int32>.size)
        guard getsockopt(
            descriptor, SOL_SOCKET, SO_ERROR, &socketError, &length
        ) == 0, socketError == 0 else { throw LocalAPIClientError.unavailable }
    }

    private func writeAll(_ data: Data, to descriptor: Int32, deadline: Double) throws {
        try data.withUnsafeBytes { bytes in
            guard let base = bytes.baseAddress else { return }
            var offset = 0
            while offset < data.count {
                let count = Darwin.write(
                    descriptor, base.advanced(by: offset), data.count - offset
                )
                if count > 0 { offset += count }
                else if count < 0, errno == EINTR { continue }
                else if count < 0, errno == EAGAIN || errno == EWOULDBLOCK {
                    try wait(descriptor, events: Int16(POLLOUT), deadline: deadline)
                } else { throw LocalAPIClientError.unavailable }
            }
        }
    }

    private func readResponse(from descriptor: Int32, deadline: Double) throws -> Data {
        var response = Data()
        var parsedHeader: (bodyStart: Int, length: Int)?
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count > 0 {
                response.append(buffer, count: count)
                if parsedHeader == nil {
                    if response.count > 65_536 { throw LocalAPIClientError.invalidResponse }
                    parsedHeader = try parseHeader(response)
                    if let parsedHeader, parsedHeader.length > maximumBodyBytes {
                        throw LocalAPIClientError.responseTooLarge
                    }
                }
                if let parsedHeader {
                    let bodyCount = response.count - parsedHeader.bodyStart
                    if bodyCount > parsedHeader.length {
                        throw LocalAPIClientError.invalidResponse
                    }
                    if bodyCount == parsedHeader.length {
                        return response.subdata(in: parsedHeader.bodyStart..<response.count)
                    }
                }
            } else if count == 0 {
                throw LocalAPIClientError.invalidResponse
            } else if errno == EINTR {
                continue
            } else if errno == EAGAIN || errno == EWOULDBLOCK {
                try wait(descriptor, events: Int16(POLLIN), deadline: deadline)
            } else {
                throw LocalAPIClientError.invalidResponse
            }
        }
    }

    private func parseHeader(_ data: Data) throws -> (bodyStart: Int, length: Int)? {
        let delimiter = Data("\r\n\r\n".utf8)
        guard let range = data.range(of: delimiter) else { return nil }
        guard let text = String(data: data[..<range.lowerBound], encoding: .utf8) else {
            throw LocalAPIClientError.invalidResponse
        }
        let lines = text.components(separatedBy: "\r\n")
        guard lines.first == "HTTP/1.1 200 OK" else {
            throw LocalAPIClientError.invalidResponse
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else {
                throw LocalAPIClientError.invalidResponse
            }
            let name = line[..<colon].lowercased()
            let value = line[line.index(after: colon)...]
                .trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty, headers[name] == nil else {
                throw LocalAPIClientError.invalidResponse
            }
            headers[name] = value
        }
        guard headers["transfer-encoding"] == nil,
              let rawLength = headers["content-length"],
              !rawLength.isEmpty, rawLength.allSatisfy(\.isNumber),
              let length = Int(rawLength), length >= 0
        else { throw LocalAPIClientError.invalidResponse }
        return (range.upperBound, length)
    }

    private func wait(_ descriptor: Int32, events: Int16, deadline: Double) throws {
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        guard remaining > 0 else { throw LocalAPIClientError.timedOut }
        var item = pollfd(fd: descriptor, events: events, revents: 0)
        let result = poll(&item, 1, Int32(min(remaining * 1_000, Double(Int32.max))))
        if result == 0 { throw LocalAPIClientError.timedOut }
        let fatalEvents = Int16(POLLERR | POLLNVAL)
        guard result > 0, item.revents & fatalEvents == 0 else {
            throw LocalAPIClientError.unavailable
        }
    }

    private static func address(path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path),
              let offset = MemoryLayout<sockaddr_un>.offset(of: \.sun_path),
              offset + bytes.count <= Int(UInt8.max)
        else { throw LocalAPIClientError.unavailable }
        address.sun_len = UInt8(offset + bytes.count)
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: bytes.map { UInt8(bitPattern: $0) })
        }
        return address
    }
}
