import Darwin
import Foundation
import Testing
@testable import UsageCore

@Suite("Bounded local API client")
struct LocalAPIClientTests {
    @Test("Current client reads the frozen N-1 snapshot and ignores additive fields")
    func nMinusOneAndAdditiveCompatibility() async throws {
        let old = try compatibilityFixture("v0.4.2.snapshot.json")
        let oldServer = try UnixFixtureServer(json: old)
        let oldSnapshot = try await LocalAPIClient(socketURL: oldServer.url)
            .snapshot(localDay: "2026-07-18")
        #expect(oldSnapshot.dataRevision == 9)
        #expect(oldSnapshot.todayTokens == 42)

        let current = try compatibilityFixture("current-additive.snapshot.json")
        let wire = try JSONDecoder().decode(FrozenV042Snapshot.self, from: Data(current.utf8))
        #expect(wire.schemaVersion == "1.0")
        #expect(wire.dataRevision == 84)
        #expect(wire.summary.todayTokens == 1_024)

        let currentServer = try UnixFixtureServer(json: current)
        let currentSnapshot = try await LocalAPIClient(socketURL: currentServer.url)
            .snapshot(localDay: "2026-07-29")
        #expect(currentSnapshot.dataRevision == 84)
        #expect(currentSnapshot.todayTokens == 1_024)
    }

