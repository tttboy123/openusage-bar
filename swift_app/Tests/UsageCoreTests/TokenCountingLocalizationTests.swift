import Foundation
import Testing
@testable import UsageCore

@Suite("Token counting localization contract")
struct TokenCountingLocalizationTests {
    @Test("Every aggregate counting convention ships English and Simplified Chinese copy")
    func bilingualDescriptions() throws {
        let swiftRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let english = try String(
            contentsOf: swiftRoot.appendingPathComponent(
                "Resources/en.lproj/Localizable.strings"
            ), encoding: .utf8
        )
        let chinese = try String(
            contentsOf: swiftRoot.appendingPathComponent(
                "Resources/zh-Hans.lproj/Localizable.strings"
            ), encoding: .utf8
        )

        for convention in AggregatedTokenCountingConvention.allCases {
            let entry = "\"\(convention.localizationKey)\" = "
            #expect(english.contains(entry), "Missing English copy for \(convention)")
            #expect(chinese.contains(entry), "Missing Chinese copy for \(convention)")
        }
        #expect(chinese.contains("缓存读取是输入 Token 的子集"))
        #expect(chinese.contains("混合了多种 Token 统计口径"))
    }
}
