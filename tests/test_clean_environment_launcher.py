import re
import subprocess
import tempfile
import textwrap
import unittest
import sys
from pathlib import Path

from openusage_bar.openusage_adapter import _CHILD_ENVIRONMENT_KEYS


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = ROOT / "scripts/clean_env_launcher.c"
SHARED_ALLOWED_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_COLLATE",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
}
MACOS_RUNTIME_ALLOWED_KEYS = {"__CF_USER_TEXT_ENCODING", "XPC_SERVICE_NAME"}


PROBE_SOURCE = r"""
#include <stdio.h>
#include <string.h>

extern char **environ;

int main(int argc, char **argv) {
    for (char **entry = environ; *entry != NULL; entry++) {
        const char *separator = strchr(*entry, '=');
        if (separator == NULL) {
            return 70;
        }
        printf("ENV:%.*s\n", (int)(separator - *entry), *entry);
    }
    if (argc > 1) {
        printf("ARG:%s\n", argv[1]);
    }
    return 0;
}
"""


@unittest.skipUnless(sys.platform == "darwin", "macOS environment launcher test")
class CleanEnvironmentLauncherTests(unittest.TestCase):
    maxDiff = None

    def _compile(self, source: Path, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "/usr/bin/clang",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-mmacosx-version-min=15.0",
                str(source),
                "-o",
                str(output),
            ],
            check=True,
            capture_output=True,
        )

    def _build_role(self, root: Path, role: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        contents = root / "OpenUsage Bar.app/Contents"
        if role == "status":
            launcher = contents / "MacOS/OpenUsage Bar"
            target = contents / "MacOS/OpenUsage Bar.runtime"
        elif role == "collector":
            launcher = contents / "MacOS/OpenUsage Collector"
            target = (
                contents
                / "Helpers/OpenUsage Provider Settings.app/Contents/MacOS"
                / "OpenUsage Provider Settings"
            )
        else:
            raise AssertionError("unknown test role")

        probe_source = root / "probe.c"
        probe_source.write_text(textwrap.dedent(PROBE_SOURCE), encoding="utf-8")
        self._compile(LAUNCHER_SOURCE, launcher)
        self._compile(probe_source, target)
        return launcher

    def _polluted_environment(self, root: Path) -> dict[str, str]:
        environment = {
            key: f"safe-{index}"
            for index, key in enumerate(
                sorted(SHARED_ALLOWED_KEYS | MACOS_RUNTIME_ALLOWED_KEYS)
            )
        }
        environment.update({
            "HOME": str(root / "home"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(root / "tmp"),
            "OPENAI_API_KEY": "synthetic-openai-secret",
            "DEEPSEEK_API_KEY": "synthetic-deepseek-secret",
            "AWS_SECRET_ACCESS_KEY": "synthetic-aws-secret",
            "PYTHONPATH": "/tmp/untrusted-python-path",
            "SSH_AUTH_SOCK": "/tmp/untrusted-agent.sock",
            "SECRET_SENTINEL": "synthetic-sentinel-secret",
        })
        return environment

    def test_shared_allowlist_matches_swift_and_python_child_contracts(self):
        swift_source = (
            ROOT / "swift_app/Sources/UsageCore/ChildProcessEnvironment.swift"
        ).read_text(encoding="utf-8")
        declared = set(
            re.findall(
                r'"([A-Z_]+)"',
                swift_source.split("allowedKeys", 1)[1].split("]", 1)[0],
            )
        )

        self.assertEqual(declared, SHARED_ALLOWED_KEYS)
        self.assertEqual(set(_CHILD_ENVIRONMENT_KEYS), SHARED_ALLOWED_KEYS)

    def test_status_and_collector_targets_receive_only_allowlisted_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = self._polluted_environment(root)

            for role in ("status", "collector"):
                with self.subTest(role=role):
                    launcher = self._build_role(root / role, role)
                    result = subprocess.run(
                        [str(launcher), "--probe"],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    names = {
                        line.removeprefix("ENV:")
                        for line in result.stdout.splitlines()
                        if line.startswith("ENV:")
                    }
                    self.assertEqual(
                        names, SHARED_ALLOWED_KEYS | MACOS_RUNTIME_ALLOWED_KEYS
                    )
                    self.assertIn("ARG:--probe", result.stdout)
                    for value in environment.values():
                        self.assertNotIn(value, result.stdout)
                        self.assertNotIn(value, result.stderr)

    def test_unknown_executable_name_fails_closed_without_echoing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "Unexpected Launcher"
            self._compile(LAUNCHER_SOURCE, launcher)
            environment = self._polluted_environment(root)

            result = subprocess.run(
                [str(launcher)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            for value in environment.values():
                self.assertNotIn(value, result.stderr)


if __name__ == "__main__":
    unittest.main()
