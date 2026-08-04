import Foundation
import Testing
@testable import OpenUsageActivity

@Suite("Provider connection draft persistence")
struct ProviderConnectionDraftTests {
    private func isolatedDefaults() -> UserDefaults {
        let suite = "OpenUsageActivityDraftTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    @Test("Non-secret draft round trips through the store")
    func roundTrip() {
        let defaults = isolatedDefaults()
        let store = ProviderConnectionDraftStore(defaults: defaults)
        let draft = ProviderConnectionDraft(
            kind: "daily_usage_feed",
            providerID: "zai-work",
            name: "ZAI Work",
            endpoint: "https://api.example.com/usage",
            familyID: "zai",
            itemsPath: "data.items"
        )

        store.save(draft, familyID: "zai", providerID: "zai-work")

        let restored = store.load(familyID: "zai", providerID: "zai-work")
        #expect(restored == draft)
    }

    @Test("Clearing removes the draft for that connection only")
    func clearScopesToConnection() {
        let defaults = isolatedDefaults()
        let store = ProviderConnectionDraftStore(defaults: defaults)
        store.save(
            ProviderConnectionDraft(name: "A"),
            familyID: "minimax", providerID: "minimax-a"
        )
        store.save(
            ProviderConnectionDraft(name: "B"),
            familyID: "minimax", providerID: "minimax-b"
        )

        store.clear(familyID: "minimax", providerID: "minimax-a")

        #expect(store.load(familyID: "minimax", providerID: "minimax-a") == nil)
        #expect(store.load(familyID: "minimax", providerID: "minimax-b") != nil)
    }

    @Test("Drafts are isolated between families and connections")
    func isolation() {
        let defaults = isolatedDefaults()
        let store = ProviderConnectionDraftStore(defaults: defaults)
        store.save(
            ProviderConnectionDraft(name: "MiniMax"),
            familyID: "minimax", providerID: "minimax-work"
        )

        #expect(store.load(familyID: "minimax", providerID: "minimax-other") == nil)
        #expect(store.load(familyID: "step_plan", providerID: "minimax-work") == nil)
    }

    @Test("Reopening restores the last non-secret draft")
    func restoreAfterDismiss() {
        let defaults = isolatedDefaults()
        let store = ProviderConnectionDraftStore(defaults: defaults)
        var draft = ProviderConnectionDraft(
            kind: "generic",
            providerID: "deepseek",
            name: "DeepSeek",
            endpoint: "https://api.example.com/quota",
            familyID: "deepseek"
        )
        draft.authPrefix = "Bearer"
        draft.primaryPath = "data.remaining_percent"
        draft.remainingPercentPath = "data.remaining_percent"
        store.save(draft, familyID: "deepseek", providerID: "deepseek")

        let restored = store.load(familyID: "deepseek", providerID: "deepseek")

        #expect(restored?.name == "DeepSeek")
        #expect(restored?.endpoint == "https://api.example.com/quota")
        #expect(restored?.primaryPath == "data.remaining_percent")
    }

    @Test("Draft diff detects only real user changes")
    func changeDetection() {
        let baseline = ProviderConnectionDraft(
            kind: "minimax", providerID: "minimax-work",
            name: "MiniMax Work", site: "china", familyID: "minimax"
        )
        #expect(baseline.hasChanges(from: baseline) == false)

        var changed = baseline
        changed.site = "international"
        #expect(changed.hasChanges(from: baseline) == true)

        var renamed = baseline
        renamed.name = "MiniMax Home"
        #expect(renamed.hasChanges(from: baseline) == true)
    }

    @Test("Persisted draft never contains credential-shaped fields")
    func privacyContract() throws {
        let draft = ProviderConnectionDraft(
            kind: "step_plan",
            providerID: "step-work",
            name: "Step Plan",
            site: "china",
            familyID: "step_plan"
        )
        let data = try JSONEncoder().encode(draft)
        let payload = String(decoding: data, as: UTF8.self)

        #expect(!payload.contains("credential"))
        #expect(!payload.contains("secret"))
        #expect(!payload.contains("session"))
        #expect(!payload.contains("replacement"))
        #expect(!payload.contains("apiKey"))
        #expect(!payload.contains("tokenMaterial"))
    }
}
