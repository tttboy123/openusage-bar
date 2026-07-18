import Foundation

/// The non-secret process context that bundled helper processes may inherit.
public enum ChildProcessEnvironment {
    public static let allowedKeys: Set<String> = [
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
        "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_COLLATE", "LC_MONETARY",
        "LC_NUMERIC", "LC_TIME", "TZ", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_STATE_HOME",
    ]

    public static func sanitized(_ environment: [String: String]) -> [String: String] {
        environment.filter { allowedKeys.contains($0.key) }
    }
}
