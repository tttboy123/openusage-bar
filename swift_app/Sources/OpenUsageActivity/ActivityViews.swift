import AppKit
import Charts
import SwiftUI
import UsageCore

struct ActivityRootView: View {
    @Bindable var store: ActivityViewStore
    @Bindable var coordinator: ActivityRouteCoordinator
    let windowRegistry: ActivityWindowRegistry
    @Environment(\.openWindow) private var openWindow

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
        .onAppear {
            coordinator.installWindowOpener { openWindow(id: "usage-details") }
            store.reload()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            if store.data != nil { store.revalidateSelection() }
        }
        .onDisappear { store.cancel() }
        .background(ActivityWindowRegistrationView(registry: windowRegistry).frame(width: 0, height: 0))
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

private struct ActivityPage: View {
    @Bindable var store: ActivityViewStore
    let data: ActivityLoadedData

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            MetricStrip(metrics: data.details.metrics, period: store.period)
            Divider()
            HeatmapSection(store: store, days: data.details.heatmapDetails)
            Divider()
            ModelChartSection(store: store, model: data.details)
        }
    }
}

private struct ActivityHeader: View {
    @Bindable var store: ActivityViewStore
    let updatedAt: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Usage Details").font(.largeTitle.weight(.semibold))
                    Text(updatedAt.map { "Latest collection \(DateText.display($0))" } ?? "Collection time unavailable")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                if store.isLoading { ProgressView().controlSize(.small) }
            }
            HStack(spacing: 12) {
                Picker("Period", selection: $store.period) {
                    ForEach(UsagePeriod.allCases, id: \.self) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented).frame(width: 270)
                Picker("Provider", selection: $store.providerID) {
                    Text("All Providers").tag(String?.none)
                    ForEach(store.providers, id: \.self) {
                        Text(store.data?.providerDescriptor(for: $0).displayName ?? DisplayText.provider($0))
                            .tag(Optional($0))
                    }
                    if let selected = store.providerID, !store.providers.contains(selected) {
                        Text("\(store.data?.providerDescriptor(for: selected).displayName ?? DisplayText.provider(selected)) (Unavailable)")
                            .tag(Optional(selected))
                    }
                }
                .frame(maxWidth: 220)
                Picker("Model", selection: $store.modelID) {
                    Text("All Models").tag(String?.none)
                    ForEach(store.models, id: \.self) { Text(DisplayText.model($0)).tag(Optional($0)) }
                    if let selected = store.modelID, !store.models.contains(selected) {
                        Text("\(DisplayText.model(selected)) (Unavailable)").tag(Optional(selected))
                    }
                }
                .frame(maxWidth: 240)
                Spacer()
            }
        }
        .onChange(of: store.period) { store.filtersDidChange() }
        .onChange(of: store.providerID) { store.filtersDidChange() }
        .onChange(of: store.modelID) { store.filtersDidChange() }
    }
}

private struct MetricStrip: View {
    let metrics: ActivityMetrics
    let period: UsagePeriod

    var body: some View {
        HStack(spacing: 0) {
            MetricValue(
                value: metrics.totalTokens.map(TokenText.compact)
                    ?? (metrics.observedTokens > 0 ? TokenText.compact(metrics.observedTokens) : "Unavailable"),
                label: metrics.isComplete ? "Total Tokens" : "Observed Tokens",
                state: metrics.isComplete ? nil : (metrics.observedTokens > 0 ? "Partial" : "Missing")
            )
            stripDivider
            MetricValue(
                value: metrics.peak.map { TokenText.compact($0.tokens) } ?? "Unavailable",
                label: metrics.isComplete ? "Peak Day" : "Observed Peak",
                state: metrics.peak.map { $0.day.rawValue }
            )
            stripDivider
            MetricValue(
                value: "\(metrics.activeDays)",
                label: metrics.isComplete ? "Active Days" : "Observed Active Days",
                state: "days"
            )
            stripDivider
            MetricValue(
                value: "\(metrics.currentStreak)",
                label: metrics.isComplete ? "Current Streak" : "Observed Streak",
                state: "days"
            )
            stripDivider
            MetricValue(
                value: "\(metrics.longestStreak)",
                label: metrics.isComplete ? "Longest Streak" : "Longest Observed Streak",
                state: "days"
            )
        }
        .padding(.vertical, 14)
        .overlay(alignment: .top) { Divider() }
        .overlay(alignment: .bottom) { Divider() }
        .accessibilityElement(children: .contain)
    }

    private var stripDivider: some View { Divider().frame(height: 48).padding(.horizontal, 18) }
}

private struct MetricValue: View {
    let value: String
    let label: String
    let state: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.title2.weight(.medium)).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
            if let state {
                Label(state, systemImage: state == "Partial" ? "circle.lefthalf.filled" : "info.circle")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct HeatmapSection: View {
    @Bindable var store: ActivityViewStore
    let days: [HeatmapDayDetail]
    @FocusState private var gridFocused: Bool
    @State private var hoveredDay: HeatmapDayDetail?
    private let rows = Array(
        repeating: GridItem(.fixed(HeatmapGeometry.cellSize), spacing: HeatmapGeometry.spacing),
        count: HeatmapGeometry.rowCount
    )

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 16) {
                SectionHeading("Daily Token Activity", detail: "One square per local calendar day")
                    .fixedSize(horizontal: true, vertical: false)
                Spacer(minLength: 12)
                Group {
                    if let hoveredDay {
                        HeatmapHoverSummary(day: hoveredDay)
                    } else {
                        Color.clear
                    }
                }
                .frame(width: 420, height: 18, alignment: .trailing)
            }
            if days.isEmpty {
                EmptyDataView(title: "No Token history", description: "History appears after a successful daily collection.")
            } else if let layout {
                ScrollViewReader { proxy in
                    ScrollView(.horizontal) {
                        VStack(alignment: .leading, spacing: 6) {
                            monthLabels(layout)
                            LazyHGrid(rows: rows, spacing: HeatmapGeometry.spacing) {
                                ForEach(layout.slots) { slot in
                                    Group {
                                        if let day = slot.detail {
                                            HeatmapCell(
                                                day: day,
                                                selected: store.heatmapFocusDay == day.activity.day
                                            )
                                            .frame(
                                                width: HeatmapGeometry.cellSize,
                                                height: HeatmapGeometry.cellSize
                                            )
                                            .contentShape(Rectangle())
                                            .onTapGesture {
                                                store.heatmapFocusDay = day.activity.day
                                                hoveredDay = day
                                            }
                                            .accessibilityHidden(true)
                                        } else {
                                            Color.clear
                                                .frame(
                                                    width: HeatmapGeometry.cellSize,
                                                    height: HeatmapGeometry.cellSize
                                                )
                                                .allowsHitTesting(false)
                                                .accessibilityHidden(true)
                                        }
                                    }
                                    .id(slot.position)
                                }
                            }
                            .focusable()
                            .focused($gridFocused)
                            .onMoveCommand { direction in move(direction, in: layout) }
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel("Annual daily Token activity")
                            .accessibilityValue(selectedSummary ?? heatmapSummary)
                            .accessibilityHint("Use arrow keys to inspect calendar days")
                            .contentShape(Rectangle())
                            .onContinuousHover { phase in
                                updateHover(phase, in: layout)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                    .defaultScrollAnchor(.trailing)
                    .onAppear { scrollToLatest(layout, proxy: proxy) }
                    .onChange(of: HeatmapScrollTarget.latestPosition(in: layout)) {
                        scrollToLatest(layout, proxy: proxy)
                    }
                }
                HStack(spacing: 6) {
                    Text("Missing").foregroundStyle(.secondary)
                    legendCell(.missing)
                    Text("Partial").foregroundStyle(.secondary)
                    legendCell(.partial, observedTokens: 1, heatLevel: 3)
                    Text("Covered zero").foregroundStyle(.secondary)
                    legendCell(.coveredZero)
                    Spacer()
                    Text("Lower").foregroundStyle(.secondary)
                    ForEach(1...5, id: \.self) { level in
                        RoundedRectangle(cornerRadius: 2).fill(Color.accentColor.opacity(HeatmapCell.opacity(level)))
                            .frame(width: 13, height: 13)
                    }
                    Text("Higher").foregroundStyle(.secondary)
                }
                .font(.caption)
            }
        }
    }

    private func monthLabels(_ layout: HeatmapCalendarLayout) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(layout.monthAnchors, id: \.key) { anchor in
                Text(DateText.month(anchor.key))
                    .font(.caption2).foregroundStyle(.secondary)
                    .offset(x: CGFloat(anchor.column) * (HeatmapGeometry.cellSize + HeatmapGeometry.spacing))
            }
        }
        .frame(
            width: CGFloat(layout.columnCount) * (HeatmapGeometry.cellSize + HeatmapGeometry.spacing),
            height: 14, alignment: .leading
        )
    }

    private var heatmapSummary: String {
        let missing = days.filter { $0.activity.state == .missing }.count
        let partial = days.filter { $0.activity.state == .partial }.count
        let active = days.filter { $0.activity.observedTokens > 0 }.count
        return "\(days.count) days, \(active) observed active, \(partial) partial, \(missing) missing"
    }

    private var layout: HeatmapCalendarLayout? {
        guard let first = days.first?.activity.day, let last = days.last?.activity.day else { return nil }
        return HeatmapCalendarLayout(range: first...last, details: days)
    }

    private var selectedSummary: String? {
        guard let selected = store.heatmapFocusDay else { return nil }
        return days.first { $0.activity.day == selected }?.accessibilitySummary
    }

    private func move(_ direction: MoveCommandDirection, in layout: HeatmapCalendarLayout) {
        guard let mapped = direction.heatmapDirection,
              let first = layout.slots.firstIndex(where: { $0.detail != nil })
        else { return }
        let current = store.heatmapFocusDay.flatMap { selected in
            layout.slots.firstIndex { $0.detail?.activity.day == selected }
        } ?? first
        let destination = layout.destination(from: current, direction: mapped)
        store.heatmapFocusDay = layout.slots[destination].detail?.activity.day
    }

    private func scrollToLatest(_ layout: HeatmapCalendarLayout, proxy: ScrollViewProxy) {
        guard let target = HeatmapScrollTarget.latestPosition(in: layout) else { return }
        proxy.scrollTo(target, anchor: .trailing)
    }

    private func updateHover(_ phase: HoverPhase, in layout: HeatmapCalendarLayout) {
        switch phase {
        case .active(let location):
            hoveredDay = HeatmapPointerTarget.position(
                x: location.x, y: location.y, slotCount: layout.slots.count
            ).flatMap { layout.slots[$0].detail }
        case .ended:
            hoveredDay = nil
        }
    }

    private func legendCell(
        _ state: ActivityDayState, observedTokens: Int64 = 0, heatLevel: Int? = nil
    ) -> some View {
        HeatmapCell(day: HeatmapDayDetail(
            activity: ActivityDay(
                day: try! LocalDay("2000-01-01"), state: state,
                totalTokens: state == .coveredZero ? 0 : nil, observedTokens: observedTokens,
                heatLevel: state == .coveredZero ? 0 : heatLevel
            ), quality: state == .missing ? .missing : .exact, lastCollectionAt: nil
        )).frame(width: 13, height: 13).accessibilityHidden(true)
    }
}

