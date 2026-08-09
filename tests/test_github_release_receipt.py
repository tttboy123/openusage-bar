from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/github_release_receipt.py"

SCHEMA_VERSION = "github-release-receipt/v1"
REPOSITORY = "tttboy123/openusage-bar"
TAG = "v1.2.3"
SOURCE_SHA = "a" * 40
PUBLISHED_AT = "2026-08-09T12:34:56Z"

ASSET_PAYLOADS = {
    "OpenUsage-Bar-v1.2.3-sbom.spdx.json": b'{"spdxVersion":"SPDX-2.3"}\n',
    "OpenUsage-Bar-v1.2.3-macos-arm64.dmg": b"dmg-payload-v1\n",
    "OpenUsage-Bar-v1.2.3-manifest.json": b'{"manifest":"public-v1"}\n',
    "OpenUsage-Bar-v1.2.3-macos-arm64.zip.sha256": b"zip-digest-line-v1\n",
    "OpenUsage-Bar-v1.2.3-macos-arm64.zip": b"zip-payload-v1\n",
    "OpenUsage-Bar-v1.2.3-macos-arm64.dmg.sha256": b"dmg-digest-line-v1\n",
}

EXPECTED_MANIFEST_SHA256 = (
    "47d8161e0915ad3c877181a6fdefb169196693f03cb8d5dde5464e035d24ca64"
)
EXPECTED_ASSET_SET_SHA256 = (
    "ecf39f951219dbb107ffc1caf7e9d2de0e526fbadaf9dff0746d9daff794578d"
)
CHANGED_MANIFEST_SHA256 = (
    "9a9a635d9acea2d74d405f1d60db78634bb46c037339fcb8c7d782511673a88c"
)
CHANGED_ASSET_SET_SHA256 = (
    "7d5d02e0fc2be8f3067da7c483aeeebf9f20b33c97052d33f1c9e66310aec862"
)

EXPECTED_RECEIPT = {
    "assetSetSha256": EXPECTED_ASSET_SET_SHA256,
    "manifestSha256": EXPECTED_MANIFEST_SHA256,
    "prerelease": True,
    "provenanceAttestation": "verified",
    "publishedAt": PUBLISHED_AT,
    "releaseId": "987654321",
    "repository": REPOSITORY,
    "schemaVersion": SCHEMA_VERSION,
    "sourceSha": SOURCE_SHA,
    "tag": TAG,
}


def _remote_asset(name: str, payload: bytes) -> dict[str, object]:
    return {
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "name": name,
        "privateAssetField": f"PRIVATE_ASSET_FIELD:{name}",
        "size": len(payload),
    }


class ReleaseReceiptFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_dir = root / "release-candidate-PRIVATE_PATH_MARKER"
        self.assets_dir.mkdir()
        for name, payload in ASSET_PAYLOADS.items():
            (self.assets_dir / name).write_bytes(payload)

        self.release_json = root / "github-release-PRIVATE_PATH_MARKER.json"
        self.output = root / "receipt-PRIVATE_PATH_MARKER.json"
        self.release: dict[str, object] = {
            "assets": [
                _remote_asset(name, payload)
                for name, payload in ASSET_PAYLOADS.items()
            ],
            "databaseId": 987654321,
            "isPrerelease": True,
            "privateReleaseField": f"PRIVATE_RELEASE_FIELD:{root}",
            "publishedAt": PUBLISHED_AT,
            "tagName": TAG,
        }
        self.write_release()

    def write_release(self, payload: dict[str, object] | None = None) -> None:
        if payload is not None:
            self.release = payload
        self.release_json.write_text(
            json.dumps(self.release, ensure_ascii=True),
            encoding="utf-8",
        )

    def update_asset(self, name: str, payload: bytes) -> None:
        (self.assets_dir / name).write_bytes(payload)
        assets = self.release["assets"]
        if not isinstance(assets, list):
            raise AssertionError("fixture assets must remain a list")
        for asset in assets:
            if isinstance(asset, dict) and asset.get("name") == name:
                asset["digest"] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
                asset["size"] = len(payload)
                self.write_release()
                return
        raise AssertionError(f"fixture asset is missing: {name}")

    def run(
        self,
        *,
        repository: str = REPOSITORY,
        tag: str = TAG,
        source_sha: str = SOURCE_SHA,
        output: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--schema-version",
                SCHEMA_VERSION,
                "--release-json",
                str(self.release_json),
                "--assets-dir",
                str(self.assets_dir),
                "--repository",
                repository,
                "--tag",
                tag,
                "--source-sha",
                source_sha,
                "--output",
                str(self.output if output is None else output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


class GitHubReleaseReceiptAcceptanceTests(unittest.TestCase):
    def assert_invalid(
        self,
        result: subprocess.CompletedProcess[str],
        fixture: ReleaseReceiptFixture,
        *,
        output_may_exist: bool = False,
    ) -> None:
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "github_release_receipt_invalid\n")
        transcript = result.stdout + result.stderr
        self.assertNotIn(str(fixture.root), transcript)
        self.assertNotIn("PRIVATE_", transcript)
        self.assertNotIn("Traceback", transcript)
        if not output_may_exist:
            self.assertFalse(fixture.output.exists())

    def test_six_matching_assets_generate_exact_canonical_closed_receipt(self):
        with tempfile.TemporaryDirectory(prefix="receipt-private-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))

            result = fixture.run()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "github_release_receipt_ok assets=6\n")
            self.assertEqual(result.stderr, "")
            receipt = json.loads(fixture.output.read_text(encoding="utf-8"))
            self.assertEqual(receipt, EXPECTED_RECEIPT)
            self.assertEqual(receipt["manifestSha256"], EXPECTED_MANIFEST_SHA256)
            self.assertEqual(receipt["assetSetSha256"], EXPECTED_ASSET_SET_SHA256)
            self.assertEqual(
                fixture.output.read_text(encoding="utf-8"),
                json.dumps(
                    EXPECTED_RECEIPT,
                    allow_nan=False,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            public_surface = result.stdout + result.stderr + json.dumps(receipt)
            self.assertNotIn(str(fixture.root), public_surface)
            self.assertNotIn("PRIVATE_", public_surface)

    def test_asset_set_digest_is_stable_across_remote_asset_order(self):
        receipts: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="receipt-order-") as directory:
            root = Path(directory)
            for index, reverse in enumerate((False, True)):
                fixture = ReleaseReceiptFixture(root / str(index))
                assets = fixture.release["assets"]
                if not isinstance(assets, list):
                    raise AssertionError("fixture assets must remain a list")
                if reverse:
                    assets.reverse()
                    fixture.write_release()

                result = fixture.run()

                self.assertEqual(result.returncode, 0, result.stderr)
                receipts.append(json.loads(fixture.output.read_text(encoding="utf-8")))

        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(receipts[0]["assetSetSha256"], EXPECTED_ASSET_SET_SHA256)

    def test_manifest_digest_is_bound_to_verified_manifest_bytes(self):
        manifest_name = "OpenUsage-Bar-v1.2.3-manifest.json"
        with tempfile.TemporaryDirectory(prefix="receipt-manifest-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))
            fixture.update_asset(manifest_name, b'{"manifest":"public-v2"}\n')

            result = fixture.run()

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(fixture.output.read_text(encoding="utf-8"))
            self.assertEqual(receipt["manifestSha256"], CHANGED_MANIFEST_SHA256)
            self.assertEqual(receipt["assetSetSha256"], CHANGED_ASSET_SET_SHA256)
            self.assertNotEqual(receipt["manifestSha256"], EXPECTED_MANIFEST_SHA256)

    def test_remote_asset_set_rejects_unknown_missing_and_duplicate_names(self):
        for case in ("unknown", "missing", "duplicate"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(prefix=f"receipt-{case}-") as directory:
                    fixture = ReleaseReceiptFixture(Path(directory))
                    payload = copy.deepcopy(fixture.release)
                    assets = payload["assets"]
                    if not isinstance(assets, list):
                        raise AssertionError("fixture assets must remain a list")
                    if case == "unknown":
                        assets[0]["name"] = "unknown-PRIVATE_REMOTE_ASSET.bin"
                    elif case == "missing":
                        assets.pop()
                    else:
                        assets.append(copy.deepcopy(assets[0]))
                    fixture.write_release(payload)

                    result = fixture.run()

                    self.assert_invalid(result, fixture)

    def test_remote_digest_and_size_drift_are_rejected(self):
        for case in ("digest", "size"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(prefix=f"receipt-{case}-") as directory:
                    fixture = ReleaseReceiptFixture(Path(directory))
                    payload = copy.deepcopy(fixture.release)
                    assets = payload["assets"]
                    if not isinstance(assets, list):
                        raise AssertionError("fixture assets must remain a list")
                    if case == "digest":
                        assets[0]["digest"] = "sha256:" + "b" * 64
                    else:
                        assets[0]["size"] += 1
                    fixture.write_release(payload)

                    result = fixture.run()

                    self.assert_invalid(result, fixture)

    def test_release_must_be_matching_prerelease_with_utc_publication_time(self):
        for case in ("not-prerelease", "remote-tag", "non-utc-time"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory(prefix=f"receipt-{case}-") as directory:
                    fixture = ReleaseReceiptFixture(Path(directory))
                    payload = copy.deepcopy(fixture.release)
                    if case == "not-prerelease":
                        payload["isPrerelease"] = False
                    elif case == "remote-tag":
                        payload["tagName"] = "v1.2.4"
                    else:
                        payload["publishedAt"] = "2026-08-09T12:34:56+00:00"
                    fixture.write_release(payload)

                    result = fixture.run()

                    self.assert_invalid(result, fixture)

    def test_repository_tag_and_source_arguments_are_strict(self):
        cases = (
            {"repository": "private-owner/openusage-bar"},
            {"tag": "v1.2.4"},
            {"source_sha": "A" * 40},
            {"source_sha": "a" * 39},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory(prefix="receipt-identity-") as directory:
                    fixture = ReleaseReceiptFixture(Path(directory))

                    result = fixture.run(**arguments)

                    self.assert_invalid(result, fixture)

    def test_local_asset_set_rejects_symlink_and_extra_asset(self):
        with tempfile.TemporaryDirectory(prefix="receipt-symlink-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))
            asset = fixture.assets_dir / "OpenUsage-Bar-v1.2.3-macos-arm64.dmg"
            target = fixture.root / "PRIVATE_SYMLINK_TARGET.dmg"
            target.write_bytes(asset.read_bytes())
            asset.unlink()
            asset.symlink_to(target)

            result = fixture.run()

            self.assert_invalid(result, fixture)

        with tempfile.TemporaryDirectory(prefix="receipt-extra-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))
            (fixture.assets_dir / "extra-PRIVATE_LOCAL_ASSET.txt").write_text(
                "private local material",
                encoding="utf-8",
            )

            result = fixture.run()

            self.assert_invalid(result, fixture)

    def test_existing_output_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="receipt-existing-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))
            original = b"PRIVATE_EXISTING_OUTPUT_MUST_SURVIVE\n"
            fixture.output.write_bytes(original)

            result = fixture.run()

            self.assert_invalid(result, fixture, output_may_exist=True)
            self.assertEqual(fixture.output.read_bytes(), original)

    def test_failure_does_not_echo_private_fields_or_paths(self):
        with tempfile.TemporaryDirectory(prefix="PRIVATE_RECEIPT_PATH-") as directory:
            fixture = ReleaseReceiptFixture(Path(directory))
            payload = copy.deepcopy(fixture.release)
            payload["privateReleaseField"] = "PRIVATE_REMOTE_SECRET_VALUE"
            payload["assets"] = "PRIVATE_REMOTE_ASSET_PAYLOAD"
            fixture.write_release(payload)

            result = fixture.run()

            self.assert_invalid(result, fixture)
            transcript = result.stdout + result.stderr
            self.assertNotIn("PRIVATE_REMOTE_SECRET_VALUE", transcript)
            self.assertNotIn("PRIVATE_REMOTE_ASSET_PAYLOAD", transcript)


if __name__ == "__main__":
    unittest.main()
