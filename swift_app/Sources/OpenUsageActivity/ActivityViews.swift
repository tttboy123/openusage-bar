import AppKit
import Charts
import SwiftUI
import UsageCore

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
            .navigationTitle("OpenUsage")
            .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 250)
        } detail: {
            content
                .navigationTitle(coordinator.route.title)
                .toolbar {
                    ToolbarItem {
                        Button("Refresh", systemImage: "arrow.clockwise") { store.reload() }
                            .disabled(store.isLoading)
                    }
                }
        }
        .overlay {
            if onboardingPhase != .hidden {
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
            store.reload()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            if store.data != nil { store.revalidateSelection() }
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
            set: { if let route = $0 { coordinator.select(route) } }
        )
    }

    @ViewBuilder private var content: some View {
        if let data = store.displayData {
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
                            case .apiSpend: APISpendPage(data: data)
                            case .localTools: LocalToolsPage(store: store, data: data)
                            case .providersAndAccounts: EmptyView()
                            case .dataHealth: DataHealthPage(data: data, retry: store.reload)
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