private struct HeatmapCell: View {
    let day: HeatmapDayDetail
    let selected: Bool
    @Environment(\.colorSchemeContrast) private var contrast

    init(day: HeatmapDayDetail, selected: Bool = false) {
        self.day = day
        self.selected = selected
    }

    var body: some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(fill)
            .overlay {
                if day.activity.state == .missing || day.activity.state == .partial {
                    RoundedRectangle(cornerRadius: 2).stroke(.secondary.opacity(0.45), lineWidth: 0.75)
                }
            }
            .overlay {
                if selected {
                    RoundedRectangle(cornerRadius: 2).stroke(Color.accentColor, lineWidth: 2)
                }
            }
            .accessibilityLabel(day.activity.day.rawValue)
            .accessibilityValue(help)
    }

    private var fill: Color {
        return switch day.activity.state {
        case .missing: Color(nsColor: .quaternaryLabelColor).opacity(0.12)
        case .partial:
            day.activity.observedTokens > 0
                ? Color.accentColor.opacity(Self.opacity(day.activity.heatLevel ?? 1, increased: contrast == .increased))
                : Color(nsColor: .secondaryLabelColor).opacity(0.2)
        case .coveredZero: Color(nsColor: .separatorColor).opacity(contrast == .increased ? 0.7 : 0.4)
        case .coveredActive: Color.accentColor.opacity(Self.opacity(day.activity.heatLevel ?? 1, increased: contrast == .increased))
        }
    }

    private var help: String {
        HeatmapTooltipText(day).accessibilityValue
    }

    static func opacity(_ level: Int, increased: Bool = false) -> Double {
        let values = increased ? [0.35, 0.5, 0.65, 0.82, 1.0] : [0.22, 0.38, 0.55, 0.72, 0.92]
        return values[max(1, min(5, level)) - 1]
    }
}

private struct HeatmapHoverSummary: View {
    let day: HeatmapDayDetail
    private var text: HeatmapTooltipText { HeatmapTooltipText(day) }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(text.title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(text.value)
                .font(.caption.weight(.semibold))
                .monospacedDigit()
            if !text.metadata.isEmpty {
                Text(text.metadata)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: 420, alignment: .trailing)
    }
}

private struct ModelChartSection: View {
    @Bindable var store: ActivityViewStore
    let model: UsageDetailsModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var keyboardFocused: Bool
    @State private var tooltipSize = CGSize.zero
    private let seriesColors: [Color] = [
        .blue, .orange, .green, .purple, .pink, .teal,
        .indigo, .mint, .cyan, .brown, .yellow, .gray,
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeading(
                "Daily Model Trend",
                detail: "Most recent 30 days, up to 12 named models"
            )
            if model.chartDays.isEmpty || presentation.displayState == .noSeries {
                EmptyDataView(title: "No model trend", description: "No matching model activity is available.")
            } else {
                Chart(presentation.seriesPoints) { point in
                    BarMark(
                        x: .value("Day", point.day.rawValue),
                        y: .value("Tokens", point.tokens)
                    )
                    .foregroundStyle(by: .value("Model", point.modelID))
                }
                .chartXScale(domain: model.chartDays.map { $0.day.rawValue })
                .chartForegroundStyleScale(
                    domain: presentation.series.map(\.modelID),
                    range: presentation.series.map(seriesColor)
                )
                .chartLegend(.hidden)
                .chartYAxis { AxisMarks(position: .leading) }
                .chartOverlay { proxy in
                    GeometryReader { geometry in
                        ZStack(alignment: .topLeading) {
                            Rectangle().fill(.clear).contentShape(Rectangle())
                                .onContinuousHover { phase in
                                    switch phase {
                                    case let .active(location):
                                        updatePointerFocus(
                                            at: location, proxy: proxy, geometry: geometry
                                        )
                                    case .ended: store.chartFocus.clear(.pointerExit)
                                    }
                                }

                            if let selected,
                               let anchor = tooltipAnchor(
                                   for: selected, proxy: proxy, geometry: geometry
                               ) {
                                let center = TooltipPlacement.center(
                                    anchor: anchor,
                                    tooltip: ChartSize(
                                        width: tooltipSize.width,
                                        height: tooltipSize.height
                                    ),
                                    container: ChartSize(
                                        width: geometry.size.width,
                                        height: geometry.size.height
                                    )
                                )
                                ChartTooltip(day: selected)
                                    .background {
                                        GeometryReader { tooltipGeometry in
                                            Color.clear.preference(
                                                key: TooltipSizePreferenceKey.self,
                                                value: tooltipGeometry.size
                                            )
                                        }
                                    }
                                    .position(x: center.x, y: center.y)
                                    .allowsHitTesting(false)
                                    .transition(tooltipTransition)
                            }
                        }
                        .onPreferenceChange(TooltipSizePreferenceKey.self) {
                            tooltipSize = $0
                        }
                        .animation(tooltipAnimation, value: selected != nil)
                    }
                }
                .frame(minHeight: 260)
                .focusable()
                .focused($keyboardFocused)
                .onMoveCommand { direction in moveChartFocus(direction) }
                .onExitCommand { store.chartFocus.clear(.escape) }
                .onReceive(NotificationCenter.default.publisher(for: NSWindow.didResignKeyNotification)) { _ in
                    store.chartFocus.clear(.windowDeactivation)
                }
                .overlay {
                    if presentation.displayState == .allHidden {
                        VStack(spacing: 8) {
                            Text("All model series hidden").foregroundStyle(.secondary)
                            Button("Show All") { store.showAllModelSeries() }
                        }
                    }
                }
                .accessibilityLabel("Daily stacked model Token trend")
                .accessibilityValue(selected?.accessibilitySummary ?? "Use arrow keys to inspect a day")
                modelLegend
            }
        }
        .onChange(of: model.revision) { store.chartFocus.clear(.filterChange) }
        .onChange(of: model.filterSignature) { store.chartFocus.clear(.filterChange) }
    }

    private var selected: DailyChartDay? {
        store.chartFocus.day.flatMap { day in presentation.chartDays.first { $0.day == day } }
    }

    private var presentation: ModelSeriesPresentation {
        model.modelSeriesPresentation(visibility: store.modelSeriesVisibility)
    }

    private var modelLegend: some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 150), spacing: 10, alignment: .leading)],
            alignment: .leading, spacing: 8
        ) {
            ForEach(presentation.series) { series in
                let action = series.isVisible ? "Hide" : "Show"
                Button {
                    store.toggleModelSeries(
                        series.modelID, availableSeriesIDs: model.modelSeriesIDs
                    )
                } label: {
                    HStack(spacing: 7) {
                        Circle()
                            .fill(series.isVisible ? seriesColor(series) : Color.clear)
                            .overlay(Circle().stroke(seriesColor(series), lineWidth: 1.5))
                            .frame(width: 9, height: 9)
                        Text(DisplayText.model(series.modelID))
                            .strikethrough(!series.isVisible)
                            .foregroundStyle(series.isVisible ? .primary : .secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("\(action) \(DisplayText.model(series.modelID)) series")
                .accessibilityLabel("\(action) \(DisplayText.model(series.modelID)) series")
                .accessibilityValue(series.isVisible ? "Visible" : "Hidden")
            }
        }
        .accessibilityLabel("Model series")
    }

    private func seriesColor(_ series: ModelSeriesDescriptor) -> Color {
        seriesColors[series.styleIndex % seriesColors.count]
    }

    private var tooltipTransition: AnyTransition {
        TooltipMotionPolicy.appearance(reduceMotion: reduceMotion) == .opacity
            ? .opacity : .identity
    }

    private var tooltipAnimation: Animation? {
        TooltipMotionPolicy.appearance(reduceMotion: reduceMotion) == .opacity
            ? .easeOut(duration: 0.12) : nil
    }

    private func updatePointerFocus(
        at location: CGPoint, proxy: ChartProxy, geometry: GeometryProxy
    ) {
        guard let plotFrame = proxy.plotFrame else {
            store.chartFocus.clear(.pointerExit)
            return
        }
        let frame = geometry[plotFrame]
        guard frame.contains(location),
              let index = ChartSelection.index(
                  at: location.x - frame.minX,
                  plotWidth: frame.width,
                  count: model.chartDays.count
              )
        else {
            store.chartFocus.clear(.pointerExit)
            return
        }
        store.chartFocus.selectPointer(
            day: model.chartDays[index].day,
            anchor: ChartAnchor(x: location.x, y: location.y)
        )
    }

    private func tooltipAnchor(
        for day: DailyChartDay, proxy: ChartProxy, geometry: GeometryProxy
    ) -> ChartAnchor? {
        switch store.chartFocus.source {
        case .pointer:
            return store.chartFocus.pointerAnchor
        case .keyboard:
            guard let plotFrame = proxy.plotFrame,
                  let index = model.chartDays.firstIndex(where: { $0.day == day.day })
            else { return nil }
            let frame = geometry[plotFrame]
            guard let x = KeyboardBarAnchor.centerX(
                index: index, count: model.chartDays.count, plotWidth: frame.width
            ) else { return nil }
            let visibleTokens = day.composition.reduce(Int64(0)) { $0 + $1.tokens }
            let top = proxy.position(forY: visibleTokens) ?? 0
            let baseline = proxy.position(forY: Int64(0)) ?? frame.height
            return ChartAnchor(
                x: frame.minX + x,
                y: frame.minY + ((top + baseline) / 2)
            )
        case nil:
            return nil
        }
    }

    private func moveChartFocus(_ direction: MoveCommandDirection) {
        guard direction == .left || direction == .right, !model.chartDays.isEmpty else { return }
        let current = store.chartFocus.day.flatMap { day in model.chartDays.firstIndex { $0.day == day } }
            ?? (direction == .right ? -1 : model.chartDays.count)
        let index = max(
            0, min(model.chartDays.count - 1, current + (direction == .right ? 1 : -1))
        )
        store.chartFocus.selectKeyboard(day: model.chartDays[index].day)
    }
}

private struct TooltipSizePreferenceKey: PreferenceKey {
    static let defaultValue = CGSize.zero

    static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
        value = nextValue()
    }
}

