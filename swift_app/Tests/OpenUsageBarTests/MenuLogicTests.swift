import Foundation
import Testing
import UsageCore
@testable import OpenUsageBar

@Suite("B v3 menu logic")
struct MenuLogicTests {
    private func capacity(
        id: String, provider: String = "demo_provider", name: String = "weekly_window",
        ratio: Double? = 0.5, remaining: String? = "50", unit: String = "percent",
        reset: String? = nil, state: String = "ok", stale: Bool = false
    ) -> CapacityItem {
        CapacityItem(
            recordID: id, providerID: provider, accountRef: "acct", quotaName: name,
            unit: unit, used: nil, limit: nil, remaining: remaining,
            remainingRatio: ratio, resetsAt: reset, periodStart: nil, periodEnd: nil,
            observedAt: "2026-07-14T12:30:00Z", freshnessSeconds: 65,
            state: state, quality: "exact", stale: stale, revision: 8
        )
    }

    @Test("Hierarchy contains only approved top-level content")
    func hierarchy() {
        #expect(MenuCopy.topLevelSections == ["Today Token", "Capacity"])
        #expect(MenuCopy.footerActions == ["Open Usage Details", "Data Health", "Settings"])
        let visible = MenuCopy.allVisibleText.joined(separator: " ")
        for banned in ["1 at risk", "Attention", "Today cost", "active models", "sparkline"] {
            #expect(!visible.localizedCaseInsensitiveContains(banned))
        }
        #expect(!visible.contains("—"))
        #expect(!visible.contains("–"))
    }

    @Test("View all providers opens Provider Center instead of capacity")
    func allProvidersDestination() {
        #expect(MenuDestination.allProviders == .providersAndAccounts)
        #expect(MenuDestination.allProviders.transportValue == "providers")
    }

    @Test("Keyboard commands map to deterministic actions")
    func keyboard() {
        #expect(MenuKeyRouter.action(for: .commandRefresh) == .refresh)
        #expect(MenuKeyRouter.action(for: .commandDetails) == .openDetails)
        #expect(MenuKeyRouter.action(for: .commandSettings) == .openSettings)
        #expect(MenuKeyRouter.action(for: .escape, hasExpansion: true) == .collapse)
        #expect(MenuKeyRouter.action(for: .escape, hasExpansion: false) == .close)
        #expect(MenuKeyRouter.action(for: .down) == .moveSelection(1))
        #expect(MenuKeyRouter.action(for: .up) == .moveSelection(-1))
        #expect(MenuKeyRouter.action(for: .returnKey) == .activateSelection)
        #expect(MenuKeyRouter.action(for: .space) == .activateSelection)
    }

    @Test("Provider presentation includes capacity reset and freshness semantics")
    func providerPresentation() {
        let row = CapacityItem(
            recordID: "minimax.window", providerID: "minimax", accountRef: "acct",
            quotaName: "5-hour window", unit: "percent", used: "82", limit: "100",
            remaining: "18", remainingRatio: 0.18, resetsAt: "2026-07-14T14:48:00Z",
            periodStart: nil, periodEnd: nil, observedAt: "2026-07-14T12:30:00Z",
            freshnessSeconds: 1_080, state: "ok", quality: "derived", stale: true,
            revision: 8
        )
        let presentation = ProviderRowPresentation(row, now: ISO8601DateFormatter().date(from: "2026-07-14T12:48:00Z")!)
        #expect(presentation.capacity == "18%")
        #expect(presentation.reset.contains("resets"))
        #expect(presentation.freshness == "Stale, 18m old")
        #expect(presentation.accessibilityValue.contains("18%"))
        #expect(presentation.accessibilityValue.contains("Stale"))
        #expect(presentation.accessibilityValue.contains("Estimated"))
        #expect(presentation.accessibilityValue.contains("Critical"))
        #expect(presentation.visibleMetadata.contains(presentation.reset))
        #expect(presentation.visibleMetadata.contains(presentation.freshness!))
        #expect(presentation.stateSymbol != nil)
    }

