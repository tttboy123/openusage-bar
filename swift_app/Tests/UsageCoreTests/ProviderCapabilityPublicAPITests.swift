import Testing
import UsageCore

@Suite("Provider capability public API")
struct ProviderCapabilityPublicAPITests {
    @Test("Quota capability rejects every invalid state and value combination")
    func rejectsInvalidQuotaCombinations() {
        #expect(capability(state: .supported, values: []) == nil)
        #expect(capability(state: .unknown, values: [.weekly]) == nil)
        #expect(capability(state: .unsupported, values: [.session]) == nil)
    }

    @Test("Quota capability accepts every valid state and value combination")
    func acceptsValidQuotaCombinations() {
        #expect(capability(state: .supported, values: [.fiveHour]) != nil)
        #expect(capability(state: .unknown, values: []) != nil)
        #expect(capability(state: .unsupported, values: []) != nil)
    }

    private func capability(
        state: ProviderCapabilityState,
        values: Set<ProviderQuotaWindow>
    ) -> ProviderQuotaWindowCapability? {
        ProviderQuotaWindowCapability(state: state, values: values)
    }
}
