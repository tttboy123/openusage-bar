import AppKit
import Charts
import SwiftUI
import UsageCore

struct ProductBuildIdentityPresentation: Equatable, Sendable {
    let versionAndBuild: String
    let lifecycle: String
    let published: String
    let canary: String

    var accessibilityLabel: String {
        [versionAndBuild, lifecycle, published, canary].joined(separator: ". ")
    }

    static func make(identity: ProductVersionTruth) -> Self {
        let lifecycle: String
        switch (identity.releaseStage, identity.publicationStatus, identity.releaseEligible) {
        case ("candidate", "not_published", false):
            lifecycle = "\(AppLocalization.text("Candidate")) · \(AppLocalization.text("Not published"))"
        case ("prerelease_ready", "not_published", true):
            lifecycle = "\(AppLocalization.text("Pre-release ready")) · \(AppLocalization.text("Not published"))"
        case ("prerelease_published", "published_prerelease", false):
            lifecycle = AppLocalization.text("Published pre-release")
        default:
            lifecycle = AppLocalization.text("Unknown")
        }

        let canaryStatus: String
        switch identity.canaryClock {
        case "not_started": canaryStatus = AppLocalization.text("Not started")
        case "running": canaryStatus = AppLocalization.text("Running")
        case "passed": canaryStatus = AppLocalization.text("Passed")
        case "blocked": canaryStatus = AppLocalization.text("Blocked")
        default: canaryStatus = AppLocalization.text("Unknown")
        }

        return Self(
            versionAndBuild: AppLocalization.format(
                "%@ (build %@)", identity.formattedCandidateVersion, identity.candidateBuild
            ),
            lifecycle: lifecycle,
            published: AppLocalization.format(
                "Published stable: %@", identity.publishedBaselineTag
            ),
            canary: AppLocalization.format(
                "Canary: %@ · %lld/%lld", canaryStatus,
                Int64(identity.canaryQualifiedMachines), Int64(identity.canaryTargetMachines)
            )
        )
    }
}

struct ProductBuildIdentityView: View {
    private let copy: ProductBuildIdentityPresentation

    init(identity: ProductVersionTruth = .current) {
        copy = ProductBuildIdentityPresentation.make(identity: identity)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(copy.versionAndBuild).font(.caption.weight(.semibold))
            Text(copy.lifecycle)
            Text(copy.published)
            Text(copy.canary)
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(copy.accessibilityLabel)
    }
}

struct ActivityRootView: View {
    @Bindable var store: ActivityViewStore
    @Bindable var coordinator: ActivityRouteCoordinator
    let windowRegistry: ActivityWindowRegistry
    @Environment(\.openWindow) private var openWindow
    @AppStorage("OpenUsageActivity.onboardingSkipped") private var onboardingSkipped = false
    @State private var showOnboardingManually = false
    @State private var dismissedOnboardingForSession = false

    init(store: ActivityViewStore, initialRoute: UsageDetailsRoute) {
        self.store = store
        windowRegistry = ActivityWindowRegistry()
        coordinator = ActivityRouteCoordinator(
            initialRoute: initialRoute, activate: {}, openWindow: {}
        )
    }

    init(
        store: ActivityViewStore, coordinator: ActivityRouteCoordinator,
        windowRegistry: ActivityWindowRegistry
    ) {
        self.store = store
        self.coordinator = coordinator
        self.windowRegistry = windowRegistry
    }

