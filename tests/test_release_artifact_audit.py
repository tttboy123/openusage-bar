import io
import plistlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release_artifact_audit import ArtifactError, inspect_members, verify_versions


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
    def test_documented_members_are_accepted(self):
        archive = archive_with((
            (f"{ROOT_NAME}/LICENSE", "license"),
            (f"{ROOT_NAME}/release-quick-start.md", "docs"),
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


if __name__ == "__main__":
    unittest.main()
