import Foundation
import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("API spend balance presentation")
struct APISpendBalanceTests {
    private func range() -> ClosedRange<LocalDay> {
        let start = try! LocalDay("2026-08-01")
        let end = try! LocalDay("2026-08-04")
        return start ... end
    }

    private func balance(
        provider: String, currency: String, recordID: String
    ) -> BalanceRecord {
        BalanceRecord(
            recordID: recordID, observedAt: "2026-08-04T04:00:00Z",
            providerID: provider, accountRef: "openusage", currency: currency,
            available: "305.98", voucher: nil, cash: nil, state: "ok",
            quality: "derived", stale: false, revision: 2,
            sourceID: "openusage.deepseek.balance"
        )
    }

    @Test("Balance records are included and sorted by provider then currency")
    func balancesIncludedAndSorted() {
        let summary = APISpendAggregator.make(
            costs: DailyCostDataset(
                records: [], coverage: [], knownScopes: [], revision: 1
            ),
            legacyRecords: [],
            range: range(),
            isLegacyCoverageComplete: true,
            balances: [
                balance(provider: "zai", currency: "USD", recordID: "z"),
                balance(provider: "deepseek", currency: "CNY", recordID: "d"),
            ]
        )

        #expect(summary.balances.map(\.providerID) == ["deepseek", "zai"])
        #expect(summary.balances.first?.available == "305.98")
        #expect(summary.balances.first?.currency == "CNY")
    }

    @Test("Missing spend still preserves balance facts")
    func balanceWithoutSpend() {
        let summary = APISpendAggregator.make(
            costs: DailyCostDataset(
                records: [], coverage: [], knownScopes: [], revision: 1
            ),
            legacyRecords: [],
            range: range(),
            isLegacyCoverageComplete: false,
            balances: [balance(provider: "deepseek", currency: "CNY", recordID: "d")]
        )

        #expect(summary.totals.isEmpty)
        #expect(summary.balances.count == 1)
        #expect(summary.coverage == .missing)
    }
}