    @Test("Secondary quota presentation retains visual and spoken risk semantics")
    func secondaryQuotaPresentation() {
        let row = CapacityItem(
            recordID: "minimax.monthly", providerID: "minimax", accountRef: "acct",
            quotaName: "monthly window", unit: "percent", used: "65", limit: "100",
            remaining: "35", remainingRatio: 0.35, resetsAt: "2026-07-14T13:30:00Z",
            periodStart: nil, periodEnd: nil, observedAt: "2026-07-14T12:30:00Z",
            freshnessSeconds: 1_080, state: "rate_limited", quality: "cached", stale: false,
            revision: 8
        )
        let presentation = ProviderRowPresentation(
            row, now: ISO8601DateFormatter().date(from: "2026-07-14T12:48:00Z")!
        )

        #expect(presentation.riskLevel == .warning)
        #expect(presentation.stateSymbol == "exclamationmark.triangle.fill")
        #expect(presentation.accessibilityLabel == "MiniMax, Monthly Window")
        for spoken in ["35% remaining", "resets in 42m", "Rate Limited", "Cached", "Warning capacity"] {
            #expect(presentation.accessibilityValue.contains(spoken))
        }
    }

    @Test("Provider rows cover native balances and unavailable states")
    func providerAlternates() {
        let balance = ProviderRowPresentation(capacity(id: "balance", ratio: nil, remaining: "12.50", unit: "USD", state: "permission_blocked"))
        #expect(balance.provider == "Demo Provider")
        #expect(balance.window == "Weekly Window")
        #expect(balance.capacity == "12.50 USD")
        #expect(balance.freshness == "Permission Blocked")
        #expect(balance.isWarning)

        let unavailable = ProviderRowPresentation(capacity(id: "none", ratio: nil, remaining: nil))
        #expect(unavailable.capacity == "Unavailable")
        #expect(unavailable.reset == "Reset unavailable")
    }

    @Test("Menu rows use the shared exact provider catalog")
    func menuProviderCatalog() {
        #expect(Format.provider("copilot") == ProviderCatalog.descriptor(for: "copilot").displayName)
        #expect(Format.provider("copilot") == "GitHub Copilot")
        #expect(Format.provider("minimaxevil") == "Minimaxevil")
        #expect(Format.provider("cursorless") == "Cursorless")
        #expect(Format.provider("not-codex") == "Not Codex")
    }

    @Test("Menu uses the explicit instance descriptor for visible and spoken provider names")
    func menuProviderInstance() {
        let descriptor = ProviderCatalog.descriptor(
            for: "minimax-main", familyID: "minimax",
            displayName: "MiniMax Main", category: .subscription
        )
        let row = CapacityItem(
            recordID: "minimax-main.window", providerID: "minimax-main", accountRef: "acct",
            quotaName: "5-hour", unit: "percent", used: "82", limit: "100",
            remaining: "18", remainingRatio: 0.18, resetsAt: nil,
            periodStart: nil, periodEnd: nil, observedAt: "2026-07-14T12:30:00Z",
            freshnessSeconds: 0, state: "ok", quality: "exact", stale: false,
            revision: 1, providerDescriptor: descriptor
        )
        let presentation = ProviderRowPresentation(row)
        #expect(presentation.provider == "MiniMax Main")
        #expect(presentation.providerDescriptor.category == .subscription)
        #expect(presentation.providerDescriptor.metricFamilies == descriptor.metricFamilies)
        #expect(presentation.accessibilityLabel == "MiniMax Main, 5 Hour")
    }

    @Test("Capacity groups retain secondary windows and urgent order")
    func capacityGroups() {
        let groups = ProviderCapacityGroup.make(from: [
            capacity(id: "slow", provider: "codex", ratio: 0.8),
            capacity(id: "urgent", provider: "minimax", ratio: 0.1),
            capacity(id: "secondary", provider: "minimax", ratio: 0.6),
        ])
        #expect(groups.map(\.primary.recordID) == ["urgent", "slow"])
        #expect(groups[0].secondary.map(\.recordID) == ["secondary"])
    }

