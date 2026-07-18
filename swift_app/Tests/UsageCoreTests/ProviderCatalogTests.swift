import Foundation
import Testing
@testable import UsageCore

@Suite("Generated provider catalog")
struct ProviderCatalogTests {
    private struct Manifest: Decodable {
        struct Upstream: Decodable {
            let version: String
            let revision: String
            let familyIDs: [String]

            enum CodingKeys: String, CodingKey {
                case version, revision
                case familyIDs = "family_ids"
            }
        }

        struct Family: Decodable {
            struct Capabilities: Decodable {
                struct QuotaWindows: Decodable {
                    let state: String
                    let values: [String]
                }

                let quotaWindows: QuotaWindows
                let tokenHistory: String
                let modelBreakdown: String
                let resetTimestamps: String
                let billing: String
                let credits: String
                let balance: String
                let cost: String
                let rateLimits: String
                let serviceStatus: String

                enum CodingKeys: String, CodingKey {
                    case billing, credits, balance, cost
                    case quotaWindows = "quota_windows"
                    case tokenHistory = "token_history"
                    case modelBreakdown = "model_breakdown"
                    case resetTimestamps = "reset_timestamps"
                    case rateLimits = "rate_limits"
                    case serviceStatus = "service_status"
                }
            }

            struct Source: Decodable {
                let sourceID: String
                let kind: String
                let credentialType: String
                let operatingSystems: [String]
                let stability: String
                let provenance: String

                enum CodingKeys: String, CodingKey {
                    case kind, stability, provenance
                    case sourceID = "source_id"
                    case credentialType = "credential_type"
                    case operatingSystems = "operating_systems"
                }
            }

            let id: String
            let displayName: String
            let aliases: [String]?
            let category: String
            let metricFamilies: [String]
            let regions: [String]
            let supportsAccounts: Bool
            let capabilities: Capabilities
            let sources: [Source]

            enum CodingKeys: String, CodingKey {
                case id, category, capabilities, sources
                case aliases
                case displayName = "display_name"
                case metricFamilies = "metric_families"
                case regions
                case supportsAccounts = "supports_accounts"
            }
        }

        let upstream: Upstream
        let families: [Family]
    }

