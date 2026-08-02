import Foundation
import UsageCore

enum ProviderAddConnectionKind: String, Sendable, Hashable {
    case minimax
    case moonshot
    case stepPlan = "step_plan"
    case openAIOrganization = "openai_organization"
    case generic
    case dailyUsageFeed = "daily_usage_feed"

    init?(familyID: String) {
        switch familyID {
        case "minimax": self = .minimax
        case "moonshot": self = .moonshot
        case "step_plan": self = .stepPlan
        case "openai": self = .openAIOrganization
        default: return nil
        }
    }

    var isBuiltIn: Bool {
        ![Self.generic, .dailyUsageFeed].contains(self)
    }
}

struct ProviderAddOption: Identifiable, Sendable, Hashable {
    enum Origin: Sendable, Hashable {
        case catalog
        case customProvider
        case customUsageFeed
    }

    let id: String
    let descriptor: ProviderDisplayDescriptor
    let connectionKind: ProviderAddConnectionKind
    let origin: Origin
    let defaultProviderID: String

    var searchTerms: [String] {
        [id, descriptor.familyID, descriptor.displayName] + descriptor.aliases
    }
}

struct ProviderAddCatalog: Sendable, Equatable {
    let serviceOptions: [ProviderAddOption]
    let automaticDescriptors: [ProviderDisplayDescriptor]
    let customOptions: [ProviderAddOption]

    init(descriptors: [ProviderDisplayDescriptor]) {
        let sorted = descriptors.sorted(by: Self.sortDescriptors)
        serviceOptions = sorted.compactMap { descriptor in
            if let nativeKind = ProviderAddConnectionKind(familyID: descriptor.familyID) {
                return ProviderAddOption(
                    id: descriptor.familyID,
                    descriptor: descriptor,
                    connectionKind: nativeKind,
                    origin: .catalog,
                    defaultProviderID: descriptor.familyID
                )
            }
            guard descriptor.category == .api else { return nil }
            return ProviderAddOption(
                id: descriptor.familyID,
                descriptor: descriptor,
                connectionKind: .generic,
                origin: .catalog,
                defaultProviderID: descriptor.familyID
            )
        }
        let serviceIDs = Set(serviceOptions.map(\.descriptor.familyID))
        automaticDescriptors = sorted.filter { !serviceIDs.contains($0.familyID) }
        customOptions = Self.makeCustomOptions()
    }

    func filteredServiceOptions(query: String) -> [ProviderAddOption] {
        Self.filter(serviceOptions, query: query, terms: \.searchTerms)
    }

    func filteredCustomOptions(query: String) -> [ProviderAddOption] {
        Self.filter(customOptions, query: query, terms: \.searchTerms)
    }

    func filteredAutomaticDescriptors(query: String) -> [ProviderDisplayDescriptor] {
        Self.filter(automaticDescriptors, query: query) { descriptor in
            [descriptor.familyID, descriptor.displayName] + descriptor.aliases
        }
    }

    private static func makeCustomOptions() -> [ProviderAddOption] {
        let quota = ProviderCatalog.descriptor(
            for: "custom-provider", familyID: "custom",
            displayName: "Custom Provider", category: .api
        )
        let daily = ProviderCatalog.descriptor(
            for: "custom-daily-usage", familyID: "custom",
            displayName: "Custom Daily Usage Feed", category: .api
        )
        return [
            ProviderAddOption(
                id: "custom-provider", descriptor: quota,
                connectionKind: .generic, origin: .customProvider,
                defaultProviderID: "custom-provider"
            ),
            ProviderAddOption(
                id: "custom-daily-usage", descriptor: daily,
                connectionKind: .dailyUsageFeed, origin: .customUsageFeed,
                defaultProviderID: "custom-daily-usage"
            ),
        ]
    }

    private static func filter<Value>(
        _ values: [Value], query: String,
        terms: KeyPath<Value, [String]>
    ) -> [Value] {
        filter(values, query: query) { $0[keyPath: terms] }
    }

    private static func filter<Value>(
        _ values: [Value], query: String,
        terms: (Value) -> [String]
    ) -> [Value] {
        let normalized = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !normalized.isEmpty else { return values }
        return values.filter { value in
            terms(value).contains { $0.lowercased().contains(normalized) }
        }
    }

