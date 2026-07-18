import Foundation
import SwiftUI
import UsageCore

struct ProvidersPage: View {
    let data: ActivityLoadedData
    let reload: () -> Void
    let openSystemIntegrations: () -> Void

    @State private var selectedCategory = ProviderBrowseCategory.all
    @State private var selectedFamilyID: String?
    @State private var selectedRegionID: String?
    @State private var searchText = ""
    @State private var configuredConnections: [ProviderConnectionSummary] = []
    @State private var discoveredConnections: [ProviderInstanceRecord] = []

    private var providerInstances: [ProviderInstanceRecord] {
        discoveredConnections.isEmpty ? data.providerInstances : discoveredConnections
    }

    private var allItems: [ProviderCenterItem] {
        let instances = Dictionary(grouping: providerInstances, by: \.familyID)
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
                        instances: providerInstances.filter {
                            $0.familyID == selectedItem.descriptor.familyID
                        },
                        connections: configuredConnections.filter {
                            $0.familyID == selectedItem.descriptor.familyID
                        },
                        hiddenProviderIDs: data.hiddenProviderIDs,
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
        if count == 0 { return AppLocalization.text("Data source and compatibility") }
        return AppLocalization.format(
            count == 1 ? "%lld diagnostic issue" : "%lld diagnostic issues",
            Int64(count)
        )
    }

