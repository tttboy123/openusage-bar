import Darwin
import Foundation
import Testing
@testable import UsageCore

@Suite("Bounded local API client")
struct LocalAPIClientTests {
    @Test("Health schema and snapshot use read-only Unix HTTP")
    func success() async throws {
        let healthServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":7,"generatedAt":"2026-07-18T01:00:00Z","sources":[],"health":{"ok":true,"status":"ok"}}"#)
        let health = try await LocalAPIClient(socketURL: healthServer.url).health()
        #expect(health.ok && health.dataRevision == 7)
        #expect(healthServer.request.contains("GET /v1/health HTTP/1.1"))

        let schemaServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":8,"generatedAt":"2026-07-18T01:01:00Z","routes":["/v1/health","/v1/snapshot"],"errorShape":{}}"#)
        let schema = try await LocalAPIClient(socketURL: schemaServer.url).schema()
        #expect(schema.routes == ["/v1/health", "/v1/snapshot"])

        let snapshotServer = try UnixFixtureServer(json: #"{"schemaVersion":"1.0","dataRevision":9,"generatedAt":"2026-07-18T01:02:00Z","localDay":"2026-07-18","summary":{"todayTokens":42,"modelCount":1,"coveredDayCount":1},"quotaWindows":[],"providers":[],"sources":[],"catalogRevision":"test"}"#)
        let snapshot = try await LocalAPIClient(socketURL: snapshotServer.url)
            .snapshot(localDay: "2026-07-18")
        #expect(snapshot.localDay == "2026-07-18")
        #expect(snapshot.dataRevision == 9)
        #expect(snapshotServer.request.contains("GET /v1/snapshot?today=2026-07-18 HTTP/1.1"))
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
