import SwiftUI
import UsageCore

private enum AutomationViewState: Sendable {
    case loading
    case loaded(AutomationLoadedState)
    case failed(AutomationFailureState)
}

struct AutomationPage: View {
    let reader: any LocalAPIReading
    @State private var state = AutomationViewState.loading

    init(
        reader: any LocalAPIReading = LocalAPIClient()
    ) {
        self.reader = reader
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Automation").font(.largeTitle.weight(.semibold))
                    Text("Read-only local facts for schedulers and developer tools")
                        .font(.callout).foregroundStyle(.secondary)
                }
                switch state {
                case .loading:
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("Reading local API")
                    }
                    .frame(maxWidth: .infinity, minHeight: 180)
                case let .failed(failure): failureView(failure)
                case let .loaded(loaded): loadedView(loaded)
                }
            }
            .frame(maxWidth: 820, alignment: .leading)
            .padding(28)
        }
        .task { await load() }
    }

    @ViewBuilder private func loadedView(_ loaded: AutomationLoadedState) -> some View {
        AutomationSection(
            title: "Local API",
            detail: "This surface shows read-only local facts from the private Observer service."
        ) {
            Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 12) {
                row("State", loaded.health.ok ? "Available" : "Unavailable")
                row("Schema version", loaded.schema.schemaVersion)
                row("Data revision", String(loaded.snapshot.dataRevision))
                row("Generated", loaded.snapshot.generatedAt)
            }
        }
        AutomationSection(
            title: "Sanitized snapshot",
            detail: "The preview contains aggregate scheduler facts only. Account identities and credentials are omitted."
        ) {
            ScrollView(.horizontal) {
                Text(verbatim: loaded.preview)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(14)
            }
            .frame(maxHeight: 280)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10))
        }
    }

    private func failureView(_ failure: AutomationFailureState) -> some View {
        let copy: (String, String) = switch failure {
        case .unavailable: (
            "Local API unavailable",
            "The private Observer service is not available."
        )
        case .timedOut: ("Local API timed out", "The local API did not respond within three seconds.")
        case .schemaMismatch: (
            "Schema version mismatch",
            "This client supports local API schema version 1.0."
        )
        case .responseTooLarge: (
            "Local API response too large",
            "The response exceeded the one MiB safety limit."
        )
        case .invalidResponse: (
            "Local API response invalid",
            "The local endpoint returned malformed or unsupported data."
        )
        }
        return ContentUnavailableView(
            AppLocalization.text(copy.0), systemImage: "terminal",
            description: Text(AppLocalization.text(copy.1))
        )
        .frame(maxWidth: .infinity, minHeight: 300)
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(AppLocalization.text(label)).foregroundStyle(.secondary)
            Text(verbatim: value).textSelection(.enabled)
        }
    }

    private func load() async {
        do {
            let health = try await reader.health()
            let schema = try await reader.schema()
            let snapshot = try await reader.snapshot(localDay: nil)
            state = .loaded(.init(
                health: health, schema: schema, snapshot: snapshot,
                preview: AutomationPresentation.snapshotPreview(snapshot)
            ))
        } catch let error as LocalAPIClientError {
            state = .failed(AutomationPresentation.failure(error))
        } catch {
            state = .failed(.invalidResponse)
        }
    }
}

private struct AutomationSection<Content: View>: View {
    let title: String
    let detail: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppLocalization.text(title)).font(.headline)
            Text(AppLocalization.text(detail)).font(.callout).foregroundStyle(.secondary)
            content
        }
        .padding(18)
        .background(.background, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.separator))
    }
}
