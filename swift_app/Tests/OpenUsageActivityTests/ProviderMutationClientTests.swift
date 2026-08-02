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

    @Test("Routing connection configuration loads and mutates through private stdin")
    func routingConnections() async throws {
        let script = #"read payload; printf '{"version":1,"ok":true,"message":"Execution connections loaded","connectionRevision":2,"connections":[{"connectionRef":"conn_0123456789abcdef","providerId":"openai","accountRef":"account-1","executionClass":"openai_compatible","executionAdapterId":"openai_compatible.direct","baseURL":"https://api.example.com/v1","enabled":true,"models":["gpt-5"]}]}'"#
        let client = RoutingConnectionMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let response = await client.loadConnections(command: .init(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", script]
        ))
        let loaded = try response.get()
        #expect(loaded.connectionRevision == 2)
        #expect(loaded.connections?.first?.connectionRef == "conn_0123456789abcdef")
        #expect(loaded.connections?.first?.models == ["gpt-5"])

        let request = RoutingConnectionMutationRequest(
            expectedRevision: 2,
            connection: try #require(loaded.connections?.first),
            secret: "replacement-secret"
        )
        let object = try #require(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)
        ) as? [String: Any])
        #expect(object["action"] as? String == "upsert_connection")
        #expect(object["secret"] as? String == "replacement-secret")
        #expect(object["credentialMaterial"] == nil)
    }

    @Test("Custom routing policies mutate without credential fields")
    func routingPolicies() async throws {
        let script = #"read payload; printf '{"version":1,"ok":true,"message":"Custom routing policies loaded","policyDocumentRevision":0,"customPolicies":[]}'"#
        let client = RoutingPolicyMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let response = await client.loadPolicies(command: .init(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", script]
        ))
        let loaded = try response.get()
        #expect(loaded.policyDocumentRevision == 0)
        #expect(loaded.customPolicies == [])

        let request = RoutingPolicyMutationRequest(
            expectedRevision: 0,
            policy: RoutingPolicyDraft(generatedID: "custom_coding").mutationValue!
        )
        let object = try #require(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)
        ) as? [String: Any])
        #expect(object["action"] as? String == "upsert_policy")
        #expect(object["secret"] == nil)
        #expect(object["apiKey"] == nil)
    }

    @Test("Custom policy helper rejects control characters in status messages")
    func routingPolicyControlCharacters() async throws {
        let script = #"read payload; printf '%s' '{"version":1,"ok":true,"message":"bad\u0001","policyDocumentRevision":1}'"#
        let client = RoutingPolicyMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let policy = try #require(
            RoutingPolicyDraft(generatedID: "custom_coding").mutationValue
        )

        let response = await client.upsertPolicy(
            RoutingPolicyMutationRequest(expectedRevision: 0, policy: policy),
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", script]
            )
        )

        #expect(response == .failure(.invalidResponse))
    }
}
