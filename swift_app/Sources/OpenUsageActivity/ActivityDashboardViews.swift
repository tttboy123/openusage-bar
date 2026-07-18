import AppKit
import Charts
import SwiftUI
import UsageCore

struct ActivityPage: View {
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

struct ActivityHeader: View {
    @Bindable var store: ActivityViewStore
    let updatedAt: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Usage Details").font(.largeTitle.weight(.semibold))
                    Text(updatedAt.map {
                        AppLocalization.format("Latest collection %@", DateText.display($0))
                    } ?? AppLocalization.text("Collection time unavailable"))
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
                        Text(AppLocalization.format(
                            "%@ (Unavailable)",
                            store.data?.providerDescriptor(for: selected).displayName
                                ?? DisplayText.provider(selected)
                        ))
                            .tag(Optional(selected))
                    }
                }
                .frame(maxWidth: 220)
                Picker("Model", selection: $store.modelID) {
                    Text("All Models").tag(String?.none)
                    ForEach(store.models, id: \.self) { Text(DisplayText.model($0)).tag(Optional($0)) }
                    if let selected = store.modelID, !store.models.contains(selected) {
                        Text(AppLocalization.format("%@ (Unavailable)", DisplayText.model(selected)))
                            .tag(Optional(selected))
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
                    ?? (metrics.observedTokens > 0
                        ? TokenText.compact(metrics.observedTokens)
                        : AppLocalization.text("Unavailable")),
                label: metrics.isComplete ? "Total Tokens" : "Observed Tokens",
                state: metrics.isComplete ? nil : (metrics.observedTokens > 0 ? "Partial" : "Missing")
            )
            stripDivider
            MetricValue(
                value: metrics.peak.map { TokenText.compact($0.tokens) }
                    ?? AppLocalization.text("Unavailable"),
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
            Text(AppLocalization.text(label)).font(.caption).foregroundStyle(.secondary)
            if let state {
                Label(
                    AppLocalization.text(state),
                    systemImage: state == "Partial" ? "circle.lefthalf.filled" : "info.circle"
                )
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
                    if let inspectedDay {
                        HeatmapHoverSummary(day: inspectedDay)
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
        return AppLocalization.format(
            "%lld days, %lld observed active, %lld partial, %lld missing",
            Int64(days.count), Int64(active), Int64(partial), Int64(missing)
        )
    }

    private var layout: HeatmapCalendarLayout? {
        guard let first = days.first?.activity.day, let last = days.last?.activity.day else { return nil }
        return HeatmapCalendarLayout(range: first...last, details: days)
    }

    private var selectedSummary: String? {
        guard let selected = store.heatmapFocusDay else { return nil }
        return days.first { $0.activity.day == selected }?.accessibilitySummary
    }

    private var inspectedDay: HeatmapDayDetail? {
        HeatmapInspectionSelection.visible(
            hovered: hoveredDay, selected: store.heatmapFocusDay, in: days
        )
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
                let action = AppLocalization.text(series.isVisible ? "Hide" : "Show")
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
                .help(AppLocalization.format(
                    "%@ %@ series", action, DisplayText.model(series.modelID)
                ))
                .accessibilityLabel(AppLocalization.format(
                    "%@ %@ series", action, DisplayText.model(series.modelID)
                ))
                .accessibilityValue(AppLocalization.text(series.isVisible ? "Visible" : "Hidden"))
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

struct TooltipSizePreferenceKey: PreferenceKey {
    static let defaultValue = CGSize.zero

    static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
        value = nextValue()
    }
}

private struct ChartTooltip: View {
    let day: DailyChartDay

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(AppLocalization.format(
                "%@ · %@ Tokens", day.day.rawValue,
                day.totalTokens.map(TokenText.compact) ?? AppLocalization.text("Unavailable")
            ))
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
            if !day.sourceIDs.isEmpty {
                Text(AppLocalization.format(
                    "Source %@", day.sourceIDs.joined(separator: ", ")
                ))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !day.qualityIDs.isEmpty {
                Text(AppLocalization.format(
                    "Quality %@", day.qualityIDs.joined(separator: ", ")
                ))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let collected = day.lastCollectionAt {
                Text(AppLocalization.format("Collected %@", DateText.display(collected)))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(10).frame(width: 230)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .overlay { RoundedRectangle(cornerRadius: 8).stroke(.separator, lineWidth: 0.5) }
        .accessibilityElement(children: .combine)
    }
}