private struct ChartTooltip: View {
    let day: DailyChartDay

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(day.day.rawValue) · \(day.totalTokens.map(TokenText.compact) ?? "Unavailable") Tokens")
                .font(.headline).monospacedDigit()
            ForEach(day.composition, id: \.modelID) { item in
                HStack {
                    Text(DisplayText.model(item.modelID))
                    Spacer(minLength: 18)
                    Text(TokenText.compact(item.tokens)).monospacedDigit()
                }
            }
            Label(day.quality.displayName, systemImage: day.quality.symbol)
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(10).frame(width: 230)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .overlay { RoundedRectangle(cornerRadius: 8).stroke(.separator, lineWidth: 0.5) }
        .accessibilityElement(children: .combine)
    }
}

private struct CapacityPage: View {
    let data: ActivityLoadedData
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading("Capacity", detail: "Subscription quota is separate from Token activity")
            CapacitySection(
                rows: data.capacity, history: data.quotaHistory,
                historyIsPartial: data.quotaHistoryIsPartial
            )
        }
    }
}

private struct CapacitySection: View {
    let rows: [CapacityItem]
    let history: [QuotaHistoryPoint]
    let historyIsPartial: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var historyFocus = QuotaHistoryFocus()
    @State private var historyTooltipSize = CGSize.zero
    @State private var historySeriesMode: QuotaHistorySeriesMode = .focused
    private let seriesColors: [Color] = [.blue, .green, .orange, .purple, .pink, .teal, .indigo, .brown]

