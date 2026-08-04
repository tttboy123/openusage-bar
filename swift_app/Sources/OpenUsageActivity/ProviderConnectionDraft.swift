import Foundation

/// Non-secret Provider connection form state that survives sheet dismissal and
/// app restarts. Credentials are deliberately absent: they live only in memory
/// while editing and in Keychain after a successful mutation.
struct ProviderConnectionDraft: Codable, Equatable, Sendable {
    var kind: String
    var providerID: String
    var name: String
    var site: String
    var endpoint: String
    var familyID: String
    var headerName: String
    var authPrefix: String
    var primaryPath: String
    var remainingPercentPath: String
    var resetPath: String
    var detailPath: String
    var itemsPath: String
    var datePath: String
    var modelPath: String
    var inputTokensPath: String
    var outputTokensPath: String
    var totalTokensPath: String
    var sinceParameter: String
    var untilParameter: String

    init(
        kind: String = "generic",
        providerID: String = "",
        name: String = "",
        site: String = "china",
        endpoint: String = "",
        familyID: String = "",
        headerName: String = "Authorization",
        authPrefix: String = "Bearer",
        primaryPath: String = "data.remaining",
        remainingPercentPath: String = "",
        resetPath: String = "",
        detailPath: String = "",
        itemsPath: String = "data.items",
        datePath: String = "date",
        modelPath: String = "model",
        inputTokensPath: String = "input_tokens",
        outputTokensPath: String = "output_tokens",
        totalTokensPath: String = "total_tokens",
        sinceParameter: String = "since",
        untilParameter: String = "until"
    ) {
        self.kind = kind
        self.providerID = providerID
        self.name = name
        self.site = site
        self.endpoint = endpoint
        self.familyID = familyID
        self.headerName = headerName
        self.authPrefix = authPrefix
        self.primaryPath = primaryPath
        self.remainingPercentPath = remainingPercentPath
        self.resetPath = resetPath
        self.detailPath = detailPath
        self.itemsPath = itemsPath
        self.datePath = datePath
        self.modelPath = modelPath
        self.inputTokensPath = inputTokensPath
        self.outputTokensPath = outputTokensPath
        self.totalTokensPath = totalTokensPath
        self.sinceParameter = sinceParameter
        self.untilParameter = untilParameter
    }

    func hasChanges(from baseline: ProviderConnectionDraft) -> Bool {
        self != baseline
    }
}

/// UserDefaults-backed draft persistence for the Provider connection form.
/// Drafts are scoped to a concrete connection so adding another account or
/// re-opening the same connection restores exactly what was being typed.
struct ProviderConnectionDraftStore {
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    private func key(familyID: String, providerID: String) -> String {
        let family = familyID.isEmpty ? "unknown" : familyID
        let provider = providerID.isEmpty ? family : providerID
        return "OpenUsageActivity.connectionDraft.\(family).\(provider)"
    }

    func load(familyID: String, providerID: String) -> ProviderConnectionDraft? {
        guard let data = defaults.data(forKey: key(familyID: familyID, providerID: providerID))
        else { return nil }
        return try? JSONDecoder().decode(ProviderConnectionDraft.self, from: data)
    }

    func save(_ draft: ProviderConnectionDraft, familyID: String, providerID: String) {
        guard let data = try? JSONEncoder().encode(draft) else { return }
        defaults.set(data, forKey: key(familyID: familyID, providerID: providerID))
    }

    func clear(familyID: String, providerID: String) {
        defaults.removeObject(forKey: key(familyID: familyID, providerID: providerID))
    }
}
