#!/usr/bin/env python3
"""Verify the canonical, renderer-safe identity embedded in built artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable


CANONICAL_IDENTITY = Path(
    "openusage_bar/resources/artifact-build-identity.v1.json"
)
PRODUCT_TRUTH = Path("openusage_bar/resources/product-version-truth.v1.json")
PACKAGED_IDENTITY_NAME = "product-build-identity.v1.json"
REPOSITORY_COPIES = (
    Path("web/public") / PACKAGED_IDENTITY_NAME,
    Path("swift_app/Resources") / PACKAGED_IDENTITY_NAME,
)
MAX_IDENTITY_BYTES = 32 * 1024
MAX_TRUTH_BYTES = 256 * 1024
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
BUILD = re.compile(r"[1-9][0-9]*")
IDENTITY_FIELDS = (
    "candidateBuild",
    "candidateVersion",
    "canaryClock",
    "canaryQualifiedMachines",
    "canaryTargetMachines",
    "channel",
    "displayName",
    "publicationStatus",
    "publishedBaselineTag",
    "publishedBaselineVersion",
    "releaseEligible",
    "releaseStage",
    "schemaVersion",
)


class ArtifactBuildIdentityError(ValueError):
    """One intentionally path-free build-identity contract failure."""


def _fail() -> None:
    raise ArtifactBuildIdentityError()


def canonical_identity_bytes(payload: dict[str, Any]) -> bytes:
    """Return the only accepted on-disk representation of an identity."""

    try:
        return (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail()


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _read_regular(path: Path, *, maximum: int) -> bytes:
    descriptor: int | None = None
    try:
        linked = path.lstat()
        if (
            stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or linked.st_size <= 0
            or linked.st_size > maximum
        ):
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            _fail()
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                _fail()
        after = os.fstat(descriptor)
        final_link = path.lstat()
        # Compare descriptor snapshots with each other. Windows can expose
        # different timestamp semantics for a path stat and an open handle.
        if (
            size != linked.st_size
            or not _same_file(opened, after)
            or stat.S_ISLNK(final_link.st_mode)
            or not stat.S_ISREG(final_link.st_mode)
            or (linked.st_dev, linked.st_ino)
            != (final_link.st_dev, final_link.st_ino)
        ):
            _fail()
        return b"".join(chunks)
    except ArtifactBuildIdentityError:
        raise
    except OSError:
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _load_json_bytes(encoded: bytes, *, ascii_only: bool) -> Any:
    try:
        raw = encoded.decode("ascii" if ascii_only else "utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: _fail(),
        )
    except ArtifactBuildIdentityError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail()


def _object(value: Any, keys: Iterable[str] | None = None) -> dict[str, Any]:
    if type(value) is not dict or (keys is not None and set(value) != set(keys)):
        _fail()
    return value


def derive_artifact_build_identity(product_truth: Any) -> dict[str, Any]:
    """Project only renderer-safe fields from product-version-truth/v1."""

    truth = _object(product_truth)
    if truth.get("schemaVersion") != "product-version-truth/v1":
        _fail()
    product = _object(truth.get("product"))
    candidate = _object(truth.get("candidate"))
    canary = _object(candidate.get("canary"))
    baseline = _object(truth.get("publishedBaseline"))
    identity = {
        "candidateBuild": candidate.get("build"),
        "candidateVersion": candidate.get("version"),
        "canaryClock": canary.get("clock"),
        "canaryQualifiedMachines": canary.get("qualifiedMachines"),
        "canaryTargetMachines": canary.get("targetMachines"),
        "channel": candidate.get("channel"),
        "displayName": product.get("displayName"),
        "publicationStatus": candidate.get("publicationStatus"),
        "publishedBaselineTag": baseline.get("tag"),
        "publishedBaselineVersion": baseline.get("version"),
        "releaseEligible": candidate.get("releaseEligible"),
        "releaseStage": candidate.get("releaseStage"),
        "schemaVersion": "artifact-build-identity/v1",
    }
    return validate_artifact_build_identity(identity)


def validate_artifact_build_identity(value: Any) -> dict[str, Any]:
    identity = _object(value, IDENTITY_FIELDS)
    string_fields = (
        "candidateBuild",
        "candidateVersion",
        "canaryClock",
        "channel",
        "displayName",
        "publicationStatus",
        "publishedBaselineTag",
        "publishedBaselineVersion",
        "releaseStage",
        "schemaVersion",
    )
    if any(type(identity[field]) is not str for field in string_fields):
        _fail()
    if (
        identity["schemaVersion"] != "artifact-build-identity/v1"
        or identity["displayName"] != "UsageHub"
        or VERSION.fullmatch(identity["candidateVersion"]) is None
        or BUILD.fullmatch(identity["candidateBuild"]) is None
        or VERSION.fullmatch(identity["publishedBaselineVersion"]) is None
        or identity["publishedBaselineTag"]
        != f"v{identity['publishedBaselineVersion']}"
        or identity["channel"] not in {"alpha", "beta", "rc"}
        or identity["canaryClock"]
        not in {"not_started", "running", "passed", "blocked"}
    ):
        _fail()
    qualified = identity["canaryQualifiedMachines"]
    target = identity["canaryTargetMachines"]
    if (
        type(qualified) is not int
        or type(target) is not int
        or qualified < 0
        or target <= 0
        or qualified > target
        or type(identity["releaseEligible"]) is not bool
    ):
        _fail()
    state = (
        identity["releaseStage"],
        identity["publicationStatus"],
        identity["releaseEligible"],
    )
    if state not in {
        ("candidate", "not_published", False),
        ("prerelease_ready", "not_published", True),
        ("prerelease_published", "published_prerelease", False),
    }:
        _fail()
    return identity


def load_product_truth(path: str | Path) -> dict[str, Any]:
    payload = _load_json_bytes(
        _read_regular(Path(path), maximum=MAX_TRUTH_BYTES),
        ascii_only=False,
    )
    return _object(payload)


def load_artifact_build_identity(path: str | Path) -> tuple[dict[str, Any], bytes]:
    encoded = _read_regular(Path(path), maximum=MAX_IDENTITY_BYTES)
    payload = validate_artifact_build_identity(
        _load_json_bytes(encoded, ascii_only=True)
    )
    if encoded != canonical_identity_bytes(payload):
        _fail()
    return payload, encoded


def verify_artifact_build_identity(
    identity: str | Path,
    product_truth: str | Path,
) -> tuple[dict[str, Any], bytes]:
    """Bind one canonical identity file to one product truth source."""

    payload, encoded = load_artifact_build_identity(identity)
    expected = derive_artifact_build_identity(load_product_truth(product_truth))
    if payload != expected:
        _fail()
    return payload, encoded


def verify_packaged_build_identity(
    packaged_identity: str | Path,
    canonical_identity: str | Path,
    product_truth: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Three-way bind a packaged copy, canonical identity, and product truth."""

    canonical_payload, canonical_bytes = verify_artifact_build_identity(
        canonical_identity,
        product_truth,
    )
    packaged_path = Path(packaged_identity)
    if packaged_path.name != PACKAGED_IDENTITY_NAME:
        _fail()
    packaged_payload, packaged_bytes = load_artifact_build_identity(packaged_path)
    if packaged_payload != canonical_payload or packaged_bytes != canonical_bytes:
        _fail()
    return packaged_payload, {
        "name": PACKAGED_IDENTITY_NAME,
        "sha256": hashlib.sha256(packaged_bytes).hexdigest(),
        "sizeBytes": len(packaged_bytes),
    }