    private var chart: QuotaHistoryChartPresentation {
        QuotaHistoryChartPresentation(points: history)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeading("Subscription Capacity", detail: "Current remaining quota and reset windows")
            if rows.isEmpty {
                EmptyDataView(title: "No subscription capacity", description: "Configured providers may still have Token activity.")
            } else {
                ForEach(rows, id: \.recordID) { row in
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(row.providerDescriptor.displayName).font(.headline)
                            Text(row.quotaName).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if row.stale { Label("Stale", systemImage: "clock.badge.exclamationmark").foregroundStyle(.orange) }
                        Text(CapacityText.value(row)).font(.title3.weight(.medium)).monospacedDigit()
                        Text(DateText.reset(row.resetsAt)).foregroundStyle(.secondary)
                    }
                    Divider()
                }
            }
            if !history.isEmpty || historyIsPartial {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Quota History").font(.headline)
                            Text("Remaining quota over time; lines break for replenishments or collection gaps")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if !visibleResetMarkers.isEmpty {
                            Label("New window", systemImage: "diamond.fill")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if chart.changingSeries.count > 6 {
                            Button(showingAllHistorySeries ? "Focus 6" : "Show all") {
                                historySeriesMode = showingAllHistorySeries ? .focused : .showAll
                                historyFocus.clear(.filterChange)
                            }
                            .buttonStyle(.link)
                            .font(.caption)
                        }
                        if historyIsPartial {
                            Label("Partial history", systemImage: "clock.arrow.circlepath")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }

                    if !chart.changingSeries.isEmpty {
                        Chart {
                            ForEach(visiblePlotPoints) { point in
                                LineMark(
                                    x: .value("Observed", point.observedAt),
                                    y: .value("Remaining", point.remainingRatio * 100),
                                    series: .value("Quota window", point.lineSegmentID)
                                )
                                .foregroundStyle(by: .value("Quota series", point.styleKey))
                                .lineStyle(stroke(for: point.seriesID))
                            }
                            ForEach(visibleResetMarkers) { point in
                                PointMark(
                                    x: .value("Observed", point.observedAt),
                                    y: .value("Remaining", point.remainingRatio * 100)
                                )
                                .foregroundStyle(by: .value("Quota series", point.styleKey))
                                .symbol(.diamond)
                                .symbolSize(46)
                            }
                            ForEach(isolatedOnlyMarkers) { point in
                                PointMark(
                                    x: .value("Observed", point.observedAt),
                                    y: .value("Remaining", point.remainingRatio * 100)
                                )
                                .foregroundStyle(by: .value("Quota series", point.styleKey))
                                .symbolSize(22)
                                .opacity(0.72)
                            }
                            ForEach(latestOnlyMarkers) { point in
                                PointMark(
                                    x: .value("Observed", point.observedAt),
                                    y: .value("Remaining", point.remainingRatio * 100)
                                )
                                .foregroundStyle(by: .value("Quota series", point.styleKey))
                                .symbolSize(38)
                            }
                            if let selectedPoint {
                                RuleMark(x: .value("Selected observation", selectedPoint.observedAt))
                                    .foregroundStyle(.secondary.opacity(0.24))
                                    .lineStyle(StrokeStyle(lineWidth: 1))
                                PointMark(
                                    x: .value("Selected observation", selectedPoint.observedAt),
                                    y: .value("Selected remaining", selectedPoint.remainingRatio * 100)
                                )
                                .foregroundStyle(color(for: selectedPoint.seriesID))
                                .symbolSize(74)
                            }
                        }
                        .chartForegroundStyleScale(
                            domain: visibleChangingSeries.map(\.styleKey),
                            range: visibleChangingSeries.map { color(for: $0.seriesID) }
                        )
                        .chartLegend(.hidden)
                        .chartXScale(domain: historyTimeDomain.range)
                        .chartYScale(domain: 0...100)
                        .chartYAxis {
                            AxisMarks(position: .leading, values: [0, 25, 50, 75, 100]) { value in
                                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                                    .foregroundStyle(.secondary.opacity(0.22))
                                AxisValueLabel {
                                    if let percentage = value.as(Int.self) {
                                        Text("\(percentage)%")
                                    }
                                }
                            }
                        }
                        .chartXAxis {
                            if historyAxisMode == .hours || historyAxisMode == .dateHours {
                                AxisMarks(values: .stride(
                                    by: .hour, count: historyTimeSpan <= 36 * 60 * 60 ? 6 : 12
                                )) { value in
                                    AxisValueLabel {
                                        if let date = value.as(Date.self) {
                                            Text(QuotaHistoryAxisText.label(date, mode: historyAxisMode))
                                                .multilineTextAlignment(.center)
                                        }
                                    }
                                }
                            } else {
                                AxisMarks(values: .automatic(desiredCount: 5)) { value in
                                    AxisValueLabel {
                                        if let date = value.as(Date.self) {
                                            Text(QuotaHistoryAxisText.label(date, mode: .days))
                                        }
                                    }
                                }
                            }
                        }
                        .chartOverlay { proxy in
                            GeometryReader { geometry in
                                ZStack(alignment: .topLeading) {
                                    Rectangle().fill(.clear).contentShape(Rectangle())
                                        .onContinuousHover { phase in
                                            switch phase {
                                            case let .active(location):
                                                updateHistoryPointer(
                                                    at: location, proxy: proxy, geometry: geometry
                                                )
                                            case .ended:
                                                historyFocus.clear(.pointerExit)
                                            }
                                        }

                                    if let selectedPoint,
                                       let anchor = historyTooltipAnchor(
                                           for: selectedPoint, proxy: proxy, geometry: geometry
                                       ) {
                                        let tooltipSize = QuotaHistoryTooltipGeometry.placementSize(
                                            measured: ChartSize(
                                                width: historyTooltipSize.width,
                                                height: historyTooltipSize.height
                                            )
                                        )
                                        let center = TooltipPlacement.center(
                                            anchor: anchor,
                                            tooltip: tooltipSize,
                                            container: ChartSize(
                                                width: geometry.size.width,
                                                height: geometry.size.height
                                            )
                                        )
                                        QuotaHistoryTooltip(point: selectedPoint)
                                            .background {
                                                GeometryReader { tooltipGeometry in
                                                    Color.clear.preference(
                                                        key: TooltipSizePreferenceKey.self,
                                                        value: tooltipGeometry.size
                                                    )
                                                }
                                            }
                                            .position(x: center.x, y: center.y)
                                            .allowsHitTesting(false)
                                            .transition(historyTooltipTransition)
                                    }
                                }
                                .onPreferenceChange(TooltipSizePreferenceKey.self) {
                                    historyTooltipSize = $0
                                }
                                .animation(historyTooltipAnimation, value: selectedPoint != nil)
                            }
                        }
                        .frame(height: 184)
                        .focusable()
                        .onMoveCommand(perform: moveHistoryFocus)
                        .onExitCommand { historyFocus.clear(.escape) }
                        .onReceive(NotificationCenter.default.publisher(
                            for: NSWindow.didResignKeyNotification
                        )) { _ in
                            historyFocus.clear(.windowDeactivation)
                        }
                        .accessibilityLabel("Subscription remaining quota history")
                        .accessibilityValue(
                            selectedPoint.map { QuotaHistoryTooltipText.make(point: $0).accessibilityValue }
                                ?? accessibilitySummary
                        )
                        .accessibilityHint("Use left and right arrow keys to inspect observations")
                        .accessibilityAdjustableAction { direction in
                            switch direction {
                            case .increment: moveHistoryFocus(.right)
                            case .decrement: moveHistoryFocus(.left)
                            @unknown default: break
                            }
                        }

                        changingLegend
                    } else if !history.isEmpty {
                        Text("No quota movement in this period.")
                            .font(.callout).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading)
                    }

                    if !chart.unchangedSeries.isEmpty {
                        unchangedSummary
                    }
                }
                .padding(.top, 6)
            }
        }
        .onChange(of: history) {
            historyFocus.clear(.filterChange)
        }
    }

    private var allChangingSeriesIDs: Set<String> {
        Set(chart.changingSeries.map(\.seriesID))
    }

    private var effectiveVisibleSeriesIDs: Set<String> {
        QuotaHistorySeriesVisibility.visibleIDs(
            mode: historySeriesMode, in: chart.changingSeries
        )
    }

    private var showingAllHistorySeries: Bool {
        effectiveVisibleSeriesIDs == allChangingSeriesIDs
    }

    private var visibleChangingSeries: [QuotaHistorySeriesPresentation] {
        chart.changingSeries.filter { effectiveVisibleSeriesIDs.contains($0.seriesID) }
    }

    private var visiblePlotPoints: [QuotaHistoryPoint] {
        chart.plotPoints.filter { effectiveVisibleSeriesIDs.contains($0.seriesID) }
    }

    private var visibleResetMarkers: [QuotaHistoryPoint] {
        chart.resetMarkers.filter { effectiveVisibleSeriesIDs.contains($0.seriesID) }
    }

    private var historyTimeSpan: TimeInterval {
        historyTimeDomain.span
    }

    private var historyTimeDomain: QuotaHistoryTimeDomain {
        QuotaHistoryTimeDomain.make(points: chart.plotPoints)
            ?? QuotaHistoryTimeDomain(
                lowerBound: Date().addingTimeInterval(-60),
                upperBound: Date().addingTimeInterval(60)
            )
    }

    private var historyAxisMode: QuotaHistoryAxisMode {
        QuotaHistoryAxisText.mode(for: historyTimeSpan)
    }

    private var selectedPoint: QuotaHistoryPoint? {
        historyFocus.snapshotID.flatMap { snapshotID in
            visiblePlotPoints.first { $0.snapshotID == snapshotID }
        }
    }

    private var latestOnlyMarkers: [QuotaHistoryPoint] {
        let resetIDs = Set(visibleResetMarkers.map(\.snapshotID))
        return chart.latestMarkers.filter {
            effectiveVisibleSeriesIDs.contains($0.seriesID) && !resetIDs.contains($0.snapshotID)
        }
    }

    private var isolatedOnlyMarkers: [QuotaHistoryPoint] {
        let emphasized = Set(
            visibleResetMarkers.map(\.snapshotID) + chart.latestMarkers
                .filter { effectiveVisibleSeriesIDs.contains($0.seriesID) }.map(\.snapshotID)
        )
        return chart.isolatedSegmentMarkers.filter {
            effectiveVisibleSeriesIDs.contains($0.seriesID) && !emphasized.contains($0.snapshotID)
        }
    }

    private var changingLegend: some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 220), spacing: 16, alignment: .leading)],
            alignment: .leading, spacing: 8
        ) {
            ForEach(chart.changingSeries) { item in
                let isVisible = effectiveVisibleSeriesIDs.contains(item.seriesID)
                Button {
                    historySeriesMode = .custom(QuotaHistorySeriesVisibility.toggled(
                        item.seriesID, visible: effectiveVisibleSeriesIDs,
                        allSeries: allChangingSeriesIDs
                    ))
                    historyFocus.clear(.filterChange)
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: isVisible ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(isVisible ? color(for: item.seriesID) : .secondary)
                            .accessibilityHidden(true)
                        QuotaHistoryLineSwatch(
                            color: color(for: item.seriesID),
                            dash: dash(for: item.seriesID)
                        )
                        Text(item.seriesLabel).lineLimit(1).truncationMode(.middle)
                        Spacer(minLength: 8)
                        if item.latestPoint.stale {
                            Image(systemName: "clock.badge.exclamationmark")
                                .foregroundStyle(.orange)
                                .help("Latest value is stale")
                                .accessibilityHidden(true)
                        }
                        Text(CapacityText.percentage(item.currentRatio))
                            .monospacedDigit().foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .opacity(isVisible ? 1 : 0.48)
                .font(.caption)
                .accessibilityLabel("\(item.seriesLabel), \(isVisible ? "shown" : "hidden")")
                .accessibilityValue(QuotaHistoryLegendText.accessibilityValue(
                    percentage: CapacityText.percentage(item.currentRatio),
                    stale: item.latestPoint.stale
                ))
                .accessibilityHint("Toggles this series in the chart")
            }
        }
        .accessibilityLabel("Changing quota series")
    }

    private var unchangedSummary: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("Unchanged in this period")
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 220), spacing: 16, alignment: .leading)],
                alignment: .leading, spacing: 6
            ) {
                ForEach(chart.unchangedSeries) { item in
                    HStack(spacing: 8) {
                        Circle().fill(.secondary.opacity(0.45)).frame(width: 6, height: 6)
                            .accessibilityHidden(true)
                        Text(item.seriesLabel).lineLimit(1).truncationMode(.middle)
                        Spacer(minLength: 8)
                        if item.latestPoint.stale {
                            Image(systemName: "clock.badge.exclamationmark")
                                .foregroundStyle(.orange)
                                .help("Latest value is stale")
                                .accessibilityHidden(true)
                        }
                        Text(CapacityText.percentage(item.currentRatio))
                            .monospacedDigit().foregroundStyle(.secondary)
                    }
                    .font(.caption)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(item.seriesLabel)
                    .accessibilityValue(QuotaHistoryLegendText.accessibilityValue(
                        percentage: CapacityText.percentage(item.currentRatio),
                        stale: item.latestPoint.stale
                    ))
                }
            }
        }
    }

    private var accessibilitySummary: String {
        visibleChangingSeries.map {
            "\($0.seriesLabel), \(CapacityText.percentage($0.currentRatio)) remaining"
        }.joined(separator: ", ")
    }

    private var historyTooltipTransition: AnyTransition {
        TooltipMotionPolicy.appearance(reduceMotion: reduceMotion) == .opacity
            ? .opacity : .identity
    }

    private var historyTooltipAnimation: Animation? {
        TooltipMotionPolicy.appearance(reduceMotion: reduceMotion) == .opacity
            ? .easeOut(duration: 0.12) : nil
    }

    private func updateHistoryPointer(
        at location: CGPoint, proxy: ChartProxy, geometry: GeometryProxy
    ) {
        guard let frame = proxy.plotFrame else {
            historyFocus.clear(.pointerExit)
            return
        }
        let plotFrame = geometry[frame]
        let targets = historyTargets(proxy: proxy, plotFrame: plotFrame)
        guard let point = QuotaHistorySelection.nearest(
            to: ChartAnchor(x: location.x, y: location.y),
            plotOrigin: ChartAnchor(x: plotFrame.minX, y: plotFrame.minY),
            plotSize: ChartSize(width: plotFrame.width, height: plotFrame.height),
            targets: targets
        ) else {
            historyFocus.clear(.pointerExit)
            return
        }
        historyFocus.selectPointer(
            snapshotID: point.snapshotID,
            anchor: ChartAnchor(x: location.x, y: location.y)
        )
    }

    private func historyTargets(
        proxy: ChartProxy, plotFrame: CGRect
    ) -> [QuotaHistoryPlotTarget] {
        visiblePlotPoints.compactMap { point in
            guard let x = proxy.position(forX: point.observedAt),
                  let y = proxy.position(forY: point.remainingRatio * 100)
            else { return nil }
            return QuotaHistoryPlotTarget(
                point: point,
                anchor: ChartAnchor(x: plotFrame.minX + x, y: plotFrame.minY + y)
            )
        }
    }

    private func historyTooltipAnchor(
        for point: QuotaHistoryPoint, proxy: ChartProxy, geometry: GeometryProxy
    ) -> ChartAnchor? {
        if historyFocus.source == .pointer { return historyFocus.pointerAnchor }
        guard let frame = proxy.plotFrame else { return nil }
        let plotFrame = geometry[frame]
        guard let x = proxy.position(forX: point.observedAt),
              let y = proxy.position(forY: point.remainingRatio * 100)
        else { return nil }
        return ChartAnchor(x: plotFrame.minX + x, y: plotFrame.minY + y)
    }

    private func moveHistoryFocus(_ direction: MoveCommandDirection) {
        let navigation: QuotaHistoryNavigationDirection
        switch direction {
        case .left: navigation = .left
        case .right: navigation = .right
        default: return
        }
        guard let point = QuotaHistorySelection.move(
            from: historyFocus.snapshotID, direction: navigation,
            points: visiblePlotPoints
        ) else { return }
        historyFocus.selectKeyboard(snapshotID: point.snapshotID)
    }

    private func styleKey(for seriesID: String) -> String {
        chart.series.first(where: { $0.seriesID == seriesID })?.styleKey ?? ""
    }

    private func color(for seriesID: String) -> Color {
        seriesColors[
            QuotaHistoryVisualStyle.colorIndex(styleKey: styleKey(for: seriesID))
                % seriesColors.count
        ]
    }

    private func dash(for seriesID: String) -> [CGFloat] {
        QuotaHistoryVisualStyle.dash(
            QuotaHistoryVisualStyle.dashIndex(styleKey: styleKey(for: seriesID))
        ).map { CGFloat($0) }
    }

    private func stroke(for seriesID: String) -> StrokeStyle {
        StrokeStyle(lineWidth: 1.75, lineCap: .round, lineJoin: .round, dash: dash(for: seriesID))
    }
}