    @Test("Fixed formatters cover token and reset boundaries")
    func formattingBoundaries() {
        #expect(Format.tokens(999) == "999")
        #expect(Format.tokens(1_250) == "1.2K")
        #expect(Format.tokens(74_200_000) == "74.2M")
        #expect(Format.tokens(2_000_000_000) == "2B")
        let now = ISO8601DateFormatter().date(from: "2026-07-14T12:00:00Z")!
        #expect(Format.reset("2026-07-14T12:30:00Z", now: now) == "resets in 30m")
        #expect(Format.reset("2026-07-16T12:00:00Z", now: now) == "resets in 2d")
        #expect(Format.reset("2026-07-14T12:00:00Z", now: now) == "Reset due")
        #expect(Format.reset("2026-07-14T11:59:59Z", now: now) == "Reset due")
        #expect(Format.reset("2026-07-14T12:00:59Z", now: now) == "resets in 1m")
        let due = ProviderRowPresentation(capacity(id: "due", reset: "2026-07-14T12:00:00Z"), now: now)
        #expect(due.visibleMetadata.contains("Reset due"))
        #expect(due.accessibilityValue.contains("Reset due"))
        #expect(Format.age(59) == "59s")
        #expect(Format.age(7_200) == "2h")
        #expect(Format.age(172_800) == "2d")
    }

    @Test("Revision gate reloads only changed ledgers")
    func revisionGate() {
        #expect(!RevisionGate.shouldReload(current: 42, observed: 42))
        #expect(RevisionGate.shouldReload(current: 42, observed: 43))
        #expect(RevisionGate.shouldReload(current: nil, observed: 0))
    }

    @Test("Initial loading is idempotent and later reads require a changed revision")
    func loadGate() {
        var gate = LoadGate()
        let first = gate.beginInitialLoad()
        let concurrent = gate.beginInitialLoad()
        #expect(first)
        #expect(!concurrent)
        gate.finish(revision: 42)
        let repeated = gate.beginInitialLoad()
        #expect(!repeated)
        #expect(!gate.shouldReload(observed: 42))
        #expect(gate.shouldReload(observed: 43))
    }

    @Test("Primary-only providers never enter expansion and Escape closes once")
    func expansionPolicy() {
        #expect(ExpansionPolicy.next(current: nil, selected: "codex", hasSecondary: false) == nil)
        #expect(ExpansionPolicy.next(current: nil, selected: "minimax", hasSecondary: true) == "minimax")
        #expect(MenuKeyRouter.action(for: .escape, hasExpansion: false) == .close)
    }

    @Test("Successful refresh clears only transient refresh errors")
    func refreshErrorPolicy() {
        #expect(RefreshErrorPolicy.next(current: "Refresh timed out. Showing last-good data.", result: .succeeded) == nil)
        #expect(RefreshErrorPolicy.next(current: nil, result: .failed) != nil)
    }

    @Test("Today Token distinguishes no data, zero, and observed partial totals")
    func missingToday() {
        #expect(Format.todayTokens(nil) == "No data")
        #expect(Format.todayTokens(0) == "0")
        #expect(TodayTokenPresentation(tokens: 4_513_614_786, isComplete: false).value == "4.5B")
        #expect(TodayTokenPresentation(tokens: 4_513_614_786, isComplete: false).coverage == "Partial")
        #expect(TodayTokenPresentation(tokens: 0, isComplete: true).coverage == nil)
    }

    @Test("Menu timestamps accept canonical fractional seconds")
    func fractionalTimestamp() {
        #expect(Format.timestamp("2026-07-16T13:51:08.060086Z") != nil)
        #expect(Format.timestamp("2026-07-16T13:51:08Z") != nil)
        #expect(Format.timestamp("not-a-timestamp") == nil)
    }

    @Test("Reset labels accept canonical fractional seconds")
    func fractionalResetTimestamp() {
        let now = try! #require(Format.timestamp("2026-07-16T14:00:00Z"))
        #expect(Format.reset("2026-07-16T16:00:00.000000Z", now: now) == "resets in 2h")
        #expect(Format.reset("2026-07-23T07:38:05.000000Z", now: now) == "resets in 6d")
        #expect(Format.reset(nil, now: now) == "Reset unavailable")
    }

    @Test("Refresh command is direct argv and carries no credential material")
    func refreshCommand() {
        let command = RefreshCommand.bundled(
            executable: URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"),
            ledger: URL(fileURLWithPath: "/Users/test/.local/state/openusage-bar/activity.sqlite3")
        )
        #expect(command.executable.path.hasSuffix("OpenUsage Provider Settings"))
        #expect(command.arguments == ["__refresh-once", "--ledger", "/Users/test/.local/state/openusage-bar/activity.sqlite3"])
        let joined = command.arguments.joined(separator: " ").lowercased()
        #expect(!joined.contains("token"))
        #expect(!joined.contains("cookie"))
        #expect(!joined.contains("key"))
        #expect(RefreshCommand.interactiveTimeout == 90)
        #expect(command.timeout == RefreshCommand.interactiveTimeout)
    }

