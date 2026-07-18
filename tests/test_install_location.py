import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCATION = ROOT / "scripts/install_location.sh"


class InstallLocationTests(unittest.TestCase):
    def test_install_update_rollback_and_uninstall_share_the_same_resolver(self) -> None:
        for name in ("install_app.sh", "rollback_app.sh", "uninstall_app.sh"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn('source "$ROOT/scripts/install_location.sh"', source)
            self.assertIn("resolve_openusage_install_dir", source)

    def resolve(self, *, home: Path, system: Path, override: Path | None = None) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "OPENUSAGE_SYSTEM_APPLICATIONS_DIR": str(system),
            }
        )
        if override is not None:
            environment["OPENUSAGE_INSTALL_DIR"] = str(override)
        else:
            environment.pop("OPENUSAGE_INSTALL_DIR", None)
        result = subprocess.run(
            [
                "/bin/zsh",
                "-c",
                f'source "{LOCATION}"; resolve_openusage_install_dir',
            ],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_explicit_install_directory_always_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            override = root / "Custom Apps"
            self.assertEqual(
                self.resolve(home=root / "home", system=root / "Applications", override=override),
                str(override),
            )

    def test_default_prefers_system_applications_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            system = root / "Applications"
            system.mkdir()
            self.assertEqual(
                self.resolve(home=root / "home", system=system),
                str(system),
            )

    def test_default_falls_back_to_user_applications_without_system_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            self.assertEqual(
                self.resolve(home=home, system=root / "missing-system-applications"),
                str(home / "Applications"),
            )

    def test_existing_user_install_remains_the_update_and_uninstall_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            system = root / "Applications"
            system.mkdir()
            (home / "Applications/OpenUsage Bar.app").mkdir(parents=True)
            self.assertEqual(
                self.resolve(home=home, system=system),
                str(home / "Applications"),
            )

    def test_reveal_passes_the_installed_app_to_finder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Applications/OpenUsage Bar.app"
            app.mkdir(parents=True)
            log = root / "open.log"
            fake_open = root / "open-spy"
            fake_open.write_text(
                '#!/bin/zsh\nprint -r -- "$@" > "$OPENUSAGE_OPEN_LOG"\n',
                encoding="utf-8",
            )
            fake_open.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "OPENUSAGE_OPEN_COMMAND": str(fake_open),
                    "OPENUSAGE_OPEN_LOG": str(log),
                }
            )
            result = subprocess.run(
                [
                    "/bin/zsh",
                    "-c",
                    f'source "{LOCATION}"; reveal_openusage_install "{app}"',
                ],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").strip(), f"-R {app}")

    def test_reveal_can_be_disabled_for_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "unexpected-open"
            fake_open = root / "open-spy"
            fake_open.write_text(
                f'#!/bin/zsh\ntouch "{marker}"\n', encoding="utf-8"
            )
            fake_open.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "OPENUSAGE_OPEN_COMMAND": str(fake_open),
                    "OPENUSAGE_REVEAL_IN_FINDER": "0",
                }
            )
            result = subprocess.run(
                [
                    "/bin/zsh",
                    "-c",
                    f'source "{LOCATION}"; reveal_openusage_install "/tmp/OpenUsage Bar.app"',
                ],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