private struct QuotaHistoryTooltip: View {
    let point: QuotaHistoryPoint

    private var text: QuotaHistoryTooltipText {
        QuotaHistoryTooltipText.make(point: point)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(text.title).font(.headline).lineLimit(2)
            Text(text.remaining).font(.title3.weight(.medium)).monospacedDigit()
            Text(text.observed).foregroundStyle(.secondary)
            if let reset = text.reset { Text(reset).foregroundStyle(.secondary) }
            if let status = text.status, let symbol = text.statusSymbol {
                Label(status, systemImage: symbol)
                    .foregroundStyle(.orange)
            }
        }
        .font(.caption)
        .padding(10)
        .frame(width: 220, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .overlay { RoundedRectangle(cornerRadius: 8).stroke(.separator, lineWidth: 0.5) }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(text.title)
        .accessibilityValue(text.detailAccessibilityValue)
    }
}

private struct QuotaHistoryLineSwatch: View {
    let color: Color
    let dash: [CGFloat]

    var body: some View {
        Path { path in
            path.move(to: CGPoint(x: 0, y: 4))
            path.addLine(to: CGPoint(x: 28, y: 4))
        }
        .stroke(
            color,
            style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: dash)
        )
        .frame(width: 28, height: 8)
        .accessibilityHidden(true)
    }
}

private struct APISpendPage: View {
    let data: ActivityLoadedData

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading("API Spend", detail: "Native currency values only")
            if data.apiSpend.totals.isEmpty {
                EmptyDataView(
                    title: data.apiSpend.coverage == .complete
                        ? "No spend reported"
                        : (data.apiSpend.coverage == .partial ? "Spend coverage incomplete" : "Spend unavailable"),
                    description: data.apiSpend.coverage == .complete
                        ? "Complete billing coverage reported no monetary charge for this selection."
                        : (data.apiSpend.coverage == .partial
                            ? "Some billing days or provider scopes have not reported a monetary value."
                            : "No authoritative or estimated API cost is present for this selection.")
                )
            } else {
                ForEach(data.apiSpend.totals, id: \.currency) { total in
                    HStack {
                        Label(total.quality.title, systemImage: total.quality.symbol)
                        Spacer()
                        Text(APISpendText.display(amount: total.amount, currency: total.currency)).monospacedDigit()
                    }
                    Divider()
                }
            }
        }
    }
}

private struct LocalToolsPage: View {
    @Bindable var store: ActivityViewStore
    let data: ActivityLoadedData
    private var tools: [ProviderDisplayDescriptor] {
        data.visibleProviderIDs
            .filter { store.providerID == nil || store.providerID == $0 }
            .map(data.providerDescriptor)
            .filter { $0.category == .localTool }
    }
    private var summaries: [LocalToolUsageSummary] {
        LocalToolUsagePresentation.make(
            descriptors: tools, periodRecords: data.records,
            historyRecords: data.historyRecords, health: data.health.sources
        )
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading(
                "Local Tools",
                detail: "Token activity from local runtime logs, separate from subscription quota"
            )
            if summaries.isEmpty {
                EmptyDataView(title: "No local tools", description: "Hermes and OpenClaw appear here when detected.")
            } else {
                ForEach(summaries) { summary in
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text(summary.displayName).font(.title3.weight(.semibold))
                            Text("Local runtime").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            StateLabel(state: summary.state)
                        }
                        HStack(spacing: 36) {
                            LocalToolMetric(
                                value: summary.observedTokens > 0
                                    ? TokenText.compact(summary.observedTokens) : "No activity",
                                label: "\(store.period.title) Tokens"
                            )
                            LocalToolMetric(
                                value: "\(summary.activeDays)", label: "Active Days"
                            )
                            LocalToolMetric(
                                value: "\(summary.knownModelIDs.count)", label: "Known Models"
                            )
                            LocalToolMetric(
                                value: summary.lastActivityDay?.rawValue ?? "Unavailable",
                                label: "Last Activity"
                            )
                        }
                        HStack(spacing: 12) {
                            Label(summary.quality.displayName, systemImage: summary.quality.symbol)
                            if !summary.knownModelIDs.isEmpty {
                                Text(summary.knownModelIDs.map(DisplayText.model).joined(separator: ", "))
                                    .lineLimit(1).truncationMode(.tail)
                            }
                            Spacer()
                            Text(summary.lastCollectionAt.map { "Collected \(DateText.display($0))" }
                                ?? "Collection unavailable")
                        }
                        .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                    .accessibilityElement(children: .contain)
                    Divider()
                }
            }
        }
    }
}

private struct LocalToolMetric: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.headline).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ProvidersPage: View {
    let data: ActivityLoadedData
    let reload: () -> Void
    let openSystemIntegrations: () -> Void

    @State private var selectedCategory = ProviderBrowseCategory.all
    @State private var selectedFamilyID: String? = "minimax"
    @State private var selectedRegionID: String? = "cn"
    @State private var searchText = ""
    @State private var configuredConnections: [ProviderConnectionSummary] = []

    private var allItems: [ProviderCenterItem] {
        let instances = Dictionary(grouping: data.providerInstances, by: \.familyID)
        let configured = Dictionary(grouping: configuredConnections, by: \.familyID)
        let observedFamilies = Set(data.availableProviderIDs.map {
            data.providerDescriptor(for: $0).familyID
        })
        let issues = Dictionary(grouping: data.health.sources.compactMap {
            source -> (String, ProviderSourceIssuePresentation)? in
            let familyID = data.providerDescriptor(for: source.providerID).familyID
            guard !ProviderCenterPresentation.isSystemIntegration(familyID) else { return nil }
            return (familyID, ProviderSourceIssuePresentation.make(from: source))
        }, by: { $0.0 }).mapValues { rows in rows.map { $0.1 } }
        var descriptors = Dictionary(uniqueKeysWithValues: ProviderCatalog.allDescriptors.map {
            ($0.familyID, $0)
        })
        for descriptor in data.providerDescriptors.values where descriptors[descriptor.familyID] == nil {
            descriptors[descriptor.familyID] = descriptor
        }
        for connection in configuredConnections where descriptors[connection.familyID] == nil {
            descriptors[connection.familyID] = ProviderCatalog.descriptor(
                for: connection.providerID,
                familyID: connection.familyID,
                displayName: connection.displayName,
                category: .api
            )
        }
        return descriptors.values.filter {
            !ProviderCenterPresentation.isSystemIntegration($0.familyID)
        }.map { descriptor in
            let connectionIDs = Set(instances[descriptor.familyID, default: []].map(\.providerID))
                .union(configured[descriptor.familyID, default: []].map(\.providerID))
            return ProviderCenterItem(
                descriptor: descriptor,
                instanceCount: connectionIDs.count,
                observed: observedFamilies.contains(descriptor.familyID),
                issues: issues[descriptor.familyID, default: []]
            )
        }.sorted { left, right in
            let leftRank = left.status.sortRank
            let rightRank = right.status.sortRank
            if leftRank != rightRank { return leftRank < rightRank }
            let order = left.descriptor.displayName.localizedStandardCompare(right.descriptor.displayName)
            return order == .orderedSame ? left.id < right.id : order == .orderedAscending
        }
    }

    private var filteredItems: [ProviderCenterItem] {
        ProviderCenterPresentation.filter(
            allItems, category: selectedCategory, query: searchText
        )
    }

    private var selectedItem: ProviderCenterItem? {
        allItems.first { $0.id == selectedFamilyID }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Providers").font(.largeTitle.weight(.semibold))
                    Text("Connect services and inspect the data each source can provide")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                TextField("Search providers or clients", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 260)
                    .accessibilityLabel("Search providers or clients")
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 20)

            Divider()

