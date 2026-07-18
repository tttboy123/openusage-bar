import Darwin
import Foundation

public struct RefreshCommand: Sendable, Hashable {
    public static let interactiveTimeout: TimeInterval = 120

    public let executable: URL
    public let arguments: [String]
    public let timeout: TimeInterval

    public static func bundled(
        executable: URL,
        ledger: URL,
        timeout: TimeInterval = RefreshCommand.interactiveTimeout
    ) -> Self {
        Self(executable: executable, arguments: ["__refresh-once", "--ledger", ledger.path], timeout: timeout)
    }

    static func python(
        executable: URL,
        entrypoint: URL,
        ledger: URL,
        timeout: TimeInterval = RefreshCommand.interactiveTimeout
    ) -> Self {
        Self(executable: executable, arguments: [entrypoint.path, "__refresh-once", "--ledger", ledger.path], timeout: timeout)
    }
}

enum RefreshResult: Sendable, Hashable { case succeeded, failed, timedOut }

enum RefreshEnvironment {
    static let allowedKeys: Set<String> = [
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
        "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_COLLATE", "LC_MONETARY",
        "LC_NUMERIC", "LC_TIME", "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_STATE_HOME",
    ]

    static func sanitized(_ environment: [String: String]) -> [String: String] {
        environment.filter { allowedKeys.contains($0.key) }
    }
}

enum WaitpidDecision: Sendable, Hashable {
    case retry
    case failed

    static func decide(result: pid_t, error: Int32) -> Self {
        result == -1 && error == EINTR ? .retry : .failed
    }
}

struct RefreshRunner: Sendable {
    private let environment: [String: String]

    init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.environment = RefreshEnvironment.sanitized(environment)
    }

    func run(_ command: RefreshCommand) -> RefreshResult {
        guard let pid = spawn(command) else { return .failed }
        let deadline = Date().addingTimeInterval(command.timeout)
        if let status = wait(pid, until: deadline) { return exitedSuccessfully(status) ? .succeeded : .failed }

        // POSIX_SPAWN_SETPGROUP with group zero makes the child's PID its PGID.
        // Verify that invariant before signaling so the host process group can never be targeted.
        guard getpgid(pid) == pid else {
            Darwin.kill(pid, SIGKILL)
            _ = wait(pid, until: Date().addingTimeInterval(1))
            return .failed
        }
        Darwin.kill(-pid, SIGTERM)
        let parentStatus = wait(pid, until: Date().addingTimeInterval(1))
        if processGroupExists(pid) {
            Darwin.kill(-pid, SIGKILL)
        }
        if parentStatus == nil { _ = wait(pid, until: Date().addingTimeInterval(1)) }
        let groupDeadline = Date().addingTimeInterval(1)
        while processGroupExists(pid) && Date() < groupDeadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        return .timedOut
    }

    private func spawn(_ command: RefreshCommand) -> pid_t? {
        var attributes: posix_spawnattr_t?
        guard posix_spawnattr_init(&attributes) == 0 else { return nil }
        defer { posix_spawnattr_destroy(&attributes) }
        let flags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)
        guard posix_spawnattr_setflags(&attributes, flags) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0
        else { return nil }

        var actions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&actions) == 0 else { return nil }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0
        else { return nil }

        let arguments = [command.executable.path] + command.arguments
        let environment = environment.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
        return withCStringArray(arguments) { argv in
            withCStringArray(environment) { envp in
                var pid = pid_t()
                let result = posix_spawn(&pid, command.executable.path, &actions, &attributes, argv, envp)
                return result == 0 ? pid : nil
            }
        }
    }

    private func wait(_ pid: pid_t, until deadline: Date) -> Int32? {
        var status = Int32()
        while Date() < deadline {
            let result = waitpid(pid, &status, WNOHANG)
            if result == pid { return status }
            if result == -1 {
                let decision = WaitpidDecision.decide(result: result, error: errno)
                if decision == .retry { continue }
                return nil
            }
            Thread.sleep(forTimeInterval: 0.02)
        }
        return nil
    }

    private func exitedSuccessfully(_ status: Int32) -> Bool {
        (status & 0x7f) == 0 && ((status >> 8) & 0xff) == 0
    }

    private func processGroupExists(_ pid: pid_t) -> Bool {
        Darwin.kill(-pid, 0) == 0
    }

    private func withCStringArray<T>(_ values: [String], body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> T) -> T {
        let strings = values.map { value in value.withCString { strdup($0) } }
        defer { strings.forEach { free($0) } }
        var pointers: [UnsafeMutablePointer<CChar>?] = strings
        pointers.append(nil)
        return pointers.withUnsafeMutableBufferPointer { body($0.baseAddress!) }
    }
}

enum InstalledPaths {
    static let ledger = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state/openusage-bar/activity.sqlite3")
    static let visibility = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/openusage-bar/visibility.json")

    static func refreshCommand(bundle: Bundle = .main) -> RefreshCommand? {
        let root = bundle.bundleURL
        let helper = root.appendingPathComponent(
            "Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
        )
        if FileManager.default.isExecutableFile(atPath: helper.path) { return .bundled(executable: helper, ledger: ledger) }
        let python = root.appendingPathComponent("Contents/Resources/python/bin/python3")
        let script = root.appendingPathComponent("Contents/Resources/openusage_collector.py")
        guard FileManager.default.isExecutableFile(atPath: python.path), FileManager.default.fileExists(atPath: script.path) else { return nil }
        return .python(executable: python, entrypoint: script, ledger: ledger)
    }
}
