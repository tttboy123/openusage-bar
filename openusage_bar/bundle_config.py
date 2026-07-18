from __future__ import annotations


APP_NAME = "OpenUsage Provider Settings"
APP_BUNDLE_PATH = "/Applications/OpenUsage Bar.app"
BUNDLE_ID = "com.lune.openusagebar.settings"
LAUNCH_AGENT_LABEL = "com.lune.openusagebar.collector"
APP_VERSION = "0.4.2"
BUILD_VERSION = "6"


def info_plist() -> dict[str, object]:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": BUILD_VERSION,
        "LSMinimumSystemVersion": "15.0",
        "NSHighResolutionCapable": True,
    }


def launch_agent_payload(stdout_path: str, stderr_path: str) -> dict[str, object]:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            f"{APP_BUNDLE_PATH}/Contents/Helpers/{APP_NAME}.app/Contents/MacOS/{APP_NAME}",
            "daemon", "--interval", "300", "--api-socket",
            "~/.local/state/openusage-bar/openusage.sock",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
    }