    @Test("Process runner reports success without invoking a shell")
    func processSuccess() {
        let command = RefreshCommand(
            executable: URL(fileURLWithPath: "/usr/bin/true"), arguments: [], timeout: 1
        )
        #expect(RefreshRunner().run(command) == .succeeded)
    }

    @Test("Collector receives only allowlisted environment and standard file descriptors")
    func processBoundary() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let resultURL = directory.appendingPathComponent("probe.json")
        let inheritedURL = directory.appendingPathComponent("inherited.txt")
        FileManager.default.createFile(atPath: inheritedURL.path, contents: Data())
        let inheritedFD = Darwin.open(inheritedURL.path, O_RDONLY)
        #expect(inheritedFD >= 3)
        defer { if inheritedFD >= 0 { Darwin.close(inheritedFD) } }
        #expect(fcntl(inheritedFD, F_SETFD, 0) == 0)
        let sourceURL = directory.appendingPathComponent("probe.c")
        let executableURL = directory.appendingPathComponent("probe")
        try """
        #include <fcntl.h>
        #include <stdio.h>
        #include <unistd.h>
        extern char **environ;
        int main(int argc, char **argv) {
            if (argc != 2) return 2;
            int fds[32], count = 0;
            for (int fd = 0; fd < 32; fd++) if (fcntl(fd, F_GETFD) != -1) fds[count++] = fd;
            char byte; int stdin_eof = read(0, &byte, 1) == 0;
            FILE *file = fopen(argv[1], "w"); if (!file) return 3;
            for (char **entry = environ; *entry; entry++) fprintf(file, "E:%s\\n", *entry);
            for (int index = 0; index < count; index++) fprintf(file, "F:%d\\n", fds[index]);
            fprintf(file, "I:%d\\n", stdin_eof);
            return fclose(file) == 0 ? 0 : 4;
        }
        """
        .write(to: sourceURL, atomically: true, encoding: .utf8)
        let compiler = Process()
        compiler.executableURL = URL(fileURLWithPath: "/usr/bin/clang")
        compiler.arguments = [sourceURL.path, "-o", executableURL.path]
        compiler.standardOutput = FileHandle.nullDevice
        compiler.standardError = FileHandle.nullDevice
        try compiler.run()
        compiler.waitUntilExit()
        #expect(compiler.terminationStatus == 0)
        let environment = [
            "PATH": "/usr/bin", "HOME": "/Users/tester", "TMPDIR": "/tmp",
            "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8", "XDG_CONFIG_HOME": "/tmp/config",
            "OPENAI_API_KEY": "must-not-pass", "OASIS_TOKEN": "must-not-pass",
            "COOKIE": "must-not-pass", "ARBITRARY_PRIVATE_VALUE": "must-not-pass",
        ]
        let command = RefreshCommand(
            executable: executableURL, arguments: [resultURL.path], timeout: 2
        )

