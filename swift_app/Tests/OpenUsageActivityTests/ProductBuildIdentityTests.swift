import Foundation
import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("Product build identity UI")
struct ProductBuildIdentityTests {
    @Test("Derives all three lifecycle rows from version truth fields", arguments: [
        ("candidate", "not_published", false, "Candidate · Not published"),
        ("prerelease_ready", "not_published", true, "Pre-release ready · Not published"),
        ("prerelease_published", "published_prerelease", false, "Published pre-release"),
    ])
    func lifecycle(stage: String, publication: String, eligible: Bool, expected: String) {
        let copy = ProductBuildIdentityPresentation.make(identity: identity(
            stage: stage, publication: publication, eligible: eligible
        ))

        #expect(copy.versionAndBuild == "0.8.7 RC (build 29)")
        #expect(copy.lifecycle == expected)
        #expect(copy.published == "Published stable: v0.7.1")
        #expect(copy.accessibilityLabel.contains(expected))
    }

    @Test("Derives every canary clock", arguments: [
        ("not_started", "Canary: Not started · 0/5"),
        ("running", "Canary: Running · 0/5"),
        ("passed", "Canary: Passed · 0/5"),
        ("blocked", "Canary: Blocked · 0/5"),
        ("future_state", "Canary: Unknown · 0/5"),
    ])
    func canary(clock: String, expected: String) {
        let copy = ProductBuildIdentityPresentation.make(identity: identity(canaryClock: clock))
        #expect(copy.canary == expected)
        #expect(copy.accessibilityLabel.contains(expected))
    }

    @Test("Unknown lifecycle combinations fail closed in visible text")
    func unknownLifecycle() {
        let copy = ProductBuildIdentityPresentation.make(identity: identity(
            stage: "prerelease_ready",
            publication: "published_prerelease",
            eligible: false
        ))
        #expect(copy.lifecycle == "Unknown")
    }

    @Test("Activity sidebar hosts the identity without color-only semantics")
    func sourceContract() throws {
        let source = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("Sources/OpenUsageActivity/ActivityViews.swift"),
            encoding: .utf8
        )

        #expect(source.contains(".safeAreaInset(edge: .bottom"))
        #expect(source.contains("ProductBuildIdentityView()"))
        #expect(source.contains(".accessibilityLabel(copy.accessibilityLabel)"))
        for forbidden in [
            "publicationReceipt", "releaseId", "sourceSha",
            "manifestSha256", "assetSetSha256",
        ] {
            #expect(!source.contains(forbidden))
        }
    }

    @Test("Chinese localization covers every derived lifecycle and canary state")
    func chineseLocalization() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: root.appendingPathComponent(
                "Resources/zh-Hans.lproj/Localizable.strings"
            ),
            encoding: .utf8
        )
        for visible in [
            "候选版", "尚未发布", "预发布就绪", "已发布预览版", "未知",
            "尚未开始", "进行中", "已通过", "已阻塞",
        ] {
            #expect(source.contains("= \"\(visible)\";"))
        }
    }

    private func identity(
        stage: String = "candidate",
        publication: String = "not_published",
        eligible: Bool = false,
        canaryClock: String = "not_started"
    ) -> ProductVersionTruth {
        ProductVersionTruth(
            displayName: "UsageHub",
            legacyDisplayName: "OpenUsage Bar",
            candidateVersion: "0.8.7",
            candidateBuild: "29",
            channel: "rc",
            releaseStage: stage,
            publicationStatus: publication,
            releaseEligible: eligible,
            publishedBaselineVersion: "0.7.1",
            publishedBaselineTag: "v0.7.1",
            canaryQualifiedMachines: 0,
            canaryTargetMachines: 5,
            canaryClock: canaryClock
        )
    }
}