    var body: some View {
        NavigationSplitView {
            List(UsageDetailsRoute.allCases, selection: routeBinding) { route in
                Label(route.title, systemImage: route.symbol).tag(route)
            }
            .scrollContentBackground(.hidden)
            .navigationTitle(AppLocalization.text("UsageHub"))
            .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 250)
            .safeAreaInset(edge: .bottom, spacing: 0) {
                ProductBuildIdentityView()
                    .padding(.horizontal, 10)
                    .padding(.bottom, 8)
            }
        } detail: {
            content
                .navigationTitle(coordinator.route.title)
                .toolbar {
                    ToolbarItem {
                        if coordinator.route != .automation {
                            Button("Refresh", systemImage: "arrow.clockwise") { store.reload() }
                                .disabled(store.isLoading)
                        }
                    }
                }
        }
        .tint(DesignTokens.accent)
        .background(DesignTokens.bg)
        .animation(.easeOut(duration: DesignTokens.motionBase), value: coordinator.route)
        .overlay {
            if coordinator.route != .automation, onboardingPhase != .hidden {
                OnboardingView(
                    phase: onboardingPhase,
                    primaryAction: performOnboardingAction,
                    skip: {
                        onboardingSkipped = true
                        showOnboardingManually = false
                        dismissedOnboardingForSession = true
                    }
                )
            }
        }
        .onAppear {
            coordinator.installWindowOpener { openWindow(id: "usage-details") }
            if ActivityRouteLoadingPolicy.loadsLedgerOnAppear(coordinator.route) { store.reload() }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            // Do not interrupt an in-progress Provider configuration when the
            // window regains focus (e.g. returning from another Space or app).
            if store.data != nil, coordinator.route != .providersAndAccounts {
                store.revalidateSelection()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: OnboardingRouteMessage.notification)) { _ in
            showOnboardingManually = true
        }
        .onDisappear { store.cancel() }
        .background(ActivityWindowRegistrationView(registry: windowRegistry).frame(width: 0, height: 0))
    }

    private var onboardingPhase: FirstRunPhase {
        if showOnboardingManually {
            return store.displayData?.hasTrustworthyFact == true ? .ready : assessment(userSkipped: false)
        }
        if dismissedOnboardingForSession { return .hidden }
        return assessment(userSkipped: onboardingSkipped)
    }

    private func assessment(userSkipped: Bool) -> FirstRunPhase {
        if let data = store.displayData {
            return FirstRunAssessment.evaluate(
                wasExplicitlyOpened: true,
                userSkipped: userSkipped,
                providerFamilyIDs: data.detectedLocalFamilyIDs,
                configuredFamilyIDs: data.configuredFamilyIDs,
                hasTrustworthyFact: data.hasTrustworthyFact,
                isRefreshing: store.isLoading
            )
        }
        guard !store.isLoading, store.error == .databaseUnavailable else { return .hidden }
        return FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: userSkipped,
            providerFamilyIDs: [], configuredFamilyIDs: [],
            hasTrustworthyFact: false, isRefreshing: false
        )
    }

    private func performOnboardingAction() {
        switch onboardingPhase {
        case .discoverableProviders, .needsConnection:
            coordinator.select(.providersAndAccounts)
            showOnboardingManually = false
            dismissedOnboardingForSession = true
        case .collecting:
            store.reload()
        case .ready:
            coordinator.select(.activity)
            showOnboardingManually = false
        case .hidden:
            break
        }
    }

    private var routeBinding: Binding<UsageDetailsRoute?> {
        Binding(
            get: { coordinator.route },
            set: {
                guard let route = $0 else { return }
                let needsInitialLedgerLoad = ActivityRouteLoadingPolicy.loadsLedgerAfterSelection(
                    from: coordinator.route, to: route
                )
                coordinator.select(route)
                if needsInitialLedgerLoad { store.reload() }
            }
        )
    }

    @ViewBuilder private var content: some View {
        if coordinator.route == .automation {
            AutomationPage()
                .background(.background)
        } else if let data = store.displayData {
            if coordinator.route == .providersAndAccounts {
                ProvidersPage(
                    data: data,
                    reload: store.reload,
                    openSystemIntegrations: { coordinator.select(.dataHealth) }
                )
                    .background(.background)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        ActivityHeader(store: store, updatedAt: data.latestCollectionAt)
                        if let error = store.error {
                            StatusBanner(symbol: "exclamationmark.triangle", text: error.localizedDescription)
                        }
                        if data.selectionMatch != .matched {
                            NoMatchView(
                                match: data.selectionMatch,
                                providerName: { data.providerDescriptor(for: $0).displayName },
                                retry: store.reload, clear: store.clearFilters
                            )
                        } else {
                            switch coordinator.route {
                            case .activity: ActivityPage(store: store, data: data)
                            case .capacity: CapacityPage(data: data)
                            case .apiSpend: APISpendPage(store: store, data: data)
                            case .localTools: LocalToolsPage(store: store, data: data)
                            case .providersAndAccounts: EmptyView()
                            case .dataHealth: DataHealthPage(data: data, retry: store.reload)
                            case .automation: EmptyView()
                            }
                        }
                    }
                    .padding(28)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .background(.background)
            }
        } else if store.isLoading {
            VStack(spacing: 12) {
                ProgressView()
                Text("Loading usage data").foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            FailureView(error: store.error, retry: store.reload)
        }
    }
}

private struct ActivityWindowRegistrationView: NSViewRepresentable {
    let registry: ActivityWindowRegistry

    func makeNSView(context: Context) -> WindowProbeView {
        WindowProbeView(registry: registry)
    }

    func updateNSView(_ view: WindowProbeView, context: Context) {
        view.registry = registry
        view.registerCurrentWindow()
    }

    final class WindowProbeView: NSView {
        var registry: ActivityWindowRegistry
        private weak var registeredWindow: NSWindow?

        init(registry: ActivityWindowRegistry) {
            self.registry = registry
            super.init(frame: .zero)
        }

        @available(*, unavailable)
        required init?(coder: NSCoder) { nil }

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if let registeredWindow, registeredWindow !== newWindow {
                registry.unregister(registeredWindow)
                self.registeredWindow = nil
            }
            super.viewWillMove(toWindow: newWindow)
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            registerCurrentWindow()
        }

        func registerCurrentWindow() {
            guard let window else { return }
            registry.register(window)
            registeredWindow = window
        }
    }
}