    private func synchronizeSelection() {
        selectedFamilyID = ProviderCenterPresentation.selection(
            current: selectedFamilyID,
            visibleIDs: filteredItems.map(\.id)
        )
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
            let loaded = await Task.detached(priority: .utility) { () -> (
                [ProviderConnectionSummary], [ProviderInstanceRecord]
            ) in
                let configured = (try? ProviderConnectionSummaryStore().load()) ?? []
                guard let repository = try? UsageRepository(databaseURL: ActivityPaths.ledger)
                else { return (configured, []) }
                defer { repository.close() }
                return (configured, (try? repository.providerInstances()) ?? [])
            }.value
            configuredConnections = loaded.0
            discoveredConnections = loaded.1
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
    let hiddenProviderIDs: Set<String>
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
    @State private var showingAddConnection = false
    @State private var pendingRemoval: ProviderConnectionSummary?
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
        .sheet(isPresented: $showingAddConnection) {
            NativeProviderConnectionSheet(
                descriptor: descriptor,
                selectedSite: selectedRegionID,
                onSaved: {
                    showingAddConnection = false
                    reload()
                }
            )
        }
        .confirmationDialog(
            "Remove this connection?", isPresented: Binding(
                get: { pendingRemoval != nil },
                set: { if !$0 { pendingRemoval = nil } }
            ), titleVisibility: .visible
        ) {
            if let pendingRemoval {
                Button("Remove Connection", role: .destructive) {
                    remove(pendingRemoval)
                    self.pendingRemoval = nil
                }
            }
            Button("Cancel", role: .cancel) { pendingRemoval = nil }
        } message: {
            Text("Its app-managed credentials will be removed from Keychain. Usage history remains local.")
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
                    value: AppLocalization.text(
                        descriptor.supportsAccounts ? "Supported" : "Not declared"
                    )
                )
                HStack {
                    Button(
                        AppLocalization.text(
                            connections.isEmpty ? "Add Connection" : "Add Account"
                        ),
                        systemImage: "plus"
                    ) {
                        showingAddConnection = true
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
                            Text(AppLocalization.text(issue.message)).font(.callout.weight(.medium))
                            if let lastSuccessAt = issue.lastSuccessAt {
                                Text(
                                    "\(AppLocalization.text("Last successful update:")) "
                                        + DateText.display(lastSuccessAt)
                                )
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
                        Label(AppLocalization.text(group.title), systemImage: group.state.symbol)
                            .foregroundStyle(group.state.color)
                        Spacer()
                        Text(group.items.isEmpty
                            ? AppLocalization.text("None")
                            : group.items.map { AppLocalization.text($0.title) }
                                .joined(separator: ", "))
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
                    Label(AppLocalization.text(savedMessage), systemImage: "checkmark.circle.fill")
                        .font(.callout)
                        .foregroundStyle(.green)
                        .accessibilityLabel(AppLocalization.format("Success: %@", savedMessage))
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
                            HStack(spacing: 8) {
                                Button("Edit Connection", systemImage: "pencil") {
                                    beginEditing(connection)
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(.accentColor)
                                .controlSize(.small)
                                .disabled(isSaving)
                                Button("Remove", systemImage: "trash", role: .destructive) {
                                    pendingRemoval = connection
                                }
                                .controlSize(.small)
                                .disabled(isSaving)
                            }
                        } else {
                            Text("Read only")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Button(AppLocalization.text(
                            hiddenProviderIDs.contains(connection.providerID) ? "Show" : "Hide"
                        )) {
                            setHidden(
                                connection.providerID,
                                hidden: !hiddenProviderIDs.contains(connection.providerID)
                            )
                        }
                        .controlSize(.small)
                        .disabled(isSaving)
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
                            Text(AppLocalization.format(
                                "Observed source · %@", DateText.display(instance.observedAt)
                            ))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text("Read only")
                            .font(.caption).foregroundStyle(.secondary)
                        Button(AppLocalization.text(
                            hiddenProviderIDs.contains(instance.providerID) ? "Show" : "Hide"
                        )) {
                            setHidden(
                                instance.providerID,
                                hidden: !hiddenProviderIDs.contains(instance.providerID)
                            )
                        }
                        .controlSize(.small)
                        .disabled(isSaving)
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
                Text(AppLocalization.format("Edit %@", connection.displayName))
                    .font(.headline)
                Spacer()
                Text(AppLocalization.text(connection.isStepPlan
                    ? "Site remains locked to this connection"
                    : "Connection type remains unchanged"))
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
            Text(AppLocalization.text(connection.isStepPlan
                ? "Blank credential fields keep the existing values. China and International credentials cannot be moved between sites."
                : "Blank credential fields keep the existing Keychain value. Provider protocol and endpoint settings remain unchanged."))
                .font(.caption).foregroundStyle(.secondary)

            if let editError {
                Label(editError, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .accessibilityLabel(AppLocalization.format("Error: %@", editError))
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
        guard let draft = connection.managedDraft(
            name: accountName.trimmingCharacters(in: .whitespacesAndNewlines),
            replacementCredential: replacementAPIKey,
            replacementSession: replacementSession
        ) else {
            editError = AppLocalization.text(
                "Saved connection configuration is incomplete. Re-add this connection."
            )
            return
        }
        isSaving = true
        editError = nil
        Task { @MainActor in
            let result = await ProviderMutationService.submit(
                draft.request(action: .updateConnection), command: command
            )
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

    private func remove(_ connection: ProviderConnectionSummary) {
        guard let command = ProviderMutationCommand.resolve(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: Bundle.main.executableURL ?? Bundle.main.bundleURL
        ) else {
            editError = ProviderMutationFailure.unavailable.message
            return
        }
        let draft: ManagedConnectionDraft = switch connection.kind {
        case "step_plan": .stepPlan(
            providerID: connection.providerID, name: connection.displayName,
            site: connection.site ?? "china", replacementCredential: "",
            replacementSession: ""
        )
        case "openai_organization": .openAIOrganization(
            providerID: connection.providerID, name: connection.displayName,
            replacementCredential: ""
        )
        case "generic": .generic(.init(
            providerID: connection.providerID, name: connection.displayName,
            familyID: connection.familyID, endpoint: "", headerName: "",
            authPrefix: "", primaryPath: "", remainingPercentPath: nil,
            resetPath: nil, detailPath: nil, replacementCredential: ""
        ))
        case "daily_usage_feed": .dailyUsageFeed(.init(
            providerID: connection.providerID, name: connection.displayName,
            familyID: connection.familyID, endpoint: "", headerName: "",
            authPrefix: "", itemsPath: "", datePath: "", modelPath: "",
            inputTokensPath: "", outputTokensPath: "", cacheReadTokensPath: nil,
            cacheCreationTokensPath: nil, reasoningTokensPath: nil,
            totalTokensPath: "", sinceParameter: "", untilParameter: "",
            replacementCredential: ""
        ))
        default: .minimax(
            providerID: connection.providerID, name: connection.displayName,
            replacementCredential: ""
        )
        }
        isSaving = true
        editError = nil
        Task { @MainActor in
            let result = await ProviderMutationService.submit(
                draft.request(action: .removeConnection), command: command
            )
            isSaving = false
            switch result {
            case let .success(response) where response.ok:
                savedMessage = response.message
                cancelEditing()
                savedMessage = response.message
                reload()
            case let .success(response): editError = response.message
            case let .failure(failure): editError = failure.message
            }
        }
    }

    private func connectionMetadata(_ connection: ProviderConnectionSummary) -> String {
        let site = ProviderCenterText.region(connection.site ?? "Configured")
        guard let observed = instances.first(where: {
            $0.providerID == connection.providerID
        }) else { return "\(site) · \(AppLocalization.text("Not collected yet"))" }
        return "\(site) · \(DateText.display(observed.observedAt))"
    }

    private func setHidden(_ providerID: String, hidden: Bool) {
        isSaving = true
        editError = nil
        Task { @MainActor in
            let result: Result<Void, Error> = await Task.detached(priority: .utility) {
                do {
                    let store = VisibilityStore(url: ActivityPaths.visibility)
                    var values = try store.load().hiddenProviderIDs
                    if hidden { values.insert(providerID) } else { values.remove(providerID) }
                    try store.save(hiddenProviderIDs: values)
                    return .success(())
                } catch { return .failure(error) }
            }.value
            isSaving = false
            switch result {
            case .success: reload()
            case .failure:
                editError = AppLocalization.text("Provider visibility could not be updated.")
            }
        }
    }
}

private struct NativeProviderConnectionSheet: View {
    let descriptor: ProviderDisplayDescriptor
    let selectedSite: String?
    let onSaved: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var kind: String
    @State private var providerID: String
    @State private var name: String
    @State private var site: String
    @State private var endpoint = ""
    @State private var familyID: String
    @State private var headerName = "Authorization"
    @State private var authPrefix = "Bearer"
    @State private var primaryPath = "data.remaining"
    @State private var remainingPercentPath = ""
    @State private var resetPath = ""
    @State private var detailPath = ""
    @State private var itemsPath = "data.items"
    @State private var datePath = "date"
    @State private var modelPath = "model"
    @State private var inputTokensPath = "input_tokens"
    @State private var outputTokensPath = "output_tokens"
    @State private var totalTokensPath = "total_tokens"
    @State private var sinceParameter = "since"
    @State private var untilParameter = "until"
    @State private var credential = ""
    @State private var session = ""
    @State private var error: String?
    @State private var isSaving = false

    init(
        descriptor: ProviderDisplayDescriptor, selectedSite: String?,
        onSaved: @escaping () -> Void
    ) {
        self.descriptor = descriptor
        self.selectedSite = selectedSite
        self.onSaved = onSaved
        let initialKind = switch descriptor.familyID {
        case "minimax": "minimax"
        case "step_plan": "step_plan"
        case "openai": "openai_organization"
        default: "generic"
        }
        _kind = State(initialValue: initialKind)
        _providerID = State(initialValue: descriptor.familyID)
        _name = State(initialValue: descriptor.displayName)
        _site = State(initialValue: selectedSite == "cn" ? "china" : selectedSite ?? "china")
        _familyID = State(initialValue: descriptor.familyID)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Add Connection").font(.title2.weight(.semibold))
                    Text("Credentials are written to Keychain and never displayed again.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(24)
            Divider()
            Form {
                Picker("Connection type", selection: $kind) {
                    Text("Quota API").tag("generic")
                    Text("Daily Usage Feed").tag("daily_usage_feed")
                    if descriptor.familyID == "minimax" { Text("MiniMax").tag("minimax") }
                    if descriptor.familyID == "step_plan" { Text("Step Plan").tag("step_plan") }
                    if descriptor.familyID == "openai" { Text("OpenAI Organization").tag("openai_organization") }
                }
                TextField("Connection ID", text: $providerID)
                TextField("Account label", text: $name)
                if kind == "step_plan" {
                    Picker("Site", selection: $site) {
                        Text("China").tag("china")
                        Text("International").tag("international")
                    }
                }
                if ["generic", "daily_usage_feed"].contains(kind) {
                    TextField("Endpoint", text: $endpoint)
                    TextField("Header name", text: $headerName)
                    TextField("Authentication prefix", text: $authPrefix)
                }
                if kind == "generic" {
                    TextField("Primary field path", text: $primaryPath)
                    TextField("Remaining percent path (optional)", text: $remainingPercentPath)
                    TextField("Reset path (optional)", text: $resetPath)
                    TextField("Detail path (optional)", text: $detailPath)
                }
                if kind == "daily_usage_feed" {
                    TextField("Provider family ID", text: $familyID)
                    TextField("Items path", text: $itemsPath)
                    TextField("Date path", text: $datePath)
                    TextField("Model path", text: $modelPath)
                    TextField("Input tokens path", text: $inputTokensPath)
                    TextField("Output tokens path", text: $outputTokensPath)
                    TextField("Total tokens path", text: $totalTokensPath)
                    TextField("Since parameter", text: $sinceParameter)
                    TextField("Until parameter", text: $untilParameter)
                }
                SecureField(
                    AppLocalization.text(kind == "minimax" ? "Coding Plan key" : "API key"),
                    text: $credential
                )
                if kind == "step_plan" {
                    SecureField("Web session (optional)", text: $session)
                }
                if let error {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
            Divider()
            HStack {
                Spacer()
                Button("Cancel") { clearSecrets(); dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(isSaving)
                Button("Add Connection") { submit() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(isSaving)
            }
            .padding(18)
        }
        .frame(width: 620, height: 680)
        .onDisappear(perform: clearSecrets)
    }

    private func submit() {
        let draft = makeDraft()
        if let validation = draft.validation(action: .createConnection) {
            let message = switch validation {
            case .missingProviderID: "Connection ID is required."
            case .missingName: "Account label is required."
            case .missingCredential: "API key or web session is required."
            case .invalidSite: "Site is invalid."
            }
            error = AppLocalization.text(message)
            return
        }
        guard let command = ProviderMutationCommand.resolve(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: Bundle.main.executableURL ?? Bundle.main.bundleURL
        ) else {
            error = ProviderMutationFailure.unavailable.message
            return
        }
        isSaving = true
        error = nil
        Task { @MainActor in
            let result = await ProviderMutationService.submit(
                draft.request(action: .createConnection), command: command
            )
            isSaving = false
            switch result {
            case let .success(response) where response.ok:
                clearSecrets()
                onSaved()
            case let .success(response): error = response.message
            case let .failure(failure): error = failure.message
            }
        }
    }

    private func makeDraft() -> ManagedConnectionDraft {
        switch kind {
        case "minimax": .minimax(
            providerID: providerID, name: name, replacementCredential: credential
        )
        case "step_plan": .stepPlan(
            providerID: providerID, name: name, site: site,
            replacementCredential: credential, replacementSession: session
        )
        case "openai_organization": .openAIOrganization(
            providerID: providerID, name: name, replacementCredential: credential
        )
        case "daily_usage_feed": .dailyUsageFeed(.init(
            providerID: providerID, name: name, familyID: familyID,
            endpoint: endpoint, headerName: headerName, authPrefix: authPrefix,
            itemsPath: itemsPath, datePath: datePath, modelPath: modelPath,
            inputTokensPath: inputTokensPath, outputTokensPath: outputTokensPath,
            cacheReadTokensPath: nil, cacheCreationTokensPath: nil,
            reasoningTokensPath: nil, totalTokensPath: totalTokensPath,
            sinceParameter: sinceParameter, untilParameter: untilParameter,
            replacementCredential: credential
        ))
        default: .generic(.init(
            providerID: providerID, name: name, familyID: familyID,
            endpoint: endpoint, headerName: headerName, authPrefix: authPrefix,
            primaryPath: primaryPath,
            remainingPercentPath: emptyToNil(remainingPercentPath),
            resetPath: emptyToNil(resetPath), detailPath: emptyToNil(detailPath),
            replacementCredential: credential
        ))
        }
    }

    private func emptyToNil(_ value: String) -> String? { value.isEmpty ? nil : value }
    private func clearSecrets() { credential = ""; session = "" }
}

private struct ProviderDetailSection<Content: View>: View {
    let title: String
    let detail: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            Text(AppLocalization.text(title)).font(.headline)
            Text(AppLocalization.text(detail)).font(.callout).foregroundStyle(.secondary)
            content
        }
    }
}
