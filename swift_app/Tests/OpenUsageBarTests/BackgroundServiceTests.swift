import Testing
@testable import OpenUsageBar

@Suite("Background service bootstrap")
struct BackgroundServiceTests {
    @Test("Unregistered services are registered without asking for terminal access")
    func registrationPlan() {
        let plan = BackgroundServicePlan.make(
            loginItem: .notRegistered,
            collector: .notRegistered
        )

        #expect(plan.actions == [.registerLoginItem, .registerCollector])
        #expect(!plan.requiresApproval)
    }

    @Test("Already enabled services are idempotent")
    func enabledPlan() {
        let plan = BackgroundServicePlan.make(loginItem: .enabled, collector: .enabled)

        #expect(plan.actions.isEmpty)
        #expect(!plan.requiresApproval)
    }

    @Test("Legacy script installations remain valid without duplicate registration")
    func legacyPlan() {
        let plan = BackgroundServicePlan.make(
            loginItem: .notRegistered,
            collector: .notRegistered,
            legacyLoginItem: true,
            legacyCollector: true
        )

        #expect(plan.actions.isEmpty)
        #expect(!plan.requiresApproval)
        #expect(!plan.hasPackagingError)
    }

    @Test("System approval and missing bundle resources are surfaced")
    func attentionPlan() {
        let approval = BackgroundServicePlan.make(
            loginItem: .requiresApproval,
            collector: .enabled
        )
        let missing = BackgroundServicePlan.make(
            loginItem: .enabled,
            collector: .notFound
        )

        #expect(approval.actions.isEmpty)
        #expect(approval.requiresApproval)
        #expect(missing.hasPackagingError)
    }
}
