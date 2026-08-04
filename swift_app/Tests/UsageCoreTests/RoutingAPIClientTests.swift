import Darwin
import Foundation
import Testing
@testable import UsageCore

@Suite("Bounded routing API client")
struct RoutingAPIClientTests {
    @Test("Health policies and targets decode through the private router socket")
    func readsControlPlane() async throws {
        let healthServer = try RoutingFixtureServer(json: #"{"schemaVersion":"1.0","health":{"ok":true,"status":"disabled"},"decisionApiEnabled":false,"defaultPolicyId":"balanced","preferencesRevision":3,"targetRevision":4,"targetCount":1,"routingRevision":9}"#)
        let health = try await RoutingAPIClient(socketURL: healthServer.url).health()
        #expect(health.ok)
        #expect(!health.decisionAPIEnabled)
        #expect(health.defaultPolicyID == "balanced")
        #expect(health.preferencesRevision == 3)
        #expect(health.targetRevision == 4)
        #expect(health.targetCount == 1)
        #expect(healthServer.request.contains("GET /v1/health HTTP/1.1"))

        let previousServer = try RoutingFixtureServer(json: #"{"schemaVersion":"1.0","health":{"ok":true,"status":"ok"},"targetRevision":3,"targetCount":0,"routingRevision":8}"#)
        let previous = try await RoutingAPIClient(socketURL: previousServer.url).health()
        #expect(previous.decisionAPIEnabled)
        #expect(previous.defaultPolicyID == "reliable")
        #expect(previous.preferencesRevision == 0)

        let policiesServer = try RoutingFixtureServer(json: #"{"schemaVersion":"1.0","policies":[{"policyId":"reliable","policyRevision":1,"weights":{"reliability":50,"headroom":25,"latency":15,"cost":10},"requirements":{"minimumHeadroomBasisPoints":1000,"maximumErrorRateBasisPoints":2500,"minimumRuntimeSamples":0,"requireCost":false,"requireRuntime":false}}]}"#)
        let policies = try await RoutingAPIClient(socketURL: policiesServer.url).policies()
        #expect(policies.map(\.policyID) == ["reliable"])
        #expect(policies.first?.weights.reliability == 50)
        #expect(policiesServer.request.contains("GET /v1/policies HTTP/1.1"))

        let targetsServer = try RoutingFixtureServer(json: #"{"schemaVersion":"1.0","targetRevision":5,"targets":[{"targetId":"openai.work.gpt-5","providerId":"openai","accountRef":"account-1","modelId":"gpt-5","connectionRef":"connection-1","executionClass":"direct_api","executionAdapterId":"openai.direct","resourceMode":"quota","factAccountRef":"account-1","runtimeScopeRef":null,"balanceCurrency":null,"costCurrency":"USD","inputCostMicrosPerMillion":3000000,"outputCostMicrosPerMillion":3000000,"enabled":true,"adapterAvailable":true,"regions":["global"],"privacyClass":"direct_provider","capabilities":["chat","reasoning","tools"],"contextWindowTokens":400000,"qualityTier":4}]}"#)
        let targetDocument = try await RoutingAPIClient(socketURL: targetsServer.url).targets()
        let target = try #require(targetDocument.targets.first)
        #expect(targetDocument.revision == 5)
        #expect(target.targetID == "openai.work.gpt-5")
        #expect(target.adapterAvailable)
        #expect(target.capabilities == ["chat", "reasoning", "tools"])
        #expect(targetsServer.request.contains("GET /v1/targets HTTP/1.1"))
    }

    @Test("Dry run posts metadata only and decodes explanations")
    func simulatesContentFreeDecision() async throws {
        let server = try RoutingFixtureServer(json: Self.decisionJSON(simulated: true))
        let request = RoutingDecisionRequest(
            policyID: "reliable",
            task: RoutingTask(
                kind: .code,
                requiredCapabilities: [.chat, .reasoning, .tools],
                estimatedInputTokens: 12_000,
                maxOutputTokens: 4_000,
                minimumContextWindowTokens: 16_000,
                privacy: .directProvider,
                regions: ["global"]
            )
        )
        let decision = try await RoutingAPIClient(socketURL: server.url).simulate(request)
        #expect(decision.simulated)
        #expect(decision.selected?.targetID == "openai.work.gpt-5")
        #expect(decision.selected?.score == 8_750)
        #expect(decision.rejected.first?.reasonCodes == ["stale_resource_fact"])
        #expect(server.request.contains("POST /v1/simulations HTTP/1.1"))
        #expect(server.request.contains("\"policyId\":\"reliable\""))
        #expect(!server.request.localizedCaseInsensitiveContains("prompt"))
        #expect(!server.request.localizedCaseInsensitiveContains("message"))
    }

    @Test("No-route simulation remains an explainable result")
    func noRouteIsData() async throws {
        let details = #"{"decisionId":"route_0123456789abcdef0123456789abcdef","generatedAt":"2026-08-02T10:00:00Z","expiresAt":"2026-08-02T10:01:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":7,"runtimeRevision":null},"rejected":[{"targetId":"minimax.primary.m3","reasonCodes":["insufficient_headroom"]}],"evidenceStored":false,"simulated":true}"#
        let body = #"{"schemaVersion":"1.0","error":{"code":"no_route","message":"No eligible route is available.","details":\#(details)}}"#
        let server = try RoutingFixtureServer(json: body, status: "409 Conflict")
        let decision = try await RoutingAPIClient(socketURL: server.url).simulate(.minimal())
        #expect(decision.selected == nil)
        #expect(decision.rejected.first?.targetID == "minimax.primary.m3")
        #expect(decision.rejected.first?.reasonCodes == ["insufficient_headroom"])
    }

    @Test("Decision history exposes content-free target evidence only")
    func readsContentFreeHistory() async throws {
        let server = try RoutingFixtureServer(json: #"{"schemaVersion":"1.0","routingRevision":12,"decisions":[{"decisionId":"route_0123456789abcdef0123456789abcdef","generatedAt":"2026-08-02T10:00:00Z","expiresAt":"2026-08-02T10:01:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":7,"runtimeRevision":8},"clientRequestRef":null,"sessionRef":null,"selected":{"targetId":"openai.work.gpt-5","score":8750,"components":{"reliability":5000,"headroom":2500,"latency":750,"cost":500},"reasons":["highest_score"]},"alternatives":[],"rejected":[]}],"nextBefore":null}"#)
        let page = try await RoutingAPIClient(socketURL: server.url).history(limit: 20)
        #expect(page.revision == 12)
        #expect(page.decisions.first?.selected?.targetID == "openai.work.gpt-5")
        #expect(server.request.contains("GET /v1/decisions?limit=20 HTTP/1.1"))
    }

    @Test("Shadow evaluation posts only decision metadata and reads bounded history")
    func evaluatesShadow() async throws {
        let evaluation = #"{"schemaVersion":"1.0","shadowId":"shadow_0123456789abcdef0123456789abcdef","createdAt":"2026-08-02T10:00:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":7,"runtimeRevision":8},"actualTargetId":"minimax.primary.m3","recommendedTargetId":"openai.work.gpt-5","actualState":"eligible","actualRejectionCodes":[],"agreement":false,"recommendedScore":8750,"actualScore":7200,"scoreAdvantage":1550,"estimatedCostDeltaMicrounits":-75,"costCurrency":"USD","estimatedLatencyDeltaMilliseconds":-600,"evidenceStored":true}"#
        let server = try RoutingFixtureServer(json: evaluation)

        let result = try await RoutingAPIClient(socketURL: server.url).shadow(
            .minimal(), actualTargetID: "minimax.primary.m3"
        )

        #expect(result.actualTargetID == "minimax.primary.m3")
        #expect(result.recommendedTargetID == "openai.work.gpt-5")
        #expect(result.scoreAdvantage == 1_550)
        #expect(result.evidenceStored == true)
        #expect(server.request.contains("POST /v1/shadow-decisions HTTP/1.1"))
        #expect(server.request.contains("\"actualTargetId\":\"minimax.primary.m3\""))
        #expect(!server.request.localizedCaseInsensitiveContains("prompt"))

        let historyJSON = #"{"schemaVersion":"1.0","routingRevision":13,"shadows":[{"shadowId":"shadow_0123456789abcdef0123456789abcdef","createdAt":"2026-08-02T10:00:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":7,"runtimeRevision":8},"actualTargetId":"minimax.primary.m3","recommendedTargetId":"openai.work.gpt-5","actualState":"eligible","actualRejectionCodes":[],"agreement":false,"recommendedScore":8750,"actualScore":7200,"scoreAdvantage":1550,"estimatedCostDeltaMicrounits":-75,"costCurrency":"USD","estimatedLatencyDeltaMilliseconds":-600}],"nextBefore":null}"#
        let historyServer = try RoutingFixtureServer(json: historyJSON)
        let history = try await RoutingAPIClient(socketURL: historyServer.url)
            .shadowHistory(before: nil, limit: 20)
        #expect(history.revision == 13)
        #expect(history.shadows.first?.agreement == false)
        #expect(historyServer.request.contains(
            "GET /v1/shadow-decisions?limit=20 HTTP/1.1"
        ))
    }

    @Test("Replay sends one bounded frozen fixture and decodes aggregate evidence")
    func replaysFrozenEvidence() async throws {
        let reportJSON = #"{"schemaVersion":"1.0","policy":{"policyId":"reliable","policyRevision":1},"summary":{"caseCount":2,"selectionCount":2,"agreementCount":1,"agreementBasisPoints":5000,"noRouteCount":0,"noRouteBasisPoints":0,"actualRejectedCount":0,"actualRejectedBasisPoints":0,"comparableScoreCount":2,"meanScoreAdvantage":775,"comparableLatencyCount":2,"meanEstimatedLatencyDeltaMilliseconds":-300,"costDeltas":[{"currency":"USD","caseCount":2,"totalDeltaMicrounits":-75,"meanDeltaMicrounits":-37}]},"cases":[{"caseId":"case-one","facts":{"dataRevision":7,"runtimeRevision":8},"actualTargetId":"minimax.primary.m3","recommendedTargetId":"openai.work.gpt-5","actualState":"eligible","actualRejectionCodes":[],"agreement":false,"recommendedScore":8750,"actualScore":7200,"scoreAdvantage":1550,"estimatedCostDeltaMicrounits":-75,"costCurrency":"USD","estimatedLatencyDeltaMilliseconds":-600},{"caseId":"case-two","facts":{"dataRevision":7,"runtimeRevision":8},"actualTargetId":"openai.work.gpt-5","recommendedTargetId":"openai.work.gpt-5","actualState":"eligible","actualRejectionCodes":[],"agreement":true,"recommendedScore":8750,"actualScore":8750,"scoreAdvantage":0,"estimatedCostDeltaMicrounits":0,"costCurrency":"USD","estimatedLatencyDeltaMilliseconds":0}]}"#
        let server = try RoutingFixtureServer(json: reportJSON)
        let fixture = Data(#"{"schemaVersion":"1.0","policyId":"reliable","cases":[{}]}"#.utf8)

        let report = try await RoutingAPIClient(socketURL: server.url).replay(fixture)

        #expect(report.summary.caseCount == 2)
        #expect(report.summary.agreementBasisPoints == 5_000)
        #expect(report.summary.costDeltas.first?.totalDeltaMicrounits == -75)
        #expect(report.cases.first?.caseID == "case-one")
        #expect(server.request.contains("POST /v1/replays HTTP/1.1"))
    }

    @Test("Schema drift oversized output and invalid requests fail closed")
    func failsClosed() async throws {
        let drift = try RoutingFixtureServer(json: #"{"schemaVersion":"2.0","health":{"ok":true,"status":"ok"},"decisionApiEnabled":true,"defaultPolicyId":"reliable","preferencesRevision":0,"targetRevision":1,"targetCount":0,"routingRevision":0}"#)
        await #expect(throws: RoutingAPIClientError.schemaMismatch) {
            try await RoutingAPIClient(socketURL: drift.url).health()
        }

        let oversized = try RoutingFixtureServer(json: String(repeating: "x", count: 2_049))
        await #expect(throws: RoutingAPIClientError.responseTooLarge) {
            try await RoutingAPIClient(socketURL: oversized.url, maximumBodyBytes: 2_048)
                .health()
        }

        let invalid = RoutingDecisionRequest(
            policyID: String(repeating: "x", count: 129),
            task: RoutingTask(kind: .code)
        )
        await #expect(throws: RoutingAPIClientError.invalidRequest) {
            try await RoutingAPIClient(socketURL: oversized.url).simulate(invalid)
        }
    }

    private static func decisionJSON(simulated: Bool) -> String {
        #"{"schemaVersion":"1.0","decisionId":"route_0123456789abcdef0123456789abcdef","generatedAt":"2026-08-02T10:00:00Z","expiresAt":"2026-08-02T10:01:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":7,"runtimeRevision":8},"selected":{"targetId":"openai.work.gpt-5","providerId":"openai","accountRef":"account-1","modelId":"gpt-5","score":8750,"components":{"reliability":5000,"headroom":2500,"latency":750,"cost":500},"reasons":["highest_score"]},"alternatives":[],"rejected":[{"targetId":"minimax.primary.m3","reasonCodes":["stale_resource_fact"]}],"warnings":[],"evidenceStored":false,"simulated":\#(simulated)}"#
    }
}

private final class RoutingFixtureServer: @unchecked Sendable {
    let url: URL
    private let descriptor: Int32
    private let queue = DispatchQueue(label: "RoutingAPIClientTests.server")
    private let lock = NSLock()
    private var capturedRequest = ""

    var request: String { lock.withLock { capturedRequest } }

    init(json: String, status: String = "200 OK") throws {
        let body = Data(json.utf8)
        let response = Data("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8) + body
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        url = directory.appendingPathComponent("router.sock")
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
            var request = Data()
            var buffer = [UInt8](repeating: 0, count: 4_096)
            while request.count < 65_536 {
                let count = Darwin.read(client, &buffer, buffer.count)
                guard count > 0 else { break }
                request.append(buffer, count: count)
                if let headerRange = request.range(of: Data("\r\n\r\n".utf8)) {
                    let header = String(decoding: request[..<headerRange.lowerBound], as: UTF8.self)
                    let length = header.split(separator: "\r\n").compactMap { line -> Int? in
                        let parts = line.split(separator: ":", maxSplits: 1)
                        guard parts.count == 2,
                              parts[0].trimmingCharacters(in: .whitespaces)
                                .lowercased() == "content-length"
                        else { return nil }
                        return Int(parts[1].trimmingCharacters(in: .whitespaces))
                    }.first ?? 0
                    if request.count >= headerRange.upperBound + length { break }
                }
            }
            lock.withLock { capturedRequest = String(decoding: request, as: UTF8.self) }
            response.withUnsafeBytes { bytes in
                if let base = bytes.baseAddress { _ = Darwin.write(client, base, response.count) }
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
