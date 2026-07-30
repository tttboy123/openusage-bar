#!/usr/bin/env python3
"""Verify a complete OpenUsage Bar release candidate without exposing user data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPOSITORY = "tttboy123/openusage-bar"
SIGNER_WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml"
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MANIFEST_PATTERN = re.compile(
    r"^OpenUsage-Bar-v((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"-manifest\.json$"
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_CHECKSUM_BYTES = 512
MAX_ATTESTATION_BYTES = 1024 * 1024
ATTESTATION_TIMEOUT_SECONDS = 60
SAFE_REASONS = frozenset(
    {
        "argument",
        "asset",
        "assets_dir",
        "attestation",
        "checksum",
        "gh",
        "hash",
        "manifest",
        "sbom",
    }
)


class CandidateError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CandidateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CandidateError("argument")


def _read_json(path: Path, reason: str) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise CandidateError(reason)
        size = path.stat().st_size
        if size <= 0 or size > MAX_METADATA_BYTES:
            raise CandidateError(reason)
        return json.loads(path.read_text(encoding="utf-8"))
    except CandidateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CandidateError(reason) from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise CandidateError("asset") from error
    return digest.hexdigest()


def _expected_asset_names(version: str) -> frozenset[str]:
    prefix = f"OpenUsage-Bar-v{version}"
    return frozenset(
        {
            f"{prefix}-macos-arm64.dmg",
            f"{prefix}-macos-arm64.dmg.sha256",
            f"{prefix}-macos-arm64.zip",
            f"{prefix}-macos-arm64.zip.sha256",
            f"{prefix}-sbom.spdx.json",
        }
    )


def _load_manifest(
    assets_dir: Path,
    expected_version: str,
) -> tuple[Path, dict[str, Any], str, str]:
    manifests = [
        path
        for path in assets_dir.glob("OpenUsage-Bar-v*-manifest.json")
        if not path.is_symlink() and path.is_file()
    ]
    if len(manifests) != 1:
        raise CandidateError("manifest")
    manifest_path = manifests[0]
    match = MANIFEST_PATTERN.fullmatch(manifest_path.name)
    if match is None:
        raise CandidateError("manifest")
    version = match.group(1)
    if (
        VERSION_PATTERN.fullmatch(expected_version) is None
        or version != expected_version
    ):
        raise CandidateError("manifest")
    manifest = _read_json(manifest_path, "manifest")
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise CandidateError("manifest")

    product = manifest.get("product")
    if not isinstance(product, dict) or (
        product.get("name") != "OpenUsage Bar"
        or product.get("version") != version
        or product.get("architecture") != "arm64"
        or not isinstance(product.get("build"), str)
        or not product["build"].isdigit()
        or int(product["build"]) <= 0
    ):
        raise CandidateError("manifest")
    commit = manifest.get("gitCommit")
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise CandidateError("manifest")
    return manifest_path, manifest, version, commit


def _verify_assets(
    assets_dir: Path,
    manifest: dict[str, Any],
    version: str,
) -> tuple[Path, ...]:
    expected_names = _expected_asset_names(version)
    published = manifest.get("publishedAssets")
    if not isinstance(published, list) or len(published) != len(expected_names):
        raise CandidateError("manifest")

    rows: dict[str, dict[str, Any]] = {}
    for row in published:
        if not isinstance(row, dict) or set(row) != {"name", "sha256", "size"}:
            raise CandidateError("manifest")
        name = row.get("name")
        sha256 = row.get("sha256")
        size = row.get("size")
        if (
            not isinstance(name, str)
            or name in rows
            or name not in expected_names
            or not isinstance(sha256, str)
            or SHA_PATTERN.fullmatch(sha256) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise CandidateError("manifest")
        rows[name] = row
    if set(rows) != expected_names:
        raise CandidateError("manifest")

    verified: list[Path] = []
    for name in sorted(expected_names):
        path = assets_dir / name
        if path.is_symlink() or not path.is_file():
            raise CandidateError("asset")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise CandidateError("asset") from error
        row = rows[name]
        if size != row["size"]:
            raise CandidateError("hash")
        if _sha256(path) != row["sha256"]:
            raise CandidateError("hash")
        verified.append(path)
    return tuple(verified)


def _verify_checksums(assets: tuple[Path, ...], version: str) -> None:
    prefix = f"OpenUsage-Bar-v{version}-macos-arm64"
    by_name = {path.name: path for path in assets}
    for suffix in ("dmg", "zip"):
        artifact_name = f"{prefix}.{suffix}"
        checksum_name = f"{artifact_name}.sha256"
        artifact = by_name[artifact_name]
        checksum = by_name[checksum_name]
        try:
            if checksum.stat().st_size > MAX_CHECKSUM_BYTES:
                raise CandidateError("checksum")
            content = checksum.read_text(encoding="ascii")
        except CandidateError:
            raise
        except (OSError, UnicodeError) as error:
            raise CandidateError("checksum") from error
        if content != f"{_sha256(artifact)}  {artifact_name}\n":
            raise CandidateError("checksum")


def _verify_sbom(assets_dir: Path, version: str, commit: str) -> None:
    sbom = _read_json(
        assets_dir / f"OpenUsage-Bar-v{version}-sbom.spdx.json",
        "sbom",
    )
    if not isinstance(sbom, dict) or (
        sbom.get("spdxVersion") != "SPDX-2.3"
        or sbom.get("dataLicense") != "CC0-1.0"
        or sbom.get("name") != f"OpenUsage-Bar-{version}"
        or sbom.get("documentNamespace")
        != f"https://github.com/{REPOSITORY}/releases/{version}/{commit}"
    ):
        raise CandidateError("sbom")
    packages = sbom.get("packages")
    if not isinstance(packages, list) or not any(
        isinstance(package, dict)
        and package.get("SPDXID") == "SPDXRef-Package-OpenUsage-Bar"
        and package.get("name") == "OpenUsage Bar"
        and package.get("versionInfo") == version
        for package in packages
    ):
        raise CandidateError("sbom")


def _verify_attestations(
    assets: tuple[Path, ...],
    version: str,
    commit: str,
) -> None:
    gh = shutil.which("gh")
    if gh is None:
        raise CandidateError("gh")
    for path in assets:
        try:
            result = subprocess.run(
                [
                    gh,
                    "attestation",
                    "verify",
                    str(path),
                    "--repo",
                    REPOSITORY,
                    "--signer-workflow",
                    SIGNER_WORKFLOW,
                    "--source-digest",
                    commit,
                    "--source-ref",
                    f"refs/tags/v{version}",
                    "--deny-self-hosted-runners",
                    "--format",
                    "json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=ATTESTATION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CandidateError("attestation") from error
        if (
            result.returncode != 0
            or len(result.stdout) <= 0
            or len(result.stdout) > MAX_ATTESTATION_BYTES
        ):
            raise CandidateError("attestation")
        try:
            payload = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CandidateError("attestation") from error
        if not isinstance(payload, list) or not payload:
            raise CandidateError("attestation")


def verify_candidate(assets_dir: Path, expected_version: str) -> tuple[str, int]:
    assets_dir = assets_dir.expanduser()
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise CandidateError("assets_dir")
    manifest_path, manifest, version, commit = _load_manifest(
        assets_dir,
        expected_version,
    )
    published_assets = _verify_assets(assets_dir, manifest, version)
    _verify_checksums(published_assets, version)
    _verify_sbom(assets_dir, version, commit)
    attested_assets = tuple(sorted((*published_assets, manifest_path)))
    _verify_attestations(attested_assets, version, commit)
    return version, len(attested_assets)


def _parser() -> argparse.ArgumentParser:
    parser = CandidateArgumentParser(add_help=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    try:
        namespace = _parser().parse_args(arguments)
        version, asset_count = verify_candidate(
            namespace.assets_dir,
            namespace.version,
        )
    except CandidateError as error:
        reason = error.reason if error.reason in SAFE_REASONS else "argument"
        print(f"canary_candidate_invalid reason={reason}", file=sys.stderr)
        return 1
    except SystemExit as error:
        return int(error.code)
    except (OSError, ValueError):
        print("canary_candidate_invalid reason=argument", file=sys.stderr)
        return 1
    print(
        f"canary_candidate_verified version={version} "
        f"assets={asset_count} attestations={asset_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
