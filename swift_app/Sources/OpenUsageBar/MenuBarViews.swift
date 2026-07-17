import SwiftUI
import UsageCore

struct StatusLabelView: View {
    let label: StatusLabel
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "chart.bar.xaxis")
            ForEach(label.values, id: \.self) { Text($0).monospacedDigit() }
        }
    }
}

struct MenuBarPopover: View {
    @Bindable var model: MenuBarViewModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            today
            capacityHeader
            ScrollView {
                LazyVStack(spacing: 0) {
                    if model.groups.isEmpty { emptyState } else { ForEach(model.groups) { provider($0) } }
                }
            }
            .frame(minHeight: 120, maxHeight: 330)
            Button("View all providers") { HelperLauncher.openActivity(route: "capacity") }
                .buttonStyle(.plain).foregroundStyle(.tint).padding(.vertical, 10)
            Divider()
            footer
        }
        .frame(width: 400)
        .background(.regularMaterial)
        .task { model.loadLastGoodOnce(); model.checkFreshness() }
        .onMoveCommand { direction in
            if direction == .down { model.perform(.moveSelection(1)) }
            if direction == .up { model.perform(.moveSelection(-1)) }
        }
        .onKeyPress(keys: [.return, .space, .escape]) { press in
            if press.key == .escape { model.perform(MenuKeyRouter.action(for: .escape, hasExpansion: model.expandedProviderID != nil)) }
            else { model.perform(.activateSelection) }
            return .handled
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text("OpenUsage Bar").font(.headline)
                Text(model.updatedAge).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button { model.refresh() } label: { Label(model.isRefreshing ? "Refreshing" : "Refresh", systemImage: "arrow.clockwise") }
                .buttonStyle(.borderless).disabled(model.isRefreshing).keyboardShortcut("r", modifiers: .command)
        }
        .padding(.horizontal, 16).padding(.vertical, 13)
    }

    private var today: some View {
        HStack {
            Text("Today Token").foregroundStyle(.secondary)
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(model.todayPresentation.value)
                    .font(.title3.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(model.summary?.todayTokens == nil ? .secondary : .primary)
                if let coverage = model.todayPresentation.coverage {
                    Label(coverage, systemImage: "circle.lefthalf.filled")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Today Token")
        .accessibilityValue(model.todayPresentation.accessibilityValue)
    }

    private var capacityHeader: some View {
        HStack {
            Text("Capacity").font(.subheadline.weight(.semibold))
            Spacer()
            Text("Most urgent first").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 4)
    }

    private func provider(_ group: ProviderCapacityGroup) -> some View {
        let item = ProviderRowPresentation(group.primary)
        return VStack(spacing: 0) {
            Button { model.selectedProviderID = group.id; model.toggle(group.id) } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.provider).fontWeight(.medium)
                        Text(item.window).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 8)
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 4) {
                            if let symbol = item.stateSymbol { Image(systemName: symbol).imageScale(.small) }
                            Text(item.capacity).fontWeight(.semibold).monospacedDigit()
                        }
                        .foregroundStyle(item.isCritical ? Color.red : item.isWarning ? Color.orange : Color.primary)
                        ForEach(item.visibleMetadata, id: \.self) {
                            Text($0).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    if !group.secondary.isEmpty { Image(systemName: model.expandedProviderID == group.id ? "chevron.up" : "chevron.down").font(.caption).foregroundStyle(.secondary) }
                }
                .contentShape(Rectangle()).padding(.horizontal, 16).padding(.vertical, 9)
            }
            .buttonStyle(.plain)
            .background(model.selectedProviderID == group.id ? Color.accentColor.opacity(0.10) : Color.clear)
            .accessibilityLabel(item.accessibilityLabel).accessibilityValue(item.accessibilityValue)
            if model.expandedProviderID == group.id { ForEach(Array(group.secondary), id: \.recordID) { secondary($0) } }
        }
    }

    private func secondary(_ row: CapacityItem) -> some View {
        let item = ProviderRowPresentation(row)
        return HStack {
            Text(item.window).foregroundStyle(.secondary)
            Spacer()
            HStack(spacing: 4) {
                if let symbol = item.stateSymbol { Image(systemName: symbol).imageScale(.small) }
                Text(item.capacity).monospacedDigit()
            }
            .foregroundStyle(riskColor(item.riskLevel))
            VStack(alignment: .trailing, spacing: 1) {
                ForEach(item.visibleMetadata, id: \.self) { Text($0) }
            }
            .foregroundStyle(.secondary)
        }
        .font(.caption).padding(.leading, 34).padding(.trailing, 16).padding(.vertical, 6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(item.accessibilityLabel)
        .accessibilityValue(item.accessibilityValue)
    }

    private func riskColor(_ level: ProviderRiskLevel) -> Color {
        switch level {
        case .critical: .red
        case .warning: .orange
        case .normal: .primary
        }
    }

    private var emptyState: some View {
        VStack(spacing: 6) {
            Image(systemName: "tray").foregroundStyle(.secondary)
            Text(model.displayError == nil ? "No capacity data" : "Last-good data unavailable").fontWeight(.medium)
            Text(model.displayError ?? "Connect a supported provider in Settings.").font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(24).frame(maxWidth: .infinity)
    }

    private var footer: some View {
        HStack(spacing: 14) {
            Button("Open Usage Details") { HelperLauncher.openActivity() }.keyboardShortcut("d", modifiers: .command)
            Spacer()
            if model.hasHealthIssues { Button("Data Health") { HelperLauncher.openHealth() } }
            Button("Settings") { HelperLauncher.openSettings() }.keyboardShortcut(",", modifiers: .command)
        }
        .buttonStyle(.plain).font(.caption).padding(.horizontal, 16).padding(.vertical, 11)
    }
}