    private static func sortDescriptors(
        _ left: ProviderDisplayDescriptor, _ right: ProviderDisplayDescriptor
    ) -> Bool {
        let order = left.displayName.localizedStandardCompare(right.displayName)
        return order == .orderedSame
            ? left.familyID < right.familyID
            : order == .orderedAscending
    }
}

enum ProviderMutationAction: String, Encodable, Sendable, Equatable {
    case createConnection = "create_connection"
    case updateConnection = "update_connection"
    case removeConnection = "remove_connection"
}

enum ProviderDraftValidation: Sendable, Equatable {
    case missingProviderID
    case missingName
    case missingCredential
    case invalidSite
}

struct GenericQuotaDraft: Sendable, Equatable {
    let providerID: String
    let name: String
    let familyID: String
    let endpoint: String
    let headerName: String
    let authPrefix: String
    let primaryPath: String
    let remainingPercentPath: String?
    let resetPath: String?
    let detailPath: String?
    let replacementCredential: String
}

struct DailyUsageFeedDraft: Sendable, Equatable {
    let providerID: String
    let name: String
    let familyID: String
    let endpoint: String
    let headerName: String
    let authPrefix: String
    let itemsPath: String
    let datePath: String
    let modelPath: String
    let inputTokensPath: String
    let outputTokensPath: String
    let cacheReadTokensPath: String?
    let cacheCreationTokensPath: String?
    let reasoningTokensPath: String?
    let totalTokensPath: String
    let sinceParameter: String
    let untilParameter: String
    let replacementCredential: String
}

enum ManagedConnectionDraft: Sendable, Equatable {
    case minimax(
        providerID: String, name: String, site: String,
        replacementCredential: String
    )
    case moonshot(
        providerID: String, name: String, site: String,
        replacementCredential: String
    )
    case stepPlan(
        providerID: String, name: String, site: String,
        replacementCredential: String, replacementSession: String
    )
    case openAIOrganization(
        providerID: String, name: String, replacementCredential: String
    )
    case generic(GenericQuotaDraft)
    case dailyUsageFeed(DailyUsageFeedDraft)

    var providerID: String {
        switch self {
        case let .minimax(providerID, _, _, _),
             let .moonshot(providerID, _, _, _),
             let .stepPlan(providerID, _, _, _, _),
             let .openAIOrganization(providerID, _, _): providerID
        case let .generic(draft): draft.providerID
        case let .dailyUsageFeed(draft): draft.providerID
        }
    }

    private var name: String {
        switch self {
        case let .minimax(_, name, _, _),
             let .moonshot(_, name, _, _),
             let .stepPlan(_, name, _, _, _),
             let .openAIOrganization(_, name, _): name
        case let .generic(draft): draft.name
        case let .dailyUsageFeed(draft): draft.name
        }
    }

    private var primaryCredential: String {
        switch self {
        case let .minimax(_, _, _, credential),
             let .moonshot(_, _, _, credential),
             let .openAIOrganization(_, _, credential): credential
        case let .stepPlan(_, _, _, credential, _): credential
        case let .generic(draft): draft.replacementCredential
        case let .dailyUsageFeed(draft): draft.replacementCredential
        }
    }

    func validation(action: ProviderMutationAction) -> ProviderDraftValidation? {
        if providerID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .missingProviderID
        }
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .missingName
        }
        let site: String? = switch self {
        case let .minimax(_, _, site, _): site
        case let .moonshot(_, _, site, _): site
        case let .stepPlan(_, _, site, _, _): site
        default: nil
        }
        if let site, !["china", "international"].contains(site) {
            return .invalidSite
        }
        if action == .createConnection {
            let hasSession: Bool = if case let .stepPlan(_, _, _, _, session) = self {
                !session.isEmpty
            } else { false }
            if primaryCredential.isEmpty && !hasSession { return .missingCredential }
        }
        return nil
    }

    func request(action: ProviderMutationAction) -> ProviderMutationRequestV2 {
        ProviderMutationRequestV2(action: action, draft: self)
    }
}

