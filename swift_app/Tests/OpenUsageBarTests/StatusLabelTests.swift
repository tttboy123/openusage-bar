import Testing
@testable import OpenUsageBar

@Suite("Persistent status label")
struct StatusLabelTests {
    @Test("Compact formats the most urgent remaining ratio")
    func compactRatio() {
        #expect(StatusLabel.compact(remainingRatio: 0.18).text == "18%")
    }

    @Test("Activity formats today's Token total")
    func activityTokens() {
        #expect(StatusLabel.activity(tokens: 74_200_000).text == "74.2M")
    }

    @Test("Missing compact capacity leaves only the template icon")
    func compactMissing() {
        #expect(StatusLabel.compact(remainingRatio: nil).text == nil)
    }

    @Test("Custom status is capped at two short values")
    func customCap() {
        let label = StatusLabel.custom(values: ["18%", "74.2M", "$4.20"])
        #expect(label.values == ["18%", "74.2M"])
        #expect(!(label.text ?? "").contains("MiniMax"))
    }

    @Test("Every status exposes meaningful accessibility text")
    func accessibility() {
        let label = StatusLabel.compact(remainingRatio: 0.18)
        #expect(!label.accessibilityTitle.isEmpty)
        #expect(!label.accessibilityValue.isEmpty)
    }
}
