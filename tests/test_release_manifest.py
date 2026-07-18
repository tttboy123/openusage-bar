import hashlib
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from scripts.generate_release_manifest import (
    ManifestError,
    _portable_swift_dependencies,
    generate,
)


COMMIT = "a" * 40
COMMIT_TIME = "2026-07-18T12:00:00+00:00"


class ReleaseManifestTests(unittest.TestCase):
    def test_swift_dependency_paths_outside_the_repository_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ManifestError):
                _portable_swift_dependencies(
                    {"path": "/Users/example/private/package"}, root
                )

    def test_manifest_and_spdx_are_deterministic_complete_and_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "OpenUsage Bar.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "0.4.0",
                "CFBundleVersion": "4",
                "LSMinimumSystemVersion": "15.0",
            }))
            executable = app / "Contents/MacOS/OpenUsage Bar"
            executable.write_bytes(b"synthetic executable")
            executable.chmod(0o755)
            archive = root / "OpenUsage-Bar-v0.4.0-macos-arm64.zip"
            archive.write_bytes(b"synthetic archive")
            checksum = Path(f"{archive}.sha256")
            checksum.write_text(
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
                encoding="ascii",
            )
            requirements = root / "requirements-build.txt"
            requirements.write_text(
                "zeta==2.0 --hash=sha256:" + "b" * 64 + "\n"
                "Alpha==1.0 --hash=sha256:" + "c" * 64 + "\n",
                encoding="utf-8",
            )
            manifest_path = root / "OpenUsage-Bar-v0.4.0-manifest.json"
            sbom_path = root / "OpenUsage-Bar-v0.4.0-sbom.spdx.json"
            arguments = dict(
                root=root, app=app, archive=archive, requirements=requirements,
                swift_dependencies={
                    "name": "OpenUsageBar",
                    "path": str(root / "swift_app"),
                    "url": str(root / "swift_app"),
                    "dependencies": [],
                },
                commit=COMMIT, commit_time=COMMIT_TIME,
            )

            generate(
                **arguments, manifest_path=manifest_path, sbom_path=sbom_path
            )
            first_manifest = manifest_path.read_bytes()
            first_sbom = sbom_path.read_bytes()
            generate(
                **arguments, manifest_path=manifest_path, sbom_path=sbom_path
            )

            self.assertEqual(first_manifest, manifest_path.read_bytes())
            self.assertEqual(first_sbom, sbom_path.read_bytes())
            manifest = json.loads(manifest_path.read_text("utf-8"))
            sbom = json.loads(sbom_path.read_text("utf-8"))
            self.assertEqual(manifest["schemaVersion"], 1)
            self.assertEqual(manifest["gitCommit"], COMMIT)
            self.assertEqual(manifest["swiftDependencies"]["path"], "swift_app")
            self.assertEqual(manifest["swiftDependencies"]["url"], "swift_app")
            self.assertNotIn(str(root), manifest_path.read_text("utf-8"))
            self.assertEqual(
                manifest["product"],
                {
                    "name": "OpenUsage Bar", "version": "0.4.0", "build": "4",
                    "minimumMacOS": "15.0", "architecture": "arm64",
                },
            )
            self.assertEqual(
                [row["name"] for row in manifest["pythonDependencies"]],
                ["Alpha", "zeta"],
            )
            self.assertEqual(manifest["executables"][0]["path"], "Contents/MacOS/OpenUsage Bar")
            self.assertEqual(len(manifest["publishedAssets"]), 3)
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(sbom["dataLicense"], "CC0-1.0")
            self.assertEqual(sbom["creationInfo"]["created"], COMMIT_TIME)
            self.assertEqual(len(sbom["packages"]), 3)


if __name__ == "__main__":
    unittest.main()
