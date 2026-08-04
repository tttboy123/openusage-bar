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

    @Test("Proxy management resolves only the fixed local CLI surface")
    func routingProxyCommand() throws {
        let command = try #require(ProviderMutationCommand.resolveProxy(
            action: .enable,
            activityBundleURL: URL(fileURLWithPath: "/tmp/OpenUsageActivity.app"),
            activityExecutableURL: URL(fileURLWithPath: "/tmp/OpenUsageActivity"),
            isExecutable: { $0.lastPathComponent == "OpenUsageSettings" }
        ))
        #expect(command.arguments == ["proxy", "enable", "--format", "json"])
    }

    @Test("Proxy management validates status and returns a generated token once")
    func routingProxyClient() async throws {
        let statusScript = #"printf '{\"schemaVersion\":\"1.0\",\"enabled\":false,\"endpoint\":\"http://127.0.0.1:64123/v1\",\"configurationRevision\":0}'"#
        let client = RoutingProxyManagementClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let status = await client.run(
            action: .status,
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", statusScript]
            )
        )
        #expect(status == .success(.init(
            enabled: false,
            endpoint: "http://127.0.0.1:64123/v1",
            configurationRevision: 0,
            restartRequired: false,
            bearerToken: nil
        )))

        let token = "generated-proxy-token-0123456789abcdef"
        let enableScript = #"printf '{\"schemaVersion\":\"1.0\",\"enabled\":true,\"endpoint\":\"http://127.0.0.1:64123/v1\",\"configurationRevision\":1,\"restartRequired\":false,\"bearerToken\":\"generated-proxy-token-0123456789abcdef\"}'"#
        let enabled = await client.run(
            action: .enable,
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", enableScript]
            )
        )
        #expect(enabled == .success(.init(
            enabled: true,
            endpoint: "http://127.0.0.1:64123/v1",
            configurationRevision: 1,
            restartRequired: false,
            bearerToken: token
        )))
    }

    @Test("Proxy management rejects remote endpoints and unexpected secret replay")
    func routingProxyClientRejectsUnsafeStatus() async {
        let client = RoutingProxyManagementClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let responses = [
            #"{\"schemaVersion\":\"1.0\",\"enabled\":true,\"endpoint\":\"https://remote.example/v1\",\"configurationRevision\":1}"#,
            #"{\"schemaVersion\":\"1.0\",\"enabled\":true,\"endpoint\":\"http://127.0.0.1:64123/v1\",\"configurationRevision\":1,\"bearerToken\":\"replayed-private-token-0123456789\"}"#,
        ]
        for response in responses {
            let result = await client.run(
                action: .status,
                command: .init(
                    executableURL: URL(fileURLWithPath: "/bin/sh"),
                    arguments: ["-c", "printf '\(response)'"]
                )
            )
            #expect(result == .failure(.invalidResponse))
        }
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

    @Test("Provider Center inference templates import without exposing the saved key")
    func providerExecutionImport() async throws {
        let templateScript = #"read payload; printf '{"version":1,"ok":true,"message":"Provider execution templates loaded","providerExecutionTemplates":[{"providerId":"step-main","familyId":"step_plan","displayName":"Step Plan","site":"china","baseURL":"https://api.stepfun.com/step_plan/v1","suggestedModels":["step-3.5-flash"],"credentialAvailable":true}]}'"#
        let client = RoutingConnectionMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 8_192),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let command = ProviderMutationCommand(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", templateScript]
        )

        let loaded = try await client.loadProviderExecutionTemplates(
            command: command
        ).get()
        let template = try #require(loaded.providerExecutionTemplates?.first)
        #expect(template.providerID == "step-main")
        #expect(template.familyID == "step_plan")
        #expect(template.credentialAvailable)
        #expect(template.suggestedModels == ["step-3.5-flash"])

        let request = RoutingProviderExecutionImportRequest(
            expectedRevision: 2,
            providerID: template.providerID,
            connectionRef: "conn_step_main",
            models: template.suggestedModels,
            enabled: true
        )
        let object = try #require(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)
        ) as? [String: Any])
        #expect(object["action"] as? String == "import_provider_execution_connection")
        #expect(object["providerId"] as? String == "step-main")
        #expect(object["baseURL"] == nil)
        #expect(object["secret"] == nil)
        #expect(object["apiKey"] == nil)
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

    @Test("Routing preferences expose only the master toggle and default policy")
    func routingPreferences() async throws {
        let script = #"read payload; printf '{"version":1,"ok":true,"message":"Routing preferences loaded","preferencesRevision":2,"routingPreferences":{"decisionApiEnabled":false,"defaultPolicyId":"balanced"}}'"#
        let client = RoutingPreferencesMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        )
        let response = await client.loadPreferences(command: .init(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", script]
        ))
        let loaded = try response.get()
        #expect(loaded.preferencesRevision == 2)
        #expect(loaded.routingPreferences == .init(
            decisionAPIEnabled: false, defaultPolicyID: "balanced"
        ))

        let request = RoutingPreferencesMutationRequest(
            expectedRevision: 2,
            decisionAPIEnabled: true,
            defaultPolicyID: "reliable"
        )
        let object = try #require(JSONSerialization.jsonObject(
            with: JSONEncoder().encode(request)
        ) as? [String: Any])
        #expect(object["action"] as? String == "set_preferences")
        #expect(object["decisionApiEnabled"] as? Bool == true)
        #expect(object["defaultPolicyId"] as? String == "reliable")
        #expect(object["secret"] == nil)
    }

    @Test("Routing mutation clients reject fields owned by another response mode")
    func routingResponseIsolation() async {
        let command = ProviderMutationCommand(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", #"read payload; printf '{"version":1,"ok":true,"message":"Execution connections loaded","connectionRevision":0,"connections":[],"preferencesRevision":1}'"#]
        )
        let response = await RoutingConnectionMutationClient(
            limits: .init(timeout: .seconds(1), maximumResponseBytes: 4_096),
            environment: ["PATH": "/usr/bin:/bin", "HOME": "/Users/tester"]
        ).loadConnections(command: command)
        #expect(response == .failure(.invalidResponse))
    }
}
