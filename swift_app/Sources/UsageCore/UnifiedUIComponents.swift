import SwiftUI

// MARK: - Cross-platform design tokens used by both MenuBar and Activity surfaces.
// These mirror the web CSS tokens in web/src/styles/tokens.css so the three
// clients (web, Swift App, Desktop/Electron) share the same visual language:
// soft rounded cards, 1px hairline borders, subtle shadows, and accent green.

public enum ProviderBrandColor {
    public static func color(for familyID: String) -> Color {
        switch familyID.lowercased() {
        case "deepseek": Color(hex: 0x4D6BFE)
        case "moonshot": Color(hex: 0x1A1A1A)
        case "minimax": Color(hex: 0xFF6B6B)
        case "openai", "openai_organization": Color(hex: 0x10A37F)
        case "anthropic": Color(hex: 0xD97757)
        case "google", "gemini_api", "gemini_cli": Color(hex: 0x4285F4)
        case "grok", "xai": Color(hex: 0x000000)
        case "opencode": Color(hex: 0x0066FF)
        case "openclaw": Color(hex: 0x7C3AED)
        case "hermes": Color(hex: 0x0EA5E9)
        default: DesignTokens.accent
        }
    }
}

public struct UnifiedProviderAvatar: View {
    let familyID: String
    let displayName: String
    let size: CGFloat
    let cornerRadius: CGFloat

    public init(familyID: String, displayName: String, size: CGFloat = 40, cornerRadius: CGFloat = 10) {
        self.familyID = familyID
        self.displayName = displayName
        self.size = size
        self.cornerRadius = cornerRadius
    }

    private var initials: String {
        let cleaned = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return "?" }
        let words = cleaned.split(separator: " ", omittingEmptySubsequences: true)
        let firstLetters = words.prefix(2).compactMap { $0.first?.uppercased() }
        return firstLetters.joined().prefix(2).map(String.init).joined()
    }

    public var body: some View {
        Text(initials)
            .font(.system(size: size * 0.38, weight: .semibold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(ProviderBrandColor.color(for: familyID), in: RoundedRectangle(cornerRadius: cornerRadius))
            .accessibilityHidden(true)
    }
}

public struct UnifiedStatusBadge: View {
    public enum Status: Sendable, Hashable {
        case ok, warning, critical, neutral
    }

    let status: Status
    let title: String
    let isLive: Bool

    public init(status: Status, title: String, isLive: Bool = false) {
        self.status = status
        self.title = title
        self.isLive = isLive
    }

    private var color: Color {
        switch status {
        case .ok: DesignTokens.accent
        case .warning: DesignTokens.warn
        case .critical: DesignTokens.bad
        case .neutral: Color.secondary
        }
    }

    public var body: some View {
        HStack(spacing: 5) {
            if isLive && status == .ok {
                LivePulseDot(color: color)
            } else {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
            }
            Text(title)
                .font(.caption.weight(.medium))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(color.opacity(0.12), in: Capsule())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(title)
    }
}

private struct LivePulseDot: View {
    let color: Color
    @State private var phase = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 6, height: 6)
            .opacity(reduceMotion ? 1 : (phase ? 0.45 : 1))
            .scaleEffect(reduceMotion ? 1 : (phase ? 0.7 : 1))
            .animation(reduceMotion ? nil : .easeInOut(duration: 1.6).repeatForever(autoreverses: true), value: phase)
            .onAppear { phase = true }
    }
}

public struct UnifiedEmptyState: View {
    let symbol: String
    let title: String
    let bodyText: String
    let actionTitle: String
    let action: () -> Void

    public init(
        symbol: String,
        title: String,
        body: String,
        actionTitle: String,
        action: @escaping () -> Void
    ) {
        self.symbol = symbol
        self.title = title
        self.bodyText = body
        self.actionTitle = actionTitle
        self.action = action
    }

    public var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 32, weight: .light))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.body.weight(.semibold))
            Text(bodyText)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(3)
            Button(actionTitle, action: action)
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
        }
        .padding(24)
        .frame(maxWidth: .infinity)
    }
}