        #expect(RefreshRunner(environment: environment).run(command) == .succeeded)
        let lines = try String(contentsOf: resultURL, encoding: .utf8).split(separator: "\n").map(String.init)
        let childEnvironment = Dictionary(uniqueKeysWithValues: lines.compactMap { line -> (String, String)? in
            guard line.hasPrefix("E:"), let equals = line.firstIndex(of: "=") else { return nil }
            return (String(line[line.index(line.startIndex, offsetBy: 2)..<equals]), String(line[line.index(after: equals)...]))
        })
        #expect(childEnvironment["PATH"] == "/usr/bin")
        #expect(childEnvironment["HOME"] == "/Users/tester")
        #expect(childEnvironment["TMPDIR"] == "/tmp")
        #expect(childEnvironment["LANG"] == "en_US.UTF-8")
        #expect(childEnvironment["LC_CTYPE"] == "UTF-8")
        #expect(childEnvironment["XDG_CONFIG_HOME"] == "/tmp/config")
        #expect(childEnvironment["OPENAI_API_KEY"] == nil)
        #expect(childEnvironment["OASIS_TOKEN"] == nil)
        #expect(childEnvironment["COOKIE"] == nil)
        #expect(childEnvironment["ARBITRARY_PRIVATE_VALUE"] == nil)
        #expect(Set(childEnvironment.keys).isSubset(of: RefreshEnvironment.allowedKeys))
        #expect(lines.filter { $0.hasPrefix("F:") } == ["F:0", "F:1", "F:2"])
        #expect(lines.contains("I:1"))
    }

    @Test("Interrupted waitpid is retried")
    func interruptedWait() {
        #expect(WaitpidDecision.decide(result: -1, error: EINTR) == .retry)
        #expect(WaitpidDecision.decide(result: -1, error: ECHILD) == .failed)
    }

    @Test("Process runner terminates work at its time bound")
    func processTimeout() {
        let command = RefreshCommand(
            executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["5"], timeout: 0.05
        )
        let start = Date()
        #expect(RefreshRunner().run(command) == .timedOut)
        #expect(Date().timeIntervalSince(start) < 2)
    }

    @Test("Timeout terminates and reaps the collector process tree")
    func processTreeTimeout() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let sourceURL = directory.appendingPathComponent("parent.c")
        let executableURL = directory.appendingPathComponent("parent")
        let pidFile = directory.appendingPathComponent("child.pid")
        let ownershipMarker = directory.appendingPathComponent("owned-by-this-test")
        try """
        #include <signal.h>
        #include <stdio.h>
        #include <unistd.h>
        int main(int argc, char **argv) {
            if (argc != 3) return 2;
            signal(SIGTERM, SIG_IGN);
            pid_t child = fork();
            if (child < 0) return 3;
            if (child == 0) {
                signal(SIGTERM, SIG_IGN);
                while (1) pause();
            }
            FILE *file = fopen(argv[1], "w");
            if (!file) return 4;
            if (fprintf(file, "%d", child) < 0 || fflush(file) != 0 || fsync(fileno(file)) != 0 || fclose(file) != 0) return 5;
            while (1) pause();
        }
        """.write(to: sourceURL, atomically: true, encoding: .utf8)
        let compiler = Process()
        compiler.executableURL = URL(fileURLWithPath: "/usr/bin/clang")
        compiler.arguments = [sourceURL.path, "-o", executableURL.path]
        compiler.standardOutput = FileHandle.nullDevice
        compiler.standardError = FileHandle.nullDevice
        try compiler.run()
        compiler.waitUntilExit()
        #expect(compiler.terminationStatus == 0)

        // Compilation happens outside the deadline. The native fixture then has
        // enough scheduling room on a shared runner to publish its child PID.
        let command = RefreshCommand(executable: executableURL, arguments: [pidFile.path, ownershipMarker.path], timeout: 5)
        #expect(RefreshRunner().run(command) == .timedOut)
        let childPID = try #require(Int32(try String(contentsOf: pidFile, encoding: .utf8)))
        defer { cleanupOwnedTestProcess(childPID, marker: ownershipMarker.path) }
        #expect(Darwin.kill(childPID, 0) != 0)
    }

    @Test("Process runner reports a nonzero collector exit")
    func processFailure() {
        let command = RefreshCommand(
            executable: URL(fileURLWithPath: "/usr/bin/false"), arguments: [], timeout: 1
        )
        #expect(RefreshRunner().run(command) == .failed)
    }

    @Test("Embedded Python collector command remains direct argv")
    func pythonCollectorCommand() {
        let command = RefreshCommand.python(
            executable: URL(fileURLWithPath: "/App/Resources/python/bin/python3"),
            entrypoint: URL(fileURLWithPath: "/App/Resources/openusage_collector.py"),
            ledger: URL(fileURLWithPath: "/tmp/activity.sqlite3")
        )
        #expect(command.arguments == ["/App/Resources/openusage_collector.py", "__refresh-once", "--ledger", "/tmp/activity.sqlite3"])
    }

    private func cleanupOwnedTestProcess(_ pid: Int32, marker: String) {
        guard Darwin.kill(pid, 0) == 0 else { return }
        let output = Pipe()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", "\(pid)", "-o", "command="]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try? process.run()
        process.waitUntilExit()
        let command = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        if process.terminationStatus == 0, command.contains(marker) { Darwin.kill(pid, SIGKILL) }
    }
}
