import Foundation

public enum AppLocalization {
    /// Localizes human-facing app copy while keeping API and CLI payloads language-neutral.
    public static func text(_ key: String, bundle: Bundle = .main) -> String {
        NSLocalizedString(key, tableName: nil, bundle: bundle, value: key, comment: "")
    }
}
