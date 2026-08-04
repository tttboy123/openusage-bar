import io
import plistlib
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

from scripts.release_artifact_audit import (
    ArtifactError,
    inspect_members,
    main,
    verify_executable_names,
    verify_versions,
)


ROOT_NAME = "OpenUsage-Bar-v0.4.0-macos-arm64"


def archive_with(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries:
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"../../../../outside")
            else:
                archive.writestr(name, value)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


class ReleaseArtifactAuditTests(unittest.TestCase):
    def test_cli_reports_only_a_safe_rule_identifier(self):
        error = io.StringIO()
        with redirect_stderr(error):
            result = main(["missing.zip"])

        self.assertEqual(result, 1)
        self.assertEqual(error.getvalue(), "release_artifact_invalid reason=archive\n")

    def test_home_path_error_carries_only_the_validated_member_basename(self):
        archive = archive_with(((
            f"{ROOT_NAME}/release-quick-start.md", "/Users/example/private"
        ),))

        with self.assertRaises(ArtifactError) as raised:
            inspect_members(archive, ROOT_NAME)

        self.assertEqual(raised.exception.reason, "home_path")
        self.assertEqual(raised.exception.member, "release-quick-start.md")

    def test_documented_members_are_accepted(self):
        archive = archive_with((
            (f"{ROOT_NAME}/LICENSE", "license"),
            (f"{ROOT_NAME}/canary.md", "canary"),
            (f"{ROOT_NAME}/release-quick-start.md", "docs"),
            (
                f"{ROOT_NAME}/scripts/export_diagnostics.py",
                'DENIED_HOME_PREFIXES = ("/Users/", "/home/")',
            ),
            (
                f"{ROOT_NAME}/scripts/verify_canary_surfaces.py",
                'REPORT_SCHEMA = "openusage-canary-surfaces-1"',
            ),
            (
                f"{ROOT_NAME}/scripts/verify_canary_candidate.py",
                'REPOSITORY = "tttboy123/openusage-bar"',
            ),
            (f"{ROOT_NAME}/scripts/activity_schema.py", "EXPECTED_SCHEMA = {}"),
            (f"{ROOT_NAME}/scripts/install_app.sh", "#!/bin/zsh"),
            (f"{ROOT_NAME}/dist/OpenUsage Bar.app/Contents/Info.plist", "plist"),
        ))
        inspect_members(archive, ROOT_NAME)

    def test_traversal_absolute_duplicate_and_unexpected_members_fail(self):
        cases = (
            [(f"{ROOT_NAME}/../private", "x")],
            [("/absolute", "x")],
            [(f"{ROOT_NAME}/notes.txt", "x")],
            [(f"{ROOT_NAME}/LICENSE", "x"), (f"{ROOT_NAME}/LICENSE", "y")],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(ArtifactError):
                    inspect_members(archive_with(entries), ROOT_NAME)

    def test_symlink_private_material_and_home_paths_fail(self):
        link = zipfile.ZipInfo(f"{ROOT_NAME}/dist/OpenUsage Bar.app/Contents/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = (
            [(link.filename, link)],
            [(f"{ROOT_NAME}/dist/OpenUsage Bar.app/activity.sqlite3", "x")],
            [(f"{ROOT_NAME}/release-quick-start.md", "/Users/example/private")],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(ArtifactError):
                    inspect_members(archive_with(entries), ROOT_NAME)

    def test_three_bundle_versions_must_match_archive_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "dist/OpenUsage Bar.app"
            paths = (
                app / "Contents/Info.plist",
                app / "Contents/Helpers/OpenUsage Activity.app/Contents/Info.plist",
                app / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist",
            )
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(plistlib.dumps({
                    "CFBundleShortVersionString": "0.4.1" if index == 2 else "0.4.0",
                    "CFBundleVersion": "4",
                }))
            with self.assertRaises(ArtifactError):
                verify_versions(root, "0.4.0")

    def test_executable_names_must_match_declared_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "dist/OpenUsage Bar.app"
            bundles = (
                app,
                app / "Contents/Helpers/OpenUsage Activity.app",
                app / "Contents/Helpers/OpenUsage Provider Settings.app",
            )
            for bundle in bundles:
                info = bundle / "Contents/Info.plist"
                info.parent.mkdir(parents=True, exist_ok=True)
                info.write_bytes(plistlib.dumps({
                    "CFBundleShortVersionString": "0.4.0",
                    "CFBundleVersion": "4",
                    "CFBundleExecutable": "OpenUsage",
                }))
                executable = bundle / "Contents/MacOS/OpenUsage"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"\xcf\xfa\xed\xfe")
                executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            verify_executable_names(root)

            mismatch = (
                app
                / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist"
            )
            mismatch.write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "0.4.0",
                "CFBundleVersion": "4",
                "CFBundleExecutable": "Missing Launcher",
            }))
            with self.assertRaises(ArtifactError):
                verify_executable_names(root)


if __name__ == "__main__":
    unittest.main()
