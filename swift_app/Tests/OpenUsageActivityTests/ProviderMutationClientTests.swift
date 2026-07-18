import Foundation
import Testing
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
}
