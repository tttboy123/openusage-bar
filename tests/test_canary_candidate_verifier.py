from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_canary_candidate.py"
VERSION = "0.7.0"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "tttboy123/openusage-bar"
WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml"


def _load_verifier_module():
    spec = importlib.util.spec_from_file_location(
        "verify_canary_candidate",
        SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("verifier module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CanaryCandidateVerifierTests(unittest.TestCase):
    if sys.platform == "win32":
        __unittest_skip__ = True
        __unittest_skip_why__ = "macOS release canary verifier test"
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.gh_log = self.root / "gh.log"
        self._write_candidate()
        self._write_gh_spy()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _asset_names(self) -> tuple[str, ...]:
        prefix = f"OpenUsage-Bar-v{VERSION}"
        return (
            f"{prefix}-macos-arm64.dmg",
            f"{prefix}-macos-arm64.dmg.sha256",
            f"{prefix}-macos-arm64.zip",
            f"{prefix}-macos-arm64.zip.sha256",
            f"{prefix}-sbom.spdx.json",
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_candidate(self) -> None:
        dmg = self.assets / f"OpenUsage-Bar-v{VERSION}-macos-arm64.dmg"
        archive = self.assets / f"OpenUsage-Bar-v{VERSION}-macos-arm64.zip"
        dmg.write_bytes(b"fake-dmg-for-contract-tests")
        archive.write_bytes(b"fake-zip-for-contract-tests")
        for artifact in (dmg, archive):
            checksum = Path(f"{artifact}.sha256")
            checksum.write_text(
                f"{self._sha256(artifact)}  {artifact.name}\n",
                encoding="utf-8",
            )

        sbom = self.assets / f"OpenUsage-Bar-v{VERSION}-sbom.spdx.json"
        sbom.write_text(
            json.dumps(
                {
                    "spdxVersion": "SPDX-2.3",
                    "dataLicense": "CC0-1.0",
                    "name": f"OpenUsage-Bar-{VERSION}",
                    "documentNamespace": (
                        "https://github.com/tttboy123/openusage-bar/releases/"
                        f"{VERSION}/{COMMIT}"
                    ),
                    "packages": [
                        {
                            "SPDXID": "SPDXRef-Package-OpenUsage-Bar",
                            "name": "OpenUsage Bar",
                            "versionInfo": VERSION,
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        published_assets = []
        for name in sorted(self._asset_names()):
            path = self.assets / name
            published_assets.append(
                {
                    "name": name,
                    "sha256": self._sha256(path),
                    "size": path.stat().st_size,
                }
            )
        manifest = {
            "schemaVersion": 1,
            "gitCommit": COMMIT,
            "product": {
                "architecture": "arm64",
                "build": "10",
                "minimumMacOS": "15.0",
                "name": "OpenUsage Bar",
                "version": VERSION,
            },
            "publishedAssets": published_assets,
        }
        (self.assets / f"OpenUsage-Bar-v{VERSION}-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_gh_spy(self, *, exit_code: int = 0, output: str = "[{}]\n") -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        spy = bin_dir / "gh"
        spy.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$OPENUSAGE_GH_LOG"\n'
            f"printf '%s' '{output}'\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        spy.chmod(0o755)

    def _run(self, *, version: str = VERSION) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PATH"] = f"{self.root / 'bin'}:{environment.get('PATH', '')}"
        environment["OPENUSAGE_GH_LOG"] = str(self.gh_log)
        return subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(SCRIPT),
                "--assets-dir",
                str(self.assets),
                "--version",
                version,
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_valid_candidate_verifies_every_local_asset_and_attestation(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "canary_candidate_verified version=0.7.0 assets=6 attestations=6\n",
        )
        invocations = self.gh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(invocations), 6)
        expected_names = {
            f"OpenUsage-Bar-v{VERSION}-macos-arm64.dmg",
            f"OpenUsage-Bar-v{VERSION}-macos-arm64.dmg.sha256",
            f"OpenUsage-Bar-v{VERSION}-macos-arm64.zip",
            f"OpenUsage-Bar-v{VERSION}-macos-arm64.zip.sha256",
            f"OpenUsage-Bar-v{VERSION}-manifest.json",
            f"OpenUsage-Bar-v{VERSION}-sbom.spdx.json",
        }
        self.assertEqual(
            {Path(arguments.split()[2]).name for arguments in invocations},
            expected_names,
        )
        for arguments in invocations:
            self.assertIn(f"--repo {REPOSITORY}", arguments)
            self.assertIn(f"--signer-workflow {WORKFLOW}", arguments)
            self.assertIn(f"--source-digest {COMMIT}", arguments)
            self.assertIn(f"--source-ref refs/tags/v{VERSION}", arguments)
            self.assertIn("--deny-self-hosted-runners", arguments)
            self.assertIn("--format json", arguments)

    def test_hash_mismatch_fails_before_any_attestation_lookup(self) -> None:
        target = self.assets / f"OpenUsage-Bar-v{VERSION}-macos-arm64.zip"
        target.write_bytes(b"tampered")

        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "canary_candidate_invalid reason=hash\n")
        self.assertFalse(self.gh_log.exists())
        self.assertNotIn(str(self.root), result.stderr)

    def test_expected_version_mismatch_fails_before_any_attestation_lookup(self) -> None:
        result = self._run(version="0.6.1")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "canary_candidate_invalid reason=manifest\n")
        self.assertFalse(self.gh_log.exists())

    def test_missing_or_malformed_sbom_fails_closed(self) -> None:
        sbom = self.assets / f"OpenUsage-Bar-v{VERSION}-sbom.spdx.json"
        sbom.write_text('{"spdxVersion":"SPDX-2.2"}\n', encoding="utf-8")
        manifest_path = self.assets / f"OpenUsage-Bar-v{VERSION}-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        row = next(
            item for item in manifest["publishedAssets"] if item["name"] == sbom.name
        )
        row["sha256"] = self._sha256(sbom)
        row["size"] = sbom.stat().st_size
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "canary_candidate_invalid reason=sbom\n")
        self.assertFalse(self.gh_log.exists())

    def test_symlinked_asset_is_rejected_without_reading_its_target(self) -> None:
        target = self.assets / f"OpenUsage-Bar-v{VERSION}-macos-arm64.dmg"
        external = self.root / "outside"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)

        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "canary_candidate_invalid reason=asset\n")
        self.assertFalse(self.gh_log.exists())

    def test_attestation_failure_and_invalid_output_fail_sanitized(self) -> None:
        for exit_code, output in ((1, ""), (0, "{}\n"), (0, "not-json\n")):
            with self.subTest(exit_code=exit_code, output=output):
                self._write_gh_spy(exit_code=exit_code, output=output)
                result = self._run()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    result.stderr,
                    "canary_candidate_invalid reason=attestation\n",
                )
                self.assertNotIn(str(self.root), result.stderr)

    def test_oversized_checksum_is_rejected_before_its_contents_are_read(self) -> None:
        module = _load_verifier_module()
        checksum = self.assets / (
            f"OpenUsage-Bar-v{VERSION}-macos-arm64.zip.sha256"
        )
        checksum.write_bytes(b"x" * 513)
        assets = tuple(self.assets / name for name in self._asset_names())

        original_read_text = Path.read_text

        def guarded_read_text(path: Path, *args, **kwargs):
            if path == checksum:
                raise AssertionError("oversized checksum content was read")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded_read_text):
            with self.assertRaises(module.CandidateError) as caught:
                module._verify_checksums(assets, VERSION)

        self.assertEqual(caught.exception.reason, "checksum")

    def test_argument_errors_are_sanitized_without_usage_or_paths(self) -> None:
        result = subprocess.run(
            [
                os.environ.get("PYTHON", "python3"),
                str(SCRIPT),
                "--assets-dir",
                str(self.assets),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "canary_candidate_invalid reason=argument\n",
        )
        self.assertEqual(result.stdout, "")
        self.assertNotIn(str(self.root), result.stderr)
        self.assertNotIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