public struct UnifiedRefreshButton: View {
    let isRefreshing: Bool
    let action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init(isRefreshing: Bool, action: @escaping () -> Void) {
        self.isRefreshing = isRefreshing
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: "arrow.clockwise")
                    .imageScale(.small)
                    .rotationEffect(rotationAngle)
                    .animation(reduceMotion ? nil : rotationAnimation, value: isRefreshing)
                Text(isRefreshing ? AppLocalization.text("Refreshing") : AppLocalization.text("Refresh"))
                    .font(.caption)
            }
        }
        .buttonStyle(.borderless)
        .disabled(isRefreshing)
    }

    private var rotationAngle: Angle {
        isRefreshing ? .degrees(360) : .zero
    }

    private var rotationAnimation: Animation {
        .linear(duration: 1).repeatForever(autoreverses: false)
    }
}

public struct UnifiedCardHoverActions: View {
    let consoleURL: URL?
    let credentialURL: URL?

    public init(consoleURL: URL? = nil, credentialURL: URL? = nil) {
        self.consoleURL = consoleURL
        self.credentialURL = credentialURL
    }

    public var body: some View {
        HStack(spacing: 6) {
            if let consoleURL {
                Link(destination: consoleURL) {
                    Image(systemName: "arrow.up.forward.square")
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                }
                .help(AppLocalization.text("Open Console"))
            }
            if let credentialURL {
                Link(destination: credentialURL) {
                    Image(systemName: "key")
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                }
                .help(AppLocalization.text("Get API Key"))
            }
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
        .background(.thinMaterial, in: Capsule())
    }
}

public struct UnifiedProviderCard: View {
    let familyID: String
    let displayName: String
    let subtitle: String
    let status: UnifiedStatusBadge.Status
    let statusTitle: String
    let consoleURL: URL?
    let credentialURL: URL?
    let action: () -> Void

    public init(
        familyID: String,
        displayName: String,
        subtitle: String,
        status: UnifiedStatusBadge.Status,
        statusTitle: String,
        consoleURL: URL? = nil,
        credentialURL: URL? = nil,
        action: @escaping () -> Void = {}
    ) {
        self.familyID = familyID
        self.displayName = displayName
        self.subtitle = subtitle
        self.status = status
        self.statusTitle = statusTitle
        self.consoleURL = consoleURL
        self.credentialURL = credentialURL
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                UnifiedProviderAvatar(familyID: familyID, displayName: displayName)
                VStack(alignment: .leading, spacing: 2) {
                    Text(displayName)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                UnifiedStatusBadge(status: status, title: statusTitle)
                UnifiedCardHoverActions(consoleURL: consoleURL, credentialURL: credentialURL)
            }
            .contentShape(Rectangle())
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .buttonStyle(UnifiedProviderCardStyle())
    }
}

public struct UnifiedProviderCardStyle: ButtonStyle {
    @Environment(\.colorScheme) private var colorScheme

    public func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(backgroundColor, in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .stroke(borderColor(for: configuration), lineWidth: 1)
            }
            .shadow(color: .black.opacity(colorScheme == .dark ? 0.28 : 0.06), radius: 1, y: 1)
            .scaleEffect(configuration.isPressed ? 0.995 : 1)
    }

    private func borderColor(for configuration: Configuration) -> Color {
        configuration.isPressed ? DesignTokens.accent.opacity(0.5) : Color(nsColor: .separatorColor).opacity(0.45)
    }

    private var backgroundColor: Color {
        colorScheme == .dark ? Color(nsColor: .controlBackgroundColor) : DesignTokens.surface
    }
}

public struct UnifiedSegmentedControl<Value: Hashable>: View {
    let options: [(value: Value, title: String)]
    @Binding var selection: Value

    public init(options: [(value: Value, title: String)], selection: Binding<Value>) {
        self.options = options
        self._selection = selection
    }

    public var body: some View {
        HStack(spacing: 2) {
            ForEach(options, id: \.value) { option in
                Button(option.title) {
                    selection = option.value
                }
                .buttonStyle(UnifiedSegmentedButtonStyle(isSelected: selection == option.value))
            }
        }
        .padding(3)
        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
    }
}

public struct UnifiedSegmentedButtonStyle: ButtonStyle {
    let isSelected: Bool

    public func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(isSelected ? .semibold : .regular))
            .foregroundStyle(isSelected ? DesignTokens.accent : Color.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(isSelected ? DesignTokens.accent.opacity(0.14) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
    }
}
