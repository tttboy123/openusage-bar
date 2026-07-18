import AppKit
import Charts
import SwiftUI
import UsageCore

struct CapacityPage: View {
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
                            Text(AppLocalization.text(
                                row.quotaName.replacingOccurrences(of: "_", with: " ").capitalized
                            )).font(.caption).foregroundStyle(.secondary)
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
                            Button(AppLocalization.text(
                                showingAllHistorySeries ? "Focus 6" : "Show all"
                            )) {
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
                .accessibilityLabel(AppLocalization.format(
                    "%@, %@", item.seriesLabel,
                    AppLocalization.text(isVisible ? "shown" : "hidden")
                ))
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
