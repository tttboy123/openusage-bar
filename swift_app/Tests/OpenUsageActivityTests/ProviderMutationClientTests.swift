import Foundation
import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("Provider mutation process isolation")
struct ProviderMutationClientTests {
    @Test("Provider mutation helper receives only the approved parent environment")
    func sanitizedEnvironment() async {
        let environment = [
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/tester",
            "TMPDIR": "/tmp/provider-mutation-tests",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "OPENAI_API_KEY": "must-not-pass",
            "MINIMAX_TOKEN": "must-not-pass",
            "COOKIE": "must-not-pass",
            "ARBITRARY_PRIVATE_VALUE": "must-not-pass",
        ]
        let client = ProviderMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 1_024),
            environment: environment
        )
        let script = #"""
        allowed=present
        [ "$PATH" = "/usr/bin:/bin" ] || allowed=missing
        [ "$HOME" = "/Users/tester" ] || allowed=missing
        [ "$TMPDIR" = "/tmp/provider-mutation-tests" ] || allowed=missing
        [ "$LANG" = "en_US.UTF-8" ] || allowed=missing
        [ "$LC_ALL" = "en_US.UTF-8" ] || allowed=missing
        leaked=none
        [ -z "${OPENAI_API_KEY+x}" ] || leaked=secret
        [ -z "${MINIMAX_TOKEN+x}" ] || leaked=secret
        [ -z "${COOKIE+x}" ] || leaked=secret
        [ -z "${ARBITRARY_PRIVATE_VALUE+x}" ] || leaked=secret
        printf '{"version":1,"ok":true,"message":"%s/%s"}' "$allowed" "$leaked"
        """#

        let result = await client.submit(
            ProviderEditRequest(
                providerID: "step-plan-main", name: "Main",
                apiKey: "replacement", sessionCookie: ""
            ),
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", script]
            )
        )

        #expect(result == .success(.init(
            version: 1, ok: true, message: "present/none"
        )))
    }

    @Test("Routing mutations use the same isolated helper with a distinct command")
    func routingMutation() async throws {
        let command = try #require(ProviderMutationCommand.resolveRouting(
            activityBundleURL: URL(fileURLWithPath: "/tmp/OpenUsageActivity.app"),
            activityExecutableURL: URL(fileURLWithPath: "/tmp/OpenUsageActivity"),
            isExecutable: { $0.lastPathComponent == "OpenUsageSettings" }
        ))
        #expect(command.arguments == ["routing-mutate"])

        let script = #"read payload; printf '{"version":1,"ok":true,"message":"Routing targets saved","targetRevision":4}'"#
        let client = RoutingMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 1_024),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let response = await client.submit(
            RoutingTargetMutationRequest(expectedRevision: 3, targets: []),
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", script]
            )
        )
        #expect(response == .success(.init(
            version: 1, ok: true,
            message: "Routing targets saved", targetRevision: 4
        )))
    }

    @Test("Routing target mutation drops server-derived adapter state")
    func routingTargetWire() throws {
        let target = try JSONDecoder().decode(RoutingTarget.self, from: Data(#"{"targetId":"openai.work.gpt-5","providerId":"openai","accountRef":"account-1","modelId":"gpt-5","connectionRef":"connection-1","executionClass":"direct_api","executionAdapterId":"openai.direct","resourceMode":"quota","factAccountRef":"account-1","runtimeScopeRef":null,"balanceCurrency":null,"costCurrency":"USD","inputCostMicrosPerMillion":3000000,"outputCostMicrosPerMillion":3000000,"enabled":true,"adapterAvailable":true,"regions":["global"],"privacyClass":"direct_provider","capabilities":["chat","reasoning","tools"],"contextWindowTokens":400000,"qualityTier":4}"#.utf8))
        let request = RoutingTargetMutationRequest(
            expectedRevision: 7,
            targets: [.init(target: target, enabled: false)]
        )
        let encoded = try JSONEncoder().encode(request)
        let object = try #require(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        let targets = try #require(object["targets"] as? [[String: Any]])
        let wire = try #require(targets.first)
        #expect(object["expectedRevision"] as? Int == 7)
        #expect(wire["enabled"] as? Bool == false)
        #expect(wire["adapterAvailable"] == nil)
        #expect(wire["apiKey"] == nil)
        #expect(wire["endpoint"] == nil)
    }
}