    @Test("Generated Swift descriptors exactly match the canonical JSON manifest")
    func manifestParity() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent(
            "openusage_bar/resources/provider-catalog.v1.json"
        ))
        let manifest = try JSONDecoder().decode(Manifest.self, from: data)

        #expect(manifest.upstream.version == GeneratedProviderCatalog.upstreamVersion)
        #expect(manifest.upstream.revision == GeneratedProviderCatalog.upstreamRevision)
        #expect(GeneratedProviderCatalog.upstreamFamilyIDs == Set(manifest.upstream.familyIDs))
        #expect(GeneratedProviderCatalog.upstreamFamilyIDs.count == 35)
        #expect(manifest.families.count == 37)
        #expect(Set(GeneratedProviderCatalog.families.keys) == Set(manifest.families.map(\.id)))

        for family in manifest.families {
            let descriptor = try #require(GeneratedProviderCatalog.families[family.id])
            let expectedCredentials = Set(family.sources.map {
                $0.credentialType == "provider_owned" ? "none" : $0.credentialType
            })
            let expectedIdentitySources = Set(family.sources.map {
                ProviderIdentitySource(credentialSource: $0.sourceID, sourceKind: $0.kind)
            })
            let expectedSources = try family.sources.map { source in
                ProviderSourceCapability(
                    sourceID: source.sourceID,
                    sourceKind: source.kind,
                    operatingSystems: Set(try source.operatingSystems.map {
                        try #require(ProviderSourceOperatingSystem(rawValue: $0))
                    }),
                    stability: try #require(ProviderSourceStability(rawValue: source.stability)),
                    provenance: try #require(ProviderSourceProvenance(rawValue: source.provenance))
                )
            }
            let capabilities = family.capabilities
            let quotaState = try #require(ProviderCapabilityState(
                rawValue: capabilities.quotaWindows.state
            ))
            let quotaValues = Set(try capabilities.quotaWindows.values.map {
                try #require(ProviderQuotaWindow(rawValue: $0))
            })
            let quotaWindows = try #require(ProviderQuotaWindowCapability(
                state: quotaState,
                values: quotaValues
            ))
            let expectedProfile = ProviderCapabilityProfile(
                quotaWindows: quotaWindows,
                tokenHistory: try #require(ProviderCapabilityState(rawValue: capabilities.tokenHistory)),
                modelBreakdown: try #require(ProviderCapabilityState(rawValue: capabilities.modelBreakdown)),
                resetTimestamps: try #require(ProviderCapabilityState(rawValue: capabilities.resetTimestamps)),
                billing: try #require(ProviderCapabilityState(rawValue: capabilities.billing)),
                credits: try #require(ProviderCapabilityState(rawValue: capabilities.credits)),
                balance: try #require(ProviderCapabilityState(rawValue: capabilities.balance)),
                cost: try #require(ProviderCapabilityState(rawValue: capabilities.cost)),
                rateLimits: try #require(ProviderCapabilityState(rawValue: capabilities.rateLimits)),
                serviceStatus: try #require(ProviderCapabilityState(rawValue: capabilities.serviceStatus))
            )
            #expect(descriptor.providerID == family.id)
            #expect(descriptor.familyID == family.id)
            #expect(descriptor.displayName == family.displayName)
            #expect(descriptor.aliases == Set(family.aliases ?? []))
            #expect(descriptor.category.manifestName == family.category)
            #expect(descriptor.metricFamilies.map(\.manifestName).sorted() == family.metricFamilies)
            #expect(descriptor.regions == Set(family.regions))
            #expect(descriptor.supportsAccounts == family.supportsAccounts)
            #expect(Set(descriptor.credentialSourceTypes.map(\.manifestName)) == expectedCredentials)
            #expect(descriptor.acceptedIdentitySources == expectedIdentitySources)
            #expect(descriptor.capabilityProfile == expectedProfile)
            #expect(descriptor.sourceCapabilities == expectedSources)
            #expect(ProviderCatalog.descriptor(for: family.id) == descriptor)
        }
    }

    @Test("Discovery aliases find families without changing stable identity")
    func discoveryAliases() {
        #expect(ProviderCatalog.search("glm").map(\.familyID) == ["zai"])
        #expect(ProviderCatalog.search("智谱").map(\.familyID) == ["zai"])
        #expect(ProviderCatalog.search("kimi").map(\.familyID) == ["kimi_cli", "moonshot"])
        #expect(ProviderCatalog.search("claude").map(\.familyID) == ["anthropic", "claude_code"])
        #expect(ProviderCatalog.search("qwen").map(\.familyID) == ["alibaba_cloud", "qwen_cli"])
        #expect(ProviderCatalog.search("opencode").map(\.familyID) == ["opencode"])
        #expect(ProviderCatalog.descriptor(for: "glm").familyID == "glm")
    }

    @Test("Capability and source enum boundaries are exact")
    func capabilityEnumBoundaries() {
        #expect(Set(ProviderCapabilityState.allCases.map(\.rawValue)) == [
            "supported", "unsupported", "unknown",
        ])
        #expect(Set(ProviderQuotaWindow.allCases.map(\.rawValue)) == [
            "session", "five_hour", "weekly", "monthly", "billing_cycle", "model_specific",
        ])
        #expect(Set(ProviderSourceOperatingSystem.allCases.map(\.rawValue)) == [
            "macos", "windows", "linux",
        ])
        #expect(Set(ProviderSourceStability.allCases.map(\.rawValue)) == [
            "stable", "experimental", "pinned", "opaque",
        ])
        #expect(Set(ProviderSourceProvenance.allCases.map(\.rawValue)) == [
            "openusage_upstream", "openusage_bar_builtin", "provider_official",
            "provider_local", "user_session",
        ])
    }

    @Test("Known families expose conservative capability and source facts")
    func knownCapabilityFacts() throws {
        let codex = try #require(GeneratedProviderCatalog.families["codex"])
        #expect(codex.capabilityProfile.quotaWindows == .supported(.fiveHour, .weekly))
        #expect(codex.capabilityProfile.tokenHistory == .supported)
        #expect(codex.capabilityProfile.modelBreakdown == .supported)
        #expect(codex.capabilityProfile.resetTimestamps == .supported)
        #expect(codex.capabilityProfile.billing == .unknown)

        let kiro = try #require(GeneratedProviderCatalog.families["kiro_cli"])
        #expect(kiro.capabilityProfile.quotaWindows == .supported(.billingCycle))
        #expect(kiro.capabilityProfile.credits == .supported)

        let stepPlan = try #require(GeneratedProviderCatalog.families["step_plan"])
        #expect(stepPlan.regions == ["cn", "international"])
        #expect(stepPlan.supportsAccounts)
        #expect(stepPlan.capabilityProfile.billing == .supported)
        #expect(stepPlan.sourceCapabilities.first == ProviderSourceCapability(
            sourceID: "step_plan_browser_session",
            sourceKind: "browser_session",
            operatingSystems: [.macOS],
            stability: .experimental,
            provenance: .userSession
        ))

        let openAI = try #require(GeneratedProviderCatalog.families["openai"])
        #expect(openAI.capabilityProfile.tokenHistory == .supported)
        #expect(openAI.capabilityProfile.modelBreakdown == .supported)
        #expect(openAI.capabilityProfile.billing == .supported)
        #expect(openAI.capabilityProfile.cost == .supported)
        #expect(openAI.sourceCapabilities.first == ProviderSourceCapability(
            sourceID: "openai_admin_api",
            sourceKind: "official_api",
            operatingSystems: [.macOS],
            stability: .stable,
            provenance: .providerOfficial
        ))
    }

    @Test("Canonical category sets are exact")
    func categorySets() {
        let categories = Dictionary(grouping: GeneratedProviderCatalog.families.values) { $0.category }
            .mapValues { Set($0.map(\.providerID)) }
        #expect(categories[.subscription] == [
            "claude_code", "codex", "copilot", "cursor", "gemini_cli", "kiro_cli", "minimax",
            "opencode", "step_plan",
        ])
        #expect(categories[.localTool] == [
            "amp", "codebuff", "crush", "droid", "goose", "hermes", "kilo_code", "kimi_cli",
            "mux", "ollama", "openclaw", "pi", "qwen_cli", "roocode", "zed",
        ])
        #expect(categories[.api]?.count == 13)
    }

    @Test("Catalog exposes every family in stable display order")
    func allDescriptors() {
        let descriptors = ProviderCatalog.allDescriptors
        let familyIDs = Set(descriptors.map(\.familyID))
        let displayNames = descriptors.map(\.displayName)
        let sortedDisplayNames = displayNames.sorted {
            $0.localizedStandardCompare($1) == .orderedAscending
        }
        #expect(descriptors.count == 37)
        #expect(familyIDs == Set(GeneratedProviderCatalog.families.keys))
        #expect(displayNames == sortedDisplayNames)
    }

    @Test("Unknown IDs never inherit a known family by substring or prefix")
    func adversarialUnknownIDs() {
        #expect(ProviderCatalog.descriptor(for: "minimaxevil").displayName == "Minimaxevil")
        #expect(ProviderCatalog.descriptor(for: "cursorless").displayName == "Cursorless")
        #expect(ProviderCatalog.descriptor(for: "not-codex").displayName == "Not Codex")
        #expect(ProviderCatalog.descriptor(for: "step-planmain").category == .api)
        #expect(ProviderCatalog.descriptor(for: "not-codex").familyID == "not-codex")
    }

    @Test("Unknown family fallback is conservative and matches the Python registry")
    func unknownCapabilityFallback() {
        let descriptor = ProviderCatalog.descriptor(for: "future_provider")
        #expect(descriptor.capabilityProfile == .unknown)
        #expect(descriptor.regions.isEmpty)
        #expect(!descriptor.supportsAccounts)
        #expect(descriptor.sourceCapabilities == [ProviderSourceCapability(
            sourceID: "openusage",
            sourceKind: "openusage",
            operatingSystems: [.macOS],
            stability: .pinned,
            provenance: .openUsageUpstream
        )])
    }

    @Test("Instance metadata overrides presentation while family metadata supplies capabilities")
    func instanceDescriptor() {
        let minimax = ProviderCatalog.descriptor(
            for: "minimax-main", familyID: "minimax",
            displayName: "MiniMax Main", category: .subscription
        )
        #expect(minimax.providerID == "minimax-main")
        #expect(minimax.familyID == "minimax")
        #expect(minimax.displayName == "MiniMax Main")
        #expect(minimax.category == .subscription)
        #expect(minimax.metricFamilies == GeneratedProviderCatalog.families["minimax"]?.metricFamilies)
        #expect(minimax.capabilityProfile == GeneratedProviderCatalog.families["minimax"]?.capabilityProfile)
        #expect(minimax.sourceCapabilities == GeneratedProviderCatalog.families["minimax"]?.sourceCapabilities)
        #expect(minimax.regions == ["cn", "international"])
        #expect(minimax.supportsAccounts)

        let redundantRawLabel = ProviderCatalog.descriptor(
            for: "minimax-generated", familyID: "minimax",
            displayName: "minimax", category: .subscription
        )
        #expect(redundantRawLabel.displayName == "MiniMax")

        let unknownRawLabel = ProviderCatalog.descriptor(
            for: "future_agent", familyID: "future_agent",
            displayName: "future_agent", category: .api
        )
        #expect(unknownRawLabel.displayName == "future_agent")

        let unicodeCustomLabel = ProviderCatalog.descriptor(
            for: "mistral-custom", familyID: "mistral",
            displayName: "miſtral", category: .api
        )
        #expect(unicodeCustomLabel.displayName == "miſtral")

        let collision = ProviderCatalog.descriptor(
            for: "minimax-foo", familyID: "minimax-foo",
            displayName: "MiniMax Foo API", category: .api
        )
        #expect(collision.familyID == "minimax-foo")
        #expect(collision.category == .api)
        #expect(collision.metricFamilies == [.billing, .tokenActivity])
        #expect(collision.capabilityProfile == .unknown)
        #expect(collision.sourceCapabilities == [ProviderSourceCapability(
            sourceID: "openusage",
            sourceKind: "openusage",
            operatingSystems: [.macOS],
            stability: .pinned,
            provenance: .openUsageUpstream
        )])
    }

    @Test("Capability domain values are Sendable and Hashable")
    func nativeValueSemantics() {
        assertSendableHashable(ProviderCapabilityState.self)
        assertSendableHashable(ProviderQuotaWindow.self)
        assertSendableHashable(ProviderQuotaWindowCapability.self)
        assertSendableHashable(ProviderCapabilityProfile.self)
        assertSendableHashable(ProviderSourceOperatingSystem.self)
        assertSendableHashable(ProviderSourceStability.self)
        assertSendableHashable(ProviderSourceProvenance.self)
        assertSendableHashable(ProviderSourceCapability.self)
        assertSendableHashable(ProviderDisplayDescriptor.self)
    }

    private func assertSendableHashable<T: Sendable & Hashable>(_: T.Type) {}
}

private extension ProviderProductCategory {
    var manifestName: String {
        switch self {
        case .subscription: "subscription"
        case .api: "api"
        case .localTool: "local_tool"
        }
    }
}

private extension ProviderMetricFamily {
    var manifestName: String {
        switch self {
        case .subscriptionQuota: "subscription_quota"
        case .tokenActivity: "token_activity"
        case .billing: "billing"
        case .operational: "operational"
        }
    }
}

private extension CredentialSourceType {
    var manifestName: String {
        switch self {
        case .none: "none"
        case .keychain: "keychain"
        case .browserSession: "browser_session"
        case .apiKey: "api_key"
        case .oauth: "oauth"
        case .cli: "cli"
        case .local: "local"
        }
    }
}