def verify_repository_artifact_build_identity(root: str | Path) -> dict[str, Any]:
    resolved = Path(root).resolve(strict=True)
    identity_path = resolved / CANONICAL_IDENTITY
    truth_path = resolved / PRODUCT_TRUTH
    payload, canonical_bytes = verify_artifact_build_identity(
        identity_path,
        truth_path,
    )
    for relative in REPOSITORY_COPIES:
        copy_payload, copy_bytes = load_artifact_build_identity(resolved / relative)
        if copy_payload != payload or copy_bytes != canonical_bytes:
            _fail()
    desktop = _object(
        _load_json_bytes(
            _read_regular(
                resolved / "desktop/package.json",
                maximum=MAX_TRUTH_BYTES,
            ),
            ascii_only=False,
        )
    )
    build = _object(desktop.get("build"))
    files = build.get("files")
    mappings = build.get("extraResources")
    extra_metadata = build.get("extraMetadata")
    if (
        build.get("buildVersion") != payload["candidateBuild"]
        or build.get("buildNumber") != payload["candidateBuild"]
        or extra_metadata
        != {
            "buildNumber": payload["candidateBuild"],
            "buildVersion": payload["candidateBuild"],
        }
        or type(files) is not list
        or "product_version_truth.js" not in files
        or "build_identity_copy.js" not in files
        or type(mappings) is not list
        or {
            "from": "../openusage_bar/resources/artifact-build-identity.v1.json",
            "to": PACKAGED_IDENTITY_NAME,
        }
        not in mappings
    ):
        _fail()
    return payload


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        _fail()


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--product-truth", type=Path)
    parser.add_argument("--packaged-copy", action="append", type=Path, default=[])
    return parser


def main(arguments: list[str]) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        root = parsed.root.resolve(strict=True)
        if parsed.identity is None and parsed.product_truth is None:
            payload = verify_repository_artifact_build_identity(root)
            canonical_path = root / CANONICAL_IDENTITY
            truth_path = root / PRODUCT_TRUTH
        else:
            canonical_path = parsed.identity or root / CANONICAL_IDENTITY
            truth_path = parsed.product_truth or root / PRODUCT_TRUTH
            payload, _encoded = verify_artifact_build_identity(
                canonical_path,
                truth_path,
            )
        for packaged in parsed.packaged_copy:
            verify_packaged_build_identity(
                packaged,
                canonical_path,
                truth_path,
            )
    except (ArtifactBuildIdentityError, OSError, RuntimeError):
        print("artifact_build_identity_invalid", file=sys.stderr)
        return 1
    print(
        "artifact_build_identity_ok "
        f"product={payload['displayName']} "
        f"candidate={payload['candidateVersion']} "
        f"build={payload['candidateBuild']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