struct ProviderConnectionPublicConfiguration: Sendable, Hashable {
    let endpoint: String?
    let headerName: String?
    let authPrefix: String?
    let primaryPath: String?
    let remainingPercentPath: String?
    let resetPath: String?
    let detailPath: String?
    let itemsPath: String?
    let datePath: String?
    let modelPath: String?
    let inputTokensPath: String?
    let outputTokensPath: String?
    let cacheReadTokensPath: String?
    let cacheCreationTokensPath: String?
    let reasoningTokensPath: String?
    let totalTokensPath: String?
    let sinceParameter: String?
    let untilParameter: String?
}

extension ProviderConnectionSummary {
    func managedDraft(
        name replacementName: String? = nil,
        replacementCredential: String, replacementSession: String = ""
    ) -> ManagedConnectionDraft? {
        let connectionName = replacementName ?? displayName
        switch kind {
        case "minimax": return .minimax(
            providerID: providerID, name: connectionName,
            site: site ?? "china",
            replacementCredential: replacementCredential
        )
        case "moonshot": return .moonshot(
            providerID: providerID, name: connectionName, site: site ?? "",
            replacementCredential: replacementCredential
        )
        case "step_plan": return .stepPlan(
            providerID: providerID, name: connectionName, site: site ?? "",
            replacementCredential: replacementCredential,
            replacementSession: replacementSession
        )
        case "openai_organization": return .openAIOrganization(
            providerID: providerID, name: connectionName,
            replacementCredential: replacementCredential
        )
        case "generic":
            guard let endpoint = configuration.endpoint,
                  let headerName = configuration.headerName,
                  let authPrefix = configuration.authPrefix,
                  let primaryPath = configuration.primaryPath
            else { return nil }
            return .generic(.init(
                providerID: providerID, name: connectionName, familyID: familyID,
                endpoint: endpoint, headerName: headerName, authPrefix: authPrefix,
                primaryPath: primaryPath,
                remainingPercentPath: configuration.remainingPercentPath,
                resetPath: configuration.resetPath, detailPath: configuration.detailPath,
                replacementCredential: replacementCredential
            ))
        case "daily_usage_feed":
            guard let endpoint = configuration.endpoint,
                  let headerName = configuration.headerName,
                  let authPrefix = configuration.authPrefix,
                  let itemsPath = configuration.itemsPath,
                  let datePath = configuration.datePath,
                  let modelPath = configuration.modelPath,
                  let inputTokensPath = configuration.inputTokensPath,
                  let outputTokensPath = configuration.outputTokensPath,
                  let totalTokensPath = configuration.totalTokensPath,
                  let sinceParameter = configuration.sinceParameter,
                  let untilParameter = configuration.untilParameter
            else { return nil }
            return .dailyUsageFeed(.init(
                providerID: providerID, name: connectionName, familyID: familyID,
                endpoint: endpoint, headerName: headerName, authPrefix: authPrefix,
                itemsPath: itemsPath, datePath: datePath, modelPath: modelPath,
                inputTokensPath: inputTokensPath, outputTokensPath: outputTokensPath,
                cacheReadTokensPath: configuration.cacheReadTokensPath,
                cacheCreationTokensPath: configuration.cacheCreationTokensPath,
                reasoningTokensPath: configuration.reasoningTokensPath,
                totalTokensPath: totalTokensPath, sinceParameter: sinceParameter,
                untilParameter: untilParameter,
                replacementCredential: replacementCredential
            ))
        default: return nil
        }
    }
}

struct ProviderMutationRequestV2: Encodable, Sendable, Equatable {
    let version = 2
    let action: ProviderMutationAction
    let providerID: String
    let kind: String
    let configuration: Configuration
    let credentialMaterial: CredentialMaterial

