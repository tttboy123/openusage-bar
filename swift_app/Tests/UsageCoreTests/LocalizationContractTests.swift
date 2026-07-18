import Foundation
import Testing
@testable import UsageCore

@Suite("Bilingual localization contract")
struct LocalizationContractTests {
    @Test("English and Simplified Chinese catalogs are unique and structurally identical")
    func catalogsMatch() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let resources = root.appendingPathComponent("swift_app/Resources")
        let english = try Catalog(
            url: resources.appendingPathComponent("en.lproj/Localizable.strings")
        )
        let chinese = try Catalog(
            url: resources.appendingPathComponent("zh-Hans.lproj/Localizable.strings")
        )

        #expect(english.duplicates.isEmpty)
        #expect(chinese.duplicates.isEmpty)
        #expect(Set(english.values.keys) == Set(chinese.values.keys))
        for key in english.values.keys {
            #expect(placeholders(english.values[key] ?? "") == placeholders(chinese.values[key] ?? ""))
        }
    }

    @Test("Parameterized copy has a deterministic English fallback")
    func formattedFallback() {
        #expect(AppLocalization.format("Collected %@", "Jul 18") == "Collected Jul 18")
        #expect(AppLocalization.format("%lld days", Int64(3)) == "3 days")
    }
}

private struct Catalog {
    let values: [String: String]
    let duplicates: Set<String>

    init(url: URL) throws {
        let text = try String(contentsOf: url, encoding: .utf8)
        let expression = try NSRegularExpression(
            pattern: #"(?m)^\"((?:\\.|[^\"\\])*)\"\s*=\s*\"((?:\\.|[^\"\\])*)\"\s*;"#
        )
        let range = NSRange(text.startIndex..., in: text)
        var parsed: [String: String] = [:]
        var repeated: Set<String> = []
        for match in expression.matches(in: text, range: range) {
            guard let keyRange = Range(match.range(at: 1), in: text),
                  let valueRange = Range(match.range(at: 2), in: text)
            else { continue }
            let key = String(text[keyRange])
            if parsed[key] != nil { repeated.insert(key) }
            parsed[key] = String(text[valueRange])
        }
        values = parsed
        duplicates = repeated
    }
}

private func placeholders(_ value: String) -> [String] {
    let expression = try! NSRegularExpression(
        pattern: #"%(?!%)(?:\d+\$)?[-+#0 '\d.*]*[A-Za-z@]"#
    )
    let range = NSRange(value.startIndex..., in: value)
    return expression.matches(in: value, range: range).compactMap {
        Range($0.range, in: value).map { String(value[$0]) }
    }
}