    @Test("Default client rejects a body over one MiB")
    func defaultOneMiBMaximum() async throws {
        let oversized = try UnixFixtureServer(json: String(repeating: "x", count: 1_048_577))
        await #expect(throws: LocalAPIClientError.responseTooLarge) {
            try await LocalAPIClient(socketURL: oversized.url).health()
        }
    }

    @Test("Health schema and snapshot use read-only Unix HTTP")
    func success() async throws {
        let healthServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":7,"generatedAt":"2026-07-18T01:00:00Z","sources":[],"health":{"ok":true,"status":"ok"}}"#)
        let health = try await LocalAPIClient(socketURL: healthServer.url).health()
        #expect(health.ok && health.dataRevision == 7)
        #expect(healthServer.request.contains("GET /v1/health HTTP/1.1"))

        let schemaServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":8,"generatedAt":"2026-07-18T01:01:00Z","routes":["/v1/health","/v1/snapshot"],"errorShape":{}}"#)
        let schema = try await LocalAPIClient(socketURL: schemaServer.url).schema()
        #expect(schema.routes == ["/v1/health", "/v1/snapshot"])

        let snapshotServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":9,"generatedAt":"2026-07-18T01:02:00Z","localDay":"2026-07-18","summary":{"todayTokens":42,"modelCount":1,"coveredDayCount":1},"balances":[{"recordId":"moonshot-main.balance","providerId":"moonshot-main","accountRef":null,"currency":"CNY","available":"123.45","voucher":"10","cash":"113.45","observedAt":"2026-07-18T01:01:00Z","freshnessSeconds":60,"state":"ok","quality":"direct","stale":false,"revision":2,"sourceId":"moonshot.balance"}],"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        let snapshot = try await LocalAPIClient(socketURL: snapshotServer.url)
            .snapshot(localDay: "2026-07-18")
        #expect(snapshot.localDay == "2026-07-18")
        #expect(snapshot.dataRevision == 9)
        #expect(snapshot.balances.count == 1)
        #expect(snapshot.balances.first?.available == "123.45")
        #expect(snapshot.balances.first?.currency == "CNY")
        #expect(snapshot.balances.first?.providerID == "moonshot-main")
        #expect(snapshotServer.request.contains("GET /v1/snapshot?today=2026-07-18 HTTP/1.1"))

        let missingServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":10,"generatedAt":"2026-07-18T01:03:00Z","localDay":"2026-07-19","summary":{"todayTokens":null,"modelCount":0,"coveredDayCount":0},"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        let missing = try await LocalAPIClient(socketURL: missingServer.url)
            .snapshot(localDay: "2026-07-19")
        #expect(missing.todayTokens == nil)
        #expect(missing.coveredDayCount == 0)

        let coveredZeroServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":11,"generatedAt":"2026-07-18T01:04:00Z","localDay":"2026-07-20","summary":{"todayTokens":0,"modelCount":0,"coveredDayCount":1},"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        let coveredZero = try await LocalAPIClient(socketURL: coveredZeroServer.url)
            .snapshot(localDay: "2026-07-20")
        #expect(coveredZero.todayTokens == 0)
        #expect(coveredZero.coveredDayCount == 1)
    }

    @Test("Snapshot rejects malformed balance facts")
    func invalidBalances() async throws {
        let negative = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":14,"generatedAt":"2026-07-18T01:07:00Z","localDay":"2026-07-22","summary":{"todayTokens":null,"modelCount":0,"coveredDayCount":0},"balances":[{"recordId":"moonshot.balance","providerId":"moonshot","accountRef":null,"currency":"CNY","available":"-1","voucher":null,"cash":null,"observedAt":"2026-07-18T01:01:00Z","freshnessSeconds":60,"state":"ok","quality":"direct","stale":false,"revision":1,"sourceId":"moonshot.balance"}],"quotaWindows":[],"providers":[],"sources":[]}"#)
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: negative.url)
                .snapshot(localDay: "2026-07-22")
        }

        let inventedUnknown = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":15,"generatedAt":"2026-07-18T01:08:00Z","localDay":"2026-07-22","summary":{"todayTokens":null,"modelCount":0,"coveredDayCount":0},"balances":[{"recordId":"moonshot.balance","providerId":"moonshot","accountRef":null,"currency":"CNY","available":"1","voucher":null,"cash":null,"observedAt":"2026-07-18T01:01:00Z","freshnessSeconds":60,"state":"unknown","quality":"direct","stale":true,"revision":1,"sourceId":"moonshot.balance"}],"quotaWindows":[],"providers":[],"sources":[]}"#)
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: inventedUnknown.url)
                .snapshot(localDay: "2026-07-22")
        }
    }

    @Test("Snapshot rejects invented zero and contradictory unknown totals")
    func invalidSummaryTotals() async throws {
        let inventedZero = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":12,"generatedAt":"2026-07-18T01:05:00Z","localDay":"2026-07-21","summary":{"todayTokens":0,"modelCount":0,"coveredDayCount":0},"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: inventedZero.url)
                .snapshot(localDay: "2026-07-21")
        }

        let contradictoryUnknown = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":13,"generatedAt":"2026-07-18T01:06:00Z","localDay":"2026-07-22","summary":{"todayTokens":null,"modelCount":1,"coveredDayCount":1},"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: contradictoryUnknown.url)
                .snapshot(localDay: "2026-07-22")
        }
    }

    @Test("Daily activity preserves counting conventions and old JSON defaults unknown")
    func dailyActivityCountingConvention() async throws {
        let current = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":10,"generatedAt":"2026-07-18T01:02:00Z","rows":[{"day":"2026-07-18","providerId":"codex","accountRef":null,"modelId":"gpt-5.6-sol","inputTokens":100,"outputTokens":20,"cacheReadTokens":80,"cacheCreationTokens":4,"reasoningTokens":3,"totalTokens":120,"costAmount":null,"costCurrency":null,"costBasis":null,"quality":"direct","importedAt":"2026-07-18T00:30:00Z","revision":1,"recordId":"daily:codex","sourceId":"codex.local_sessions","tokenCountingConvention":"input_includes_cache"}],"coverage":[]}"#)
        let currentActivity = try await LocalAPIClient(socketURL: current.url).dailyActivity(
            from: "2026-07-18", to: "2026-07-18"
        )
        let currentRow = try #require(currentActivity.records.first)
        #expect(currentRow.totalTokens == 120)
        #expect(currentRow.tokenCountingConvention == .inputIncludesCache)
        #expect(current.request.contains(
            "GET /v1/activity/daily?from=2026-07-18&to=2026-07-18 HTTP/1.1"
        ))

        let legacy = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":9,"generatedAt":"2026-07-18T01:02:00Z","rows":[{"day":"2026-07-18","providerId":"legacy","accountRef":null,"modelId":"unknown","inputTokens":1,"outputTokens":1,"cacheReadTokens":0,"cacheCreationTokens":0,"reasoningTokens":null,"totalTokens":99,"costAmount":null,"costCurrency":null,"costBasis":null,"quality":"legacy","importedAt":"2026-07-18T00:30:00Z","revision":1,"recordId":"daily:legacy","sourceId":"legacy"}],"coverage":[]}"#)
        let legacyActivity = try await LocalAPIClient(socketURL: legacy.url).dailyActivity(
            from: "2026-07-18", to: "2026-07-18"
        )
        #expect(legacyActivity.records.first?.tokenCountingConvention == .unknown)
        #expect(legacyActivity.records.first?.totalTokens == 99)
    }

    @Test("Unavailable sockets and total timeout are typed")
    func unavailableAndTimeout() async throws {
        let missing = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        await #expect(throws: LocalAPIClientError.unavailable) {
            try await LocalAPIClient(socketURL: missing).health()
        }
        let server = try UnixFixtureServer(json: "{}", responseDelay: 0.5)
        await #expect(throws: LocalAPIClientError.timedOut) {
            try await LocalAPIClient(
                socketURL: server.url, timeout: .milliseconds(50)
            ).health()
        }
    }

    @Test("Schema drift malformed framing oversized bodies and non JSON are rejected")
    func hostileResponses() async throws {
        let drift = try UnixFixtureServer(json: #"{"schemaVersion":"2.0","dataRevision":1,"generatedAt":"x","health":{"ok":true,"status":"ok"}}"#)
        await #expect(throws: LocalAPIClientError.schemaMismatch) {
            try await LocalAPIClient(socketURL: drift.url).health()
        }

        let malformed = try UnixFixtureServer(rawResponse: Data("HTTP/1.0 200 OK\r\n\r\n{}".utf8))
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: malformed.url).health()
        }

        let oversized = try UnixFixtureServer(json: String(repeating: "x", count: 1_025))
        await #expect(throws: LocalAPIClientError.responseTooLarge) {
            try await LocalAPIClient(
                socketURL: oversized.url, maximumBodyBytes: 1_024
            ).health()
        }

        let nonJSON = try UnixFixtureServer(json: "not-json")
        await #expect(throws: LocalAPIClientError.invalidResponse) {
            try await LocalAPIClient(socketURL: nonJSON.url).health()
        }
    }
}