    init(action: ProviderMutationAction, draft: ManagedConnectionDraft) {
        self.action = action
        providerID = draft.providerID
        if action == .removeConnection {
            switch draft {
            case .minimax: kind = "minimax"
            case .moonshot: kind = "moonshot"
            case .stepPlan: kind = "step_plan"
            case .openAIOrganization: kind = "openai_organization"
            case .generic: kind = "generic"
            case .dailyUsageFeed: kind = "daily_usage_feed"
            }
            configuration = .empty
            credentialMaterial = .empty
            return
        }
        switch draft {
        case let .minimax(_, name, site, credential):
            kind = "minimax"
            configuration = .regional(name: name, site: site)
            credentialMaterial = .values(primary: credential, session: "")
        case let .moonshot(_, name, site, credential):
            kind = "moonshot"
            configuration = .moonshot(name: name, site: site)
            credentialMaterial = .values(primary: credential, session: "")
        case let .stepPlan(_, name, site, credential, session):
            kind = "step_plan"
            configuration = .regional(name: name, site: site)
            credentialMaterial = .values(primary: credential, session: session)
        case let .openAIOrganization(_, name, credential):
            kind = "openai_organization"
            configuration = .named(name)
            credentialMaterial = .values(primary: credential, session: "")
        case let .generic(draft):
            kind = "generic"
            configuration = .generic(draft)
            credentialMaterial = .values(
                primary: draft.replacementCredential, session: ""
            )
        case let .dailyUsageFeed(draft):
            kind = "daily_usage_feed"
            configuration = .dailyUsageFeed(draft)
            credentialMaterial = .values(
                primary: draft.replacementCredential, session: ""
            )
        }
    }

    enum CodingKeys: String, CodingKey {
        case version, action, kind, configuration, credentialMaterial
        case providerID = "providerId"
    }

    enum Configuration: Encodable, Sendable, Equatable {
        case empty
        case named(String)
        case regional(name: String, site: String)
        case moonshot(name: String, site: String)
        case generic(GenericQuotaDraft)
        case dailyUsageFeed(DailyUsageFeedDraft)

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: DynamicCodingKey.self)
            switch self {
            case .empty: break
            case let .named(name): try container.encode(name, forKey: "name")
            case let .regional(name, site),
                 let .moonshot(name, site):
                try container.encode(name, forKey: "name")
                try container.encode(site, forKey: "site")
            case let .generic(draft):
                try container.encode(draft.name, forKey: "name")
                try container.encode(draft.endpoint, forKey: "endpoint")
                try container.encode(draft.headerName, forKey: "headerName")
                try container.encode(draft.authPrefix, forKey: "authPrefix")
                try container.encode(draft.primaryPath, forKey: "primaryPath")
                try container.encode(draft.remainingPercentPath, forKey: "remainingPercentPath")
                try container.encode(draft.resetPath, forKey: "resetPath")
                try container.encode(draft.detailPath, forKey: "detailPath")
            case let .dailyUsageFeed(draft):
                try container.encode(draft.name, forKey: "name")
                try container.encode(draft.familyID, forKey: "familyId")
                try container.encode(draft.endpoint, forKey: "endpoint")
                try container.encode(draft.headerName, forKey: "headerName")
                try container.encode(draft.authPrefix, forKey: "authPrefix")
                try container.encode(draft.itemsPath, forKey: "itemsPath")
                try container.encode(draft.datePath, forKey: "datePath")
                try container.encode(draft.modelPath, forKey: "modelPath")
                try container.encode(draft.inputTokensPath, forKey: "inputTokensPath")
                try container.encode(draft.outputTokensPath, forKey: "outputTokensPath")
                try container.encode(draft.cacheReadTokensPath, forKey: "cacheReadTokensPath")
                try container.encode(draft.cacheCreationTokensPath, forKey: "cacheCreationTokensPath")
                try container.encode(draft.reasoningTokensPath, forKey: "reasoningTokensPath")
                try container.encode(draft.totalTokensPath, forKey: "totalTokensPath")
                try container.encode(draft.sinceParameter, forKey: "sinceParameter")
                try container.encode(draft.untilParameter, forKey: "untilParameter")
            }
        }
    }

    enum CredentialMaterial: Encodable, Sendable, Equatable {
        case empty
        case values(primary: String, session: String)

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: DynamicCodingKey.self)
            if case let .values(primary, session) = self {
                try container.encode(primary, forKey: "primary")
                try container.encode(session, forKey: "session")
            }
        }
    }
}

private struct DynamicCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
    init(_ value: String) { stringValue = value }
}

private extension KeyedEncodingContainer where Key == DynamicCodingKey {
    mutating func encode<T: Encodable>(_ value: T, forKey key: String) throws {
        try encode(value, forKey: DynamicCodingKey(key))
    }
}

extension ProviderCenterPresentation {
    static func canMutate(kind: String) -> Bool {
        [
            "minimax", "moonshot", "step_plan", "openai_organization",
            "generic", "daily_usage_feed",
        ]
            .contains(kind)
    }
}