            HSplitView {
                providerList
                    .frame(minWidth: 250, idealWidth: 290, maxWidth: 340)
                if let selectedItem {
                    ProviderConnectionDetail(
                        item: selectedItem,
                        instances: data.providerInstances.filter {
                            $0.familyID == selectedItem.descriptor.familyID
                        },
                        connections: configuredConnections.filter {
                            $0.familyID == selectedItem.descriptor.familyID
                        },
                        sources: providerSources(for: selectedItem.descriptor.familyID),
                        selectedRegionID: $selectedRegionID,
                        reload: {
                            loadConfiguredConnections()
                            reload()
                        }
                    )
                    .id(selectedItem.id)
                } else {
                    ContentUnavailableView(
                        "Select a Provider", systemImage: "bolt.horizontal.circle",
                        description: Text("Review connection methods and available data.")
                    )
                }
            }
        }
        .onAppear {
            synchronizeSelection()
            loadConfiguredConnections()
        }
        .onChange(of: selectedCategory) { synchronizeSelection() }
        .onChange(of: searchText) { synchronizeSelection() }
        .onChange(of: selectedFamilyID) { _, _ in synchronizeRegion() }
    }

    private var providerList: some View {
        VStack(spacing: 0) {
            Picker("Category", selection: $selectedCategory) {
                ForEach(ProviderBrowseCategory.allCases) { category in
                    Text(category.title).tag(category)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(12)

            Divider()

            if filteredItems.isEmpty {
                ContentUnavailableView.search(text: searchText)
            } else {
                List(selection: $selectedFamilyID) {
                    if selectedCategory == .all && searchText.isEmpty {
                        Section("System Integrations") {
                            Button(action: openSystemIntegrations) {
                                HStack(spacing: 10) {
                                    Image(systemName: "arrow.triangle.branch")
                                        .font(.system(size: 15, weight: .semibold))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 30, height: 30)
                                        .background(
                                            .secondary.opacity(0.12),
                                            in: RoundedRectangle(cornerRadius: 8)
                                        )
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("OpenUsage").font(.body.weight(.medium))
                                        Text(systemIntegrationSummary)
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help("Open OpenUsage data-source diagnostics")
                        }
                    }
                    Section("Providers") {
                        ForEach(filteredItems) { item in
                            ProviderCenterRow(item: item)
                                .tag(Optional(item.id))
                        }
                    }
                }
                .listStyle(.sidebar)
            }
        }
    }

    private var systemIntegrationSummary: String {
        let count = data.health.sources.filter { source in
            ProviderCenterPresentation.isSystemIntegration(
                data.providerDescriptor(for: source.providerID).familyID
            ) && !["ok", "available"].contains(source.effectiveState.lowercased())
        }.count
        return count == 0
            ? "Data source and compatibility"
            : "\(count) diagnostic issue\(count == 1 ? "" : "s")"
    }

    private func synchronizeSelection() {
        guard !filteredItems.contains(where: { $0.id == selectedFamilyID }) else { return }
        selectedFamilyID = filteredItems.first?.id
    }

    private func synchronizeRegion() {
        selectedRegionID = selectedItem?.descriptor.regions.sorted().first
    }

    private func providerSources(for familyID: String) -> [SourceHealthItem] {
        data.health.sources.filter {
            data.providerDescriptor(for: $0.providerID).familyID == familyID
        }
    }

    private func loadConfiguredConnections() {
        Task { @MainActor in
            configuredConnections = await Task.detached(priority: .utility) {
                (try? ProviderConnectionSummaryStore().load()) ?? []
            }.value.filter { !data.hiddenProviderIDs.contains($0.providerID) }
            synchronizeSelection()
        }
    }
}

private struct ProviderCenterRow: View {
    let item: ProviderCenterItem

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: item.category.symbol)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(item.category.color)
                .frame(width: 30, height: 30)
                .background(item.category.color.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.descriptor.displayName)
                    .font(.body.weight(.medium)).lineLimit(1)
                Text(ProviderCenterText.scope(item.descriptor)
                    ?? ProviderCenterText.connectionMethod(item.descriptor))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            Image(systemName: item.status.symbol)
                .foregroundStyle(item.status.color)
                .accessibilityLabel(item.status.title)
        }
        .padding(.vertical, 4)
        .help(item.helpText)
        .accessibilityElement(children: .combine)
    }
}

private struct ProviderConnectionDetail: View {
    let item: ProviderCenterItem
    let instances: [ProviderInstanceRecord]
    let connections: [ProviderConnectionSummary]
    let sources: [SourceHealthItem]
    @Binding var selectedRegionID: String?
    let reload: () -> Void

    @State private var editingProviderID: String?
    @State private var originalName = ""
    @State private var accountName = ""
    @State private var replacementAPIKey = ""
    @State private var replacementSession = ""
    @State private var isSaving = false
    @State private var editError: String?
    @State private var savedMessage: String?
    @FocusState private var focusedField: EditField?

    private enum EditField: Hashable { case name, apiKey, session }

    private var descriptor: ProviderDisplayDescriptor { item.descriptor }
    private var capability: ProviderCapabilityPresentation {
        ProviderCapabilityPresentation(descriptor: descriptor)
    }
    private var sourceIssues: [ProviderSourceIssuePresentation] {
        sources.map(ProviderSourceIssuePresentation.make).filter(\.isIssue)
            .sorted { left, right in
                if left.requiresUserAction != right.requiresUserAction {
                    return left.requiresUserAction
                }
                return left.title.localizedStandardCompare(right.title) == .orderedAscending
            }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                header
                Divider()
                if !connections.isEmpty || !instances.isEmpty { instanceSection }
                connectionSection
                if !sourceIssues.isEmpty { sourceIssueSection }
                capabilitySection
            }
            .frame(maxWidth: 720, alignment: .leading)
            .padding(.horizontal, 34)
            .padding(.vertical, 28)
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: item.category.symbol)
                .font(.system(size: 23, weight: .semibold))
                .foregroundStyle(item.category.color)
                .frame(width: 50, height: 50)
                .background(item.category.color.opacity(0.12), in: RoundedRectangle(cornerRadius: 13))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(descriptor.displayName).font(.title2.weight(.semibold))
                Text(ProviderCenterText.scope(descriptor) ?? item.category.title)
                    .foregroundStyle(.secondary)
                Label(item.status.title, systemImage: item.status.symbol)
                    .font(.caption).foregroundStyle(item.status.color)
            }
            Spacer()
        }
    }

    private var connectionSection: some View {
        ProviderDetailSection(
            title: "Connection Setup",
            detail: "Add another account or review how this Provider connects. Saved credentials are never displayed."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if descriptor.regions.count > 1 {
                    LabeledContent("Site") {
                        Picker("Site", selection: $selectedRegionID) {
                            ForEach(descriptor.regions.sorted(), id: \.self) { region in
                                Text(ProviderCenterText.region(region)).tag(Optional(region))
                            }
                        }
                        .labelsHidden().pickerStyle(.segmented).frame(width: 250)
                    }
                } else if let scope = ProviderCenterText.scope(descriptor) {
                    LabeledContent("Site", value: scope)
                }
                LabeledContent(
                    "Connection method",
                    value: ProviderCenterText.connectionMethod(descriptor)
                )
                LabeledContent(
                    "Multiple accounts",
                    value: descriptor.supportsAccounts ? "Supported" : "Not declared"
                )
                HStack {
                    Button(
                        connections.isEmpty ? "Add Connection" : "Add Account",
                        systemImage: "plus"
                    ) {
                        SettingsHelper.open()
                    }
                    .controlSize(.large)
                    Button("Refresh Data", systemImage: "arrow.clockwise", action: reload)
                        .controlSize(.large)
                }
                if connections.contains(where: { $0.isManaged }) {
                    Text("Existing app-managed accounts are edited in Connections above.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var sourceIssueSection: some View {
        ProviderDetailSection(
            title: "Data Source Issues",
            detail: "These diagnostics do not mark the Provider connection as failed unless credentials need attention."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(sourceIssues.enumerated()), id: \.element.id) { index, issue in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: issue.requiresUserAction
                            ? "exclamationmark.triangle.fill"
                            : "clock.badge.exclamationmark")
                            .foregroundStyle(issue.requiresUserAction ? .red : .orange)
                            .frame(width: 18)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(issue.message).font(.callout.weight(.medium))
                            if let lastSuccessAt = issue.lastSuccessAt {
                                Text("Last successful update: \(DateText.display(lastSuccessAt))")
                                    .font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text("No successful update recorded")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                    }
                    if index < sourceIssues.count - 1 { Divider() }
                }
            }
        }
    }

    private var capabilitySection: some View {
        ProviderDetailSection(
            title: "Available Data",
            detail: "Unknown means OpenUsage Bar has no reliable declaration. It is not zero."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(capability.groups, id: \.state) { group in
                    HStack(alignment: .firstTextBaseline) {
                        Label(group.title, systemImage: group.state.symbol)
                            .foregroundStyle(group.state.color)
                        Spacer()
                        Text(group.items.isEmpty
                            ? "None"
                            : group.items.map(\.title).joined(separator: ", "))
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                    }
                    .font(.callout)
                    Divider()
                }
            }
        }
    }

    private var instanceSection: some View {
        ProviderDetailSection(
            title: "Connections",
            detail: connections.contains { $0.isManaged }
                ? "Edit app-managed account labels and replace saved credentials here. Blank credential fields keep their current Keychain values."
                : "These connections are discovered from local tools or OpenUsage and are read only here."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                if let savedMessage {
                    Label(savedMessage, systemImage: "checkmark.circle.fill")
                        .font(.callout)
                        .foregroundStyle(.green)
                        .accessibilityLabel("Success: \(savedMessage)")
                }
                ForEach(connections) { connection in
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(connection.displayName).font(.callout.weight(.medium))
                            Text(connectionMetadata(connection))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if connection.isManaged {
                            Button("Edit Connection", systemImage: "pencil") {
                                beginEditing(connection)
                            }
                            .buttonStyle(.borderless)
                            .disabled(isSaving)
                        } else {
                            Text("Read only")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .frame(minHeight: 44)
                    if editingProviderID == connection.providerID {
                        inlineEditor(for: connection)
                            .padding(.vertical, 8)
                    }
                    Divider()
                }
                ForEach(instances.filter { instance in
                    !connections.contains { $0.providerID == instance.providerID }
                }) { instance in
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(instance.displayName).font(.callout.weight(.medium))
                            Text("Observed source · \(DateText.display(instance.observedAt))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text("Read only")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .frame(minHeight: 44)
                    Divider()
                }
            }
        }
    }

    @ViewBuilder
    private func inlineEditor(for connection: ProviderConnectionSummary) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Edit \(connection.displayName)")
                    .font(.headline)
                Spacer()
                Text(connection.isStepPlan
                    ? "Site remains locked to this connection"
                    : "Connection type remains unchanged")
                    .font(.caption).foregroundStyle(.secondary)
            }

            LabeledContent("Account label") {
                TextField("Account label", text: $accountName)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 390)
                    .focused($focusedField, equals: .name)
                    .disabled(isSaving)
            }
            if let site = connection.site {
                LabeledContent("Site", value: ProviderCenterText.region(site))
            }
            LabeledContent(connection.credentialLabel) {
                SecureField(connection.credentialPlaceholder, text: $replacementAPIKey)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 390)
                    .focused($focusedField, equals: .apiKey)
                    .disabled(isSaving)
            }
            if connection.isStepPlan {
                LabeledContent("Replacement web session") {
                    SecureField("Leave blank to keep the saved session", text: $replacementSession)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 390)
                        .focused($focusedField, equals: .session)
                        .disabled(isSaving)
                }
            }
            Text(connection.isStepPlan
                ? "Blank credential fields keep the existing values. China and International credentials cannot be moved between sites."
                : "Blank credential fields keep the existing Keychain value. Provider protocol and endpoint settings remain unchanged.")
                .font(.caption).foregroundStyle(.secondary)

            if let editError {
                Label(editError, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .accessibilityLabel("Error: \(editError)")
            }

            HStack {
                Spacer()
                Button("Cancel") { cancelEditing() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(isSaving)
                Button("Save Changes") { save(connection) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canSave || isSaving)
                    .overlay {
                        if isSaving { ProgressView().controlSize(.small) }
                    }
            }
            .controlSize(.large)
        }
        .padding(.leading, 16)
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.accentColor).frame(width: 2)
        }
        .onSubmit { if canSave && !isSaving { save(connection) } }
    }

    private var canSave: Bool {
        let trimmedName = accountName.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmedName.isEmpty
            && (trimmedName != originalName
                || !replacementAPIKey.isEmpty
                || !replacementSession.isEmpty)
    }

    private func beginEditing(_ connection: ProviderConnectionSummary) {
        editingProviderID = connection.providerID
        originalName = connection.displayName
        accountName = connection.displayName
        replacementAPIKey = ""
        replacementSession = ""
        editError = nil
        savedMessage = nil
        focusedField = .name
    }

    private func cancelEditing() {
        editingProviderID = nil
        originalName = ""
        accountName = ""
        replacementAPIKey = ""
        replacementSession = ""
        editError = nil
        focusedField = nil
    }

    private func save(_ connection: ProviderConnectionSummary) {
        guard let command = ProviderMutationCommand.resolve(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: Bundle.main.executableURL ?? Bundle.main.bundleURL
        ) else {
            editError = ProviderMutationFailure.unavailable.message
            return
        }
        let request = ProviderEditRequest(
            providerID: connection.providerID,
            name: accountName.trimmingCharacters(in: .whitespacesAndNewlines),
            apiKey: replacementAPIKey,
            sessionCookie: replacementSession
        )
        isSaving = true
        editError = nil
        Task { @MainActor in
            let result = await ProviderMutationService.submit(request, command: command)
            isSaving = false
            switch result {
            case let .success(response) where response.ok:
                savedMessage = response.message
                cancelEditing()
                savedMessage = response.message
                reload()
            case let .success(response):
                editError = response.message
                focusedField = .name
            case let .failure(failure):
                editError = failure.message
            }
        }
    }

    private func connectionMetadata(_ connection: ProviderConnectionSummary) -> String {
        let site = ProviderCenterText.region(connection.site ?? "Configured")
        guard let observed = instances.first(where: {
            $0.providerID == connection.providerID
        }) else { return "\(site) · Not collected yet" }
        return "\(site) · \(DateText.display(observed.observedAt))"
    }
}