private struct FrozenV042Snapshot: Decodable {
    struct Summary: Decodable {
        let todayTokens: Int64?
        let modelCount: Int
        let coveredDayCount: Int
    }

    let schemaVersion: String
    let dataRevision: UInt64
    let generatedAt: String
    let localDay: String
    let summary: Summary
    let quotaWindows: [FrozenItem]
    let providers: [FrozenItem]
    let sources: [FrozenItem]
    let catalogRevision: String
}

private struct FrozenItem: Decodable {}

private func compatibilityFixture(_ name: String) throws -> String {
    let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    return try String(
        contentsOf: root.appendingPathComponent("tests/fixtures/local-api-v1/\(name)"),
        encoding: .utf8
    )
}

private final class UnixFixtureServer: @unchecked Sendable {
    let url: URL
    private let descriptor: Int32
    private let queue = DispatchQueue(label: "LocalAPIClientTests.server")
    private let lock = NSLock()
    private var capturedRequest = ""

    var request: String { lock.withLock { capturedRequest } }

    convenience init(json: String, responseDelay: TimeInterval = 0) throws {
        let body = Data(json.utf8)
        let header = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
        try self.init(rawResponse: header + body, responseDelay: responseDelay)
    }

    init(rawResponse: Data, responseDelay: TimeInterval = 0) throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        url = directory.appendingPathComponent("api.sock")
        descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw CocoaError(.fileWriteUnknown) }
        var address = try Self.address(path: url.path)
        let addressLength = socklen_t(address.sun_len)
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(descriptor, $0, addressLength)
            }
        }
        guard result == 0, listen(descriptor, 1) == 0 else {
            close(descriptor)
            throw CocoaError(.fileWriteUnknown)
        }
        queue.async { [weak self] in
            guard let self else { return }
            let client = accept(descriptor, nil, nil)
            guard client >= 0 else { return }
            defer { close(client) }
            var suppressBrokenPipe = Int32(1)
            guard setsockopt(
                client, SOL_SOCKET, SO_NOSIGPIPE,
                &suppressBrokenPipe, socklen_t(MemoryLayout<Int32>.size)
            ) == 0 else { return }
            var buffer = [UInt8](repeating: 0, count: 4_096)
            let count = Darwin.read(client, &buffer, buffer.count)
            if count > 0 {
                lock.withLock { capturedRequest = String(decoding: buffer[..<count], as: UTF8.self) }
            }
            if responseDelay > 0 { Thread.sleep(forTimeInterval: responseDelay) }
            rawResponse.withUnsafeBytes { bytes in
                if let base = bytes.baseAddress { _ = Darwin.write(client, base, rawResponse.count) }
            }
        }
    }

    deinit {
        close(descriptor)
        unlink(url.path)
        try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
    }

    private static func address(path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw CocoaError(.fileWriteUnknown)
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.offset(of: \.sun_path)! + bytes.count)
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: bytes.map { UInt8(bitPattern: $0) })
        }
        return address
    }
}