private struct ProviderDetailSection<Content: View>: View {
    let title: String
    let detail: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text(title).font(.headline)
            Text(detail).font(.callout).foregroundStyle(.secondary)
            content
        }
    }
}

private struct DataHealthPage: View {
    let data: ActivityLoadedData
    let retry: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading("Data Health", detail: "Sanitized collection status")
            if data.visibilityIssue {
                StatusBanner(symbol: "eye.slash", text: "Provider visibility settings are invalid. All providers remain visible.")
            }
            let integrationSources = data.health.sources.filter { source in
                !OpenUsageCatalogPresentation.isCatalogSource(source)
                    && ProviderCenterPresentation.isSystemIntegration(
                        data.providerDescriptor(for: source.providerID).familyID
                    )
            }
            if OpenUsageCatalogPresentation.from(data.health.sources) != nil
                || !integrationSources.isEmpty {
                Text("System Integrations").font(.title2.weight(.semibold))
            }
            if let catalog = OpenUsageCatalogPresentation.from(data.health.sources) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text("OpenUsage compatibility").font(.headline)
                        Text("Provider catalog").foregroundStyle(.secondary)
                        Spacer()
                        StateLabel(state: catalog.state)
                    }
                    LabeledContent("Status", value: catalog.title)
                    LabeledContent("Providers", value: catalog.countSummary)
                    Text("Diagnostic only; readable provider data remains available.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Divider()
            }
            ForEach(integrationSources, id: \.stableID) { source in
                let issue = ProviderSourceIssuePresentation.make(from: source)
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text("OpenUsage").font(.headline)
                        Text(issue.title).foregroundStyle(.secondary)
                        Spacer()
                        StateLabel(state: source.effectiveState)
                    }
                    if issue.isIssue {
                        Text(issue.message).font(.callout)
                    } else {
                        Text("OpenUsage data collection is available.").font(.callout)
                    }
                    LabeledContent("Last attempt", value: DateText.display(source.lastAttemptAt))
                    LabeledContent(
                        "Last success",
                        value: source.lastSuccessAt.map(DateText.display) ?? "Unavailable"
                    )
                }
                Divider()
            }
            let providerSources = data.health.sources.filter {
                !OpenUsageCatalogPresentation.isCatalogSource($0)
                    && !ProviderCenterPresentation.isSystemIntegration(
                        data.providerDescriptor(for: $0.providerID).familyID
                    )
            }
            if providerSources.isEmpty
                && integrationSources.isEmpty
                && OpenUsageCatalogPresentation.from(data.health.sources) == nil {
                EmptyDataView(title: "No source status", description: "No collection source has reported status yet.")
            } else {
                if !providerSources.isEmpty {
                    Text("Provider Data Sources").font(.title2.weight(.semibold))
                }
                ForEach(providerSources, id: \.stableID) { source in
                    let descriptor = data.providerDescriptor(for: source.providerID)
                    let runtimeSource = ProviderRuntimeSourcePresentation.resolve(
                        runtimeSourceID: source.sourceID, descriptor: descriptor
                    )
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(descriptor.displayName).font(.headline)
                            Text(runtimeSource.roleTitle).foregroundStyle(.secondary)
                            Spacer()
                            StateLabel(state: source.effectiveState)
                        }
                        if runtimeSource.strategies.isEmpty {
                            LabeledContent("Strategy", value: "Uncatalogued source")
                        } else {
                            LabeledContent("Strategy") {
                                Text(runtimeSource.strategies.map(\.summary).joined(separator: "; "))
                                    .multilineTextAlignment(.trailing)
                            }
                            LabeledContent("Platforms", value: runtimeSource.platforms)
                        }
                        LabeledContent("Last attempt", value: DateText.display(source.lastAttemptAt))
                        LabeledContent("Last success", value: source.lastSuccessAt.map(DateText.display) ?? "Unavailable")
                        if let stale = source.staleAt { LabeledContent("Stale after", value: DateText.display(stale)) }
                        if let code = source.errorCode { LabeledContent("Error code", value: SourceText.errorCode(code)) }
                    }
                    Divider()
                }
            }
            HStack {
                Button("Retry", systemImage: "arrow.clockwise") { retry() }
                Button("Repair in Provider Settings", systemImage: "wrench.and.screwdriver") { SettingsHelper.open() }
            }
        }
    }
}

struct OpenUsageCatalogPresentation: Sendable, Hashable {
    let outcome: String
    let expectedCount: Int
    let actualCount: Int
    let missingCount: Int
    let extraCount: Int

    static let providerID = "openusage_catalog"
    static let sourceID = "openusage.detect"
    private static let outcomes = Set([
        "ok", "openusage_unavailable", "unsupported_openusage_version",
        "provider_catalog_drift", "invalid_detect_output", "timeout",
    ])

    static func isCatalogSource(_ source: SourceHealthItem) -> Bool {
        source.providerID == providerID && source.sourceID == sourceID
    }

    static func globalHealthHasIssues(_ sources: [SourceHealthItem]) -> Bool {
        sources.contains {
            !isCatalogSource($0) && $0.effectiveState.lowercased() != "ok"
        }
    }

    static func from(_ sources: [SourceHealthItem]) -> Self? {
        guard let source = sources.first(where: isCatalogSource) else { return nil }
        if source.state == "ok", source.errorCode == nil {
            return Self(
                outcome: "ok", expectedCount: upstreamCount,
                actualCount: upstreamCount,
                missingCount: 0, extraCount: 0
            )
        }
        guard let code = source.errorCode else { return invalid }
        let expression = try? NSRegularExpression(
            pattern: #"^([a-z_]+)_e([0-9]+)_a([0-9]+)_m([0-9]+)_x([0-9]+)$"#
        )
        let range = NSRange(code.startIndex..<code.endIndex, in: code)
        guard let match = expression?.firstMatch(in: code, range: range),
              match.range == range,
              let outcomeRange = Range(match.range(at: 1), in: code),
              let expectedRange = Range(match.range(at: 2), in: code),
              let actualRange = Range(match.range(at: 3), in: code),
              let missingRange = Range(match.range(at: 4), in: code),
              let extraRange = Range(match.range(at: 5), in: code),
              outcomes.contains(String(code[outcomeRange])),
              let expected = Int(code[expectedRange]),
              let actual = Int(code[actualRange]),
              let missing = Int(code[missingRange]),
              let extra = Int(code[extraRange])
        else { return invalid }
        return Self(
            outcome: String(code[outcomeRange]), expectedCount: expected,
            actualCount: actual, missingCount: missing, extraCount: extra
        )
    }

    private static var invalid: Self {
        Self(
            outcome: "invalid_detect_output", expectedCount: upstreamCount,
            actualCount: 0, missingCount: 0, extraCount: 0
        )
    }

    private static var upstreamCount: Int {
        GeneratedProviderCatalog.upstreamFamilyIDs.count
    }

    var state: String { outcome == "ok" ? "ok" : "diagnostic" }
    var isGlobalFailure: Bool { false }
    var title: String {
        switch outcome {
        case "ok": "Compatible"
        case "openusage_unavailable": "OpenUsage unavailable"
        case "unsupported_openusage_version": "Unsupported OpenUsage version"
        case "provider_catalog_drift": "Provider catalog changed"
        case "timeout": "Compatibility check timed out"
        default: "Compatibility check unavailable"
        }
    }
    var countSummary: String {
        if outcome == "provider_catalog_drift" {
            return "\(actualCount) detected · \(missingCount) missing · \(extraCount) extra"
        }
        return "\(actualCount) of \(expectedCount) detected"
    }
}

private struct NoMatchView: View {
    let match: ActivitySelectionMatch
    let providerName: (String) -> String
    let retry: () -> Void
    let clear: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("No matching usage data", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text(description)
        } actions: {
            Button("Retry", systemImage: "arrow.clockwise", action: retry)
            Button("Clear Filters", systemImage: "xmark.circle", action: clear)
        }
        .frame(maxWidth: .infinity, minHeight: 280)
    }

    private var description: String {
        switch match {
        case .matched: "No matching usage data is available."
        case let .noMatchingProvider(id): "The selected Provider \(providerName(id)) is unavailable or hidden."
        case let .noMatchingModel(id): "The selected model \(DisplayText.model(id)) has no visible activity."
        }
    }
}

private struct FailureView: View {
    let error: RepositoryError?
    let retry: () -> Void
    var body: some View {
        ContentUnavailableView {
            Label(error == .databaseUnavailable ? "Usage database unavailable" : "Usage details unavailable", systemImage: "externaldrive.badge.exclamationmark")
        } description: {
            Text(error?.localizedDescription ?? "No matching usage data is available.")
        } actions: {
            Button("Retry") { retry() }
        }
    }
}

private struct EmptyDataView: View {
    let title: String
    let description: String
    var body: some View {
        ContentUnavailableView(title, systemImage: "chart.bar.xaxis", description: Text(description))
            .frame(maxWidth: .infinity, minHeight: 140)
    }
}

private struct PageHeading: View {
    let title: String
    let detail: String
    init(_ title: String, detail: String) { self.title = title; self.detail = detail }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.largeTitle.weight(.semibold))
            Text(detail).foregroundStyle(.secondary)
        }
    }
}

private struct SectionHeading: View {
    let title: String
    let detail: String
    init(_ title: String, detail: String) { self.title = title; self.detail = detail }
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.title2.weight(.semibold))
            Text(detail).font(.callout).foregroundStyle(.secondary)
            Spacer()
        }
    }
}

private struct StatusBanner: View {
    let symbol: String
    let text: String
    var body: some View {
        Label(text, systemImage: symbol).font(.callout).foregroundStyle(.orange)
            .padding(.vertical, 8)
    }
}

private struct StateLabel: View {
    let state: String
    var body: some View {
        Label(SourceText.state(state), systemImage: SourceText.symbol(state))
            .font(.caption).foregroundStyle(SourceText.color(state))
    }
}

private enum SettingsHelper {
    @MainActor static func open() {
        let plan = ActivityHelperPlan.settings(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: Bundle.main.executableURL ?? Bundle.main.bundleURL
        )
        Task { @MainActor in
            guard await ActivityHelperLaunchService.live.launch(plan) == .launched else {
                showUnavailable()
                return
            }
        }
    }

    @MainActor private static func showUnavailable() {
            let alert = NSAlert()
            alert.messageText = "Provider Settings unavailable"
            alert.informativeText = "Reinstall OpenUsage Bar to restore the settings helper."
            alert.runModal()
    }
}

private enum AccountText {
    static func pseudonymous(_ value: String) -> String {
        guard !value.isEmpty else { return "Default" }
        let hash = value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
        return "Account " + String(format: "%08llx", hash).suffix(8)
    }
}

private enum CapacityText {
    static func value(_ row: CapacityItem) -> String {
        if let ratio = row.remainingRatio { return percentage(ratio) }
        if let remaining = row.remaining { return row.unit == "count" ? remaining : "\(remaining) \(row.unit)" }
        return "Unavailable"
    }

    static func percentage(_ ratio: Double) -> String {
        QuotaHistoryTooltipText.percentage(ratio)
    }
}

enum APISpendText {
    static func display(amount: Decimal, currency: String) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = false
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 6
        let number = formatter.string(from: NSDecimalNumber(decimal: amount))
            ?? NSDecimalNumber(decimal: amount).stringValue
        return "\(currency) \(number)"
    }
}

enum DateText {
    static func display(_ value: String) -> String {
        guard let date = ActivityTimestamp.date(from: value) else { return "Unavailable" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
    static func reset(_ value: String?) -> String { value.map { "Resets \(display($0))" } ?? "Reset unavailable" }
    static func month(_ yearMonth: String) -> String {
        let parts = yearMonth.split(separator: "-")
        guard parts.count == 2, let month = Int(parts[1]), (1...12).contains(month) else { return "" }
        return Calendar.current.shortMonthSymbols[month - 1]
    }
}

private enum SourceText {
    static func state(_ value: String) -> String { value.replacingOccurrences(of: "_", with: " ").capitalized }
    static func errorCode(_ value: String) -> String {
        value.range(of: #"^[A-Za-z0-9._-]{1,80}$"#, options: .regularExpression) == nil
            ? "unknown_error" : value
    }
    static func symbol(_ value: String) -> String {
        switch value { case "ok": "checkmark.circle"; case "stale": "clock.badge.exclamationmark"; default: "exclamationmark.triangle" }
    }
    static func color(_ value: String) -> Color { value == "ok" ? .secondary : .orange }
}

private extension UsageDetailsRoute {
    var title: String {
        switch self {
        case .activity: "Activity"; case .capacity: "Capacity"
        case .apiSpend: "API Spend"; case .localTools: "Local Tools"
        case .providersAndAccounts: "Providers"; case .dataHealth: "Data Health"
        }
    }
    var symbol: String {
        switch self {
        case .activity: "chart.bar.xaxis"; case .capacity: "gauge.with.dots.needle.50percent"
        case .apiSpend: "dollarsign.circle"
        case .localTools: "terminal"; case .providersAndAccounts: "bolt.horizontal.circle"
        case .dataHealth: "waveform.path.ecg"
        }
    }
}

private extension ProviderBrowseCategory {
    var title: String {
        switch self {
        case .all: "All"
        case .subscription: "Plans"
        case .api: "API"
        case .cloud: "Cloud"
        case .local: "Local"
        }
    }

    var symbol: String {
        switch self {
        case .all: "square.grid.2x2"
        case .subscription: "gauge.with.dots.needle.50percent"
        case .api: "key"
        case .cloud: "cloud"
        case .local: "terminal"
        }
    }

    var color: Color {
        .secondary
    }
}

private extension ProviderConnectionStatus {
    var sortRank: Int {
        switch self { case .attention: 0; case .connected: 1; case .available: 2 }
    }

    var title: String {
        switch self { case .available: "Available"; case .connected: "Connected"; case .attention: "Needs attention" }
    }

    var symbol: String {
        switch self { case .available: "circle"; case .connected: "checkmark.circle.fill"; case .attention: "exclamationmark.triangle.fill" }
    }

    var color: Color {
        switch self { case .available: .secondary; case .connected: .green; case .attention: .orange }
    }
}

private extension ProviderCapabilityState {
    var symbol: String {
        switch self { case .supported: "checkmark"; case .unsupported: "minus"; case .unknown: "questionmark" }
    }

    var color: Color {
        switch self { case .supported: .green; case .unsupported: .secondary; case .unknown: .orange }
    }
}

private extension UsagePeriod {
    var title: String { rawValue.capitalized }
}

private extension ActivityQuality {
    var symbol: String {
        switch self { case .exact: "checkmark.seal"; case .estimated: "function"; case .partial: "circle.lefthalf.filled"; case .missing: "questionmark.circle" }
    }
}

private extension ProviderProductCategory {
    var title: String { self == .localTool ? "Local Tool" : rawValue.capitalized }
}

private extension ProviderMetricFamily {
    var title: String {
        switch self { case .subscriptionQuota: "Subscription Quota"; case .tokenActivity: "Token Activity"; case .billing: "Billing"; case .operational: "Operational" }
    }
}

private extension CredentialSourceType {
    var title: String {
        switch self {
        case .none: "Provider owned"
        case .keychain: "Keychain"
        case .browserSession: "Browser Session"
        case .apiKey: "API Key"
        case .oauth: "OAuth"
        case .cli: "CLI"
        case .local: "Local"
        }
    }
}

private extension APISpendQuality {
    var title: String {
        switch self { case .reported: "Reported"; case .estimated: "Estimated"; case .partial: "Observed, partial" }
    }
    var symbol: String {
        switch self { case .reported: "checkmark.seal"; case .estimated: "function"; case .partial: "circle.lefthalf.filled" }
    }
}

private extension SourceHealthItem {
    var stableID: String { "\(providerID)|\(sourceID)" }
}

private extension MoveCommandDirection {
    var heatmapDirection: HeatmapDirection? {
        switch self { case .up: .up; case .down: .down; case .left: .left; case .right: .right; @unknown default: nil }
    }
}
