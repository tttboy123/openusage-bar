#!/usr/bin/env python3
"""Generate and verify path-free evidence for one native desktop CI row."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
from pathlib import Path
from typing import Any, Iterable


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.distribution_trust_posture import verify_distribution_trust
from scripts.observer_source_native_evidence import (
    EvidenceError as ObserverSourceEvidenceError,
    validate_evidence as validate_observer_source_evidence,
    verify_evidence_file as verify_observer_source_evidence_file,
)
from scripts.verify_artifact_build_identity import (
    ArtifactBuildIdentityError,
    CANONICAL_IDENTITY,
    PACKAGED_IDENTITY_NAME,
    PRODUCT_TRUTH,
    canonical_identity_bytes,
    validate_artifact_build_identity,
    verify_artifact_build_identity,
)


MAX_EVIDENCE_BYTES = 128 * 1024
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DECIMAL_ID = re.compile(r"[1-9][0-9]*")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

MATRIX = {
    ("mac", "x64"): {
        "runnerLabel": "macos-15-intel",
        "runnerOs": "macOS",
        "runnerArch": "X64",
        "artifactArch": "x64",
        "extension": "dmg",
        "containerFormat": "dmg",
        "collector": "openusage-collector",
        "collectorFormat": "macho64",
    },
    ("mac", "arm64"): {
        "runnerLabel": "macos-15",
        "runnerOs": "macOS",
        "runnerArch": "ARM64",
        "artifactArch": "arm64",
        "extension": "dmg",
        "containerFormat": "dmg",
        "collector": "openusage-collector",
        "collectorFormat": "macho64",
    },
    ("win", "x64"): {
        "runnerLabel": "windows-2025",
        "runnerOs": "Windows",
        "runnerArch": "X64",
        "artifactArch": "x64",
        "extension": "exe",
        "containerFormat": "nsis",
        "collector": "openusage-collector.exe",
        "collectorFormat": "pe32+",
    },
    ("win", "arm64"): {
        "runnerLabel": "windows-11-arm",
        "runnerOs": "Windows",
        "runnerArch": "ARM64",
        "artifactArch": "arm64",
        "extension": "exe",
        "containerFormat": "nsis",
        "collector": "openusage-collector.exe",
        "collectorFormat": "pe32+",
    },
    ("linux", "x64"): {
        "runnerLabel": "ubuntu-24.04",
        "runnerOs": "Linux",
        "runnerArch": "X64",
        "artifactArch": "x86_64",
        "extension": "AppImage",
        "containerFormat": "appimage",
        "collector": "openusage-collector",
        "collectorFormat": "elf64",
    },
    ("linux", "arm64"): {
        "runnerLabel": "ubuntu-24.04-arm",
        "runnerOs": "Linux",
        "runnerArch": "ARM64",
        "artifactArch": "arm64",
        "extension": "AppImage",
        "containerFormat": "appimage",
        "collector": "openusage-collector",
        "collectorFormat": "elf64",
    },
}

CHECK_NAMES = (
    "actionPins",
    "productVersionTruth",
    "runnerArchitecture",
    "pythonContracts",
    "windowsNativeProcessTests",
    "webDependencyAudit",
    "webTests",
    "webBuild",
    "desktopDependencyAudit",
    "desktopTests",
    "collectorStatus",
    "gatewaySelfTest",
    "desktopRuntimeCapabilitySmoke",
    "unpackedPackageAudit",
    "distributionTrustPosture",
    "packagedBuildIdentity",
    "finalMacContainerAudit",
    "finalWindowsContainerAudit",
    "finalLinuxContainerAudit",
)


class EvidenceError(ValueError):
    """A safe, allowlisted evidence-contract failure."""


def _fail(reason: str) -> None:
    raise EvidenceError(reason)


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{label}_unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail(f"{label}_not_regular")
    return metadata


def _open_regular(path: Path, label: str) -> tuple[int, os.stat_result]:
    metadata = _regular_file(path, label)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            _fail(f"{label}_not_regular")
        return descriptor, opened
    except EvidenceError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        _fail(f"{label}_not_regular")


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


def _inspect_regular(
    path: Path,
    label: str,
) -> tuple[os.stat_result, str, bytes, bytes]:
    descriptor, before = _open_regular(path, label)
    digest = hashlib.sha256()
    prefix = bytearray()
    trailer = b""
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if len(prefix) < 4096:
                prefix.extend(chunk[: 4096 - len(prefix)])
            trailer = (trailer + chunk)[-512:]
            total += len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        _fail("artifact_read_failed")
    finally:
        os.close(descriptor)
    if total != before.st_size or not _same_file(before, after):
        _fail(f"{label}_not_regular")
    return before, digest.hexdigest(), bytes(prefix), trailer


def _pe_arch(prefix: bytes) -> str | None:
    if len(prefix) < 64 or prefix[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", prefix, 60)[0]
    if pe_offset > len(prefix) - 26 or prefix[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    optional_header_magic = struct.unpack_from("<H", prefix, pe_offset + 24)[0]
    if optional_header_magic != 0x20B:
        return None
    machine = struct.unpack_from("<H", prefix, pe_offset + 4)[0]
    return {0x8664: "x64", 0xAA64: "arm64"}.get(machine)


def _elf_arch(prefix: bytes) -> str | None:
    if len(prefix) < 20 or prefix[:4] != b"\x7fELF" or prefix[4] != 2:
        return None
    byte_order = {1: "<", 2: ">"}.get(prefix[5])
    if byte_order is None:
        return None
    machine = struct.unpack_from(f"{byte_order}H", prefix, 18)[0]
    return {62: "x64", 183: "arm64"}.get(machine)


def _macho_arch(prefix: bytes) -> str | None:
    if len(prefix) < 8:
        return None
    if prefix[:4] == b"\xcf\xfa\xed\xfe":
        byte_order = "<"
    elif prefix[:4] == b"\xfe\xed\xfa\xcf":
        byte_order = ">"
    else:
        return None
    cpu_type = struct.unpack_from(f"{byte_order}I", prefix, 4)[0]
    return {0x01000007: "x64", 0x0100000C: "arm64"}.get(cpu_type)


def _collector_identity(prefix: bytes, expected_format: str) -> tuple[str, str]:
    if expected_format == "macho64":
        arch = _macho_arch(prefix)
    elif expected_format == "pe32+":
        arch = _pe_arch(prefix)
    elif expected_format == "elf64":
        arch = _elf_arch(prefix)
    else:  # pragma: no cover - constants are closed above
        arch = None
    if arch is None:
        _fail("collector_format_invalid")
    return expected_format, arch


def _validate_container(
    prefix: bytes,
    trailer: bytes,
    container_format: str,
    arch: str,
) -> None:
    if container_format == "dmg":
        if len(trailer) != 512 or trailer[:4] != b"koly":
            _fail("container_format_invalid")
    elif container_format == "nsis":
        if len(prefix) < 2 or prefix[:2] != b"MZ":
            _fail("container_format_invalid")
    elif container_format == "appimage":
        if _elf_arch(prefix) != arch:
            _fail("container_format_invalid")
    else:  # pragma: no cover - constants are closed above
        _fail("container_format_invalid")


def _artifact_record(
    path: Path,
    *,
    label: str,
    expected_name: str,
    file_format: str,
    arch: str | None = None,
    inspection: tuple[os.stat_result, str, bytes, bytes] | None = None,
) -> dict[str, Any]:
    metadata, sha256, _prefix, _trailer = (
        inspection if inspection is not None else _inspect_regular(path, label)
    )
    if path.name != expected_name:
        _fail(f"{label}_name_invalid")
    record: dict[str, Any] = {
        "format": file_format,
        "name": path.name,
        "sha256": sha256,
        "sizeBytes": metadata.st_size,
    }
    if arch is not None:
        record["arch"] = arch
    return record


def _validate_scalar_metadata(arguments: argparse.Namespace) -> None:
    if arguments.runner_environment != "github-hosted":
        _fail("runner_environment_invalid")
    row = MATRIX.get((arguments.platform, arguments.arch))
    if row is None:
        _fail("target_invalid")
    if (
        arguments.runner_label != row["runnerLabel"]
        or arguments.runner_os != row["runnerOs"]
        or arguments.runner_arch != row["runnerArch"]
    ):
        _fail("runner_identity_invalid")
    if VERSION.fullmatch(arguments.version) is None:
        _fail("product_version_invalid")
    if SHA1.fullmatch(arguments.source_sha) is None:
        _fail("source_sha_invalid")
    if arguments.source_event not in {"push", "pull_request", "workflow_dispatch"}:
        _fail("source_event_invalid")
    if DECIMAL_ID.fullmatch(arguments.run_id) is None:
        _fail("run_id_invalid")
    if DECIMAL_ID.fullmatch(arguments.run_attempt) is None:
        _fail("run_attempt_invalid")
    if re.fullmatch(r"3\.13\.[0-9]+", arguments.python_version) is None:
        _fail("python_version_invalid")
    if arguments.node_version != "24.16.0":
        _fail("node_version_invalid")
    if arguments.pyinstaller_version != "6.22.0":
        _fail("pyinstaller_version_invalid")
    if arguments.electron_builder_version != "26.15.3":
        _fail("electron_builder_version_invalid")


def _checks(values: Iterable[str], platform: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if value.count("=") != 1:
            _fail("check_invalid")
        name, outcome = value.split("=", 1)
        if name not in CHECK_NAMES or name in parsed:
            _fail("check_invalid")
        parsed[name] = outcome
    if tuple(sorted(parsed)) != tuple(sorted(CHECK_NAMES)):
        _fail("check_set_invalid")

    expected_skipped = {
        "windowsNativeProcessTests" if platform != "win" else "",
        "finalMacContainerAudit" if platform != "mac" else "",
        "finalWindowsContainerAudit" if platform != "win" else "",
        "finalLinuxContainerAudit" if platform != "linux" else "",
    } - {""}
    normalized: dict[str, str] = {}
    for name in CHECK_NAMES:
        expected = "skipped" if name in expected_skipped else "success"
        if parsed[name] != expected:
            _fail("check_outcome_invalid")
        normalized[name] = (
            "not_applicable" if expected == "skipped" else "passed"
        )
    return normalized


def _distribution_trust(platform: str) -> dict[str, Any]:
    return {
        "platformCodeSigning": "not_verified",
        "platformNotarization": (
            "not_verified" if platform == "mac" else "not_applicable"
        ),
        "policy": "native-distribution-trust-nonclaim/v1",
        "provenanceAttestation": "not_verified",
        "releaseEligible": False,
    }


def _validated_build_identity(value: Any) -> dict[str, Any]:
    _exact_keys(value, ("artifact", "identity"), "build_identity")
    artifact = value["artifact"]
    _exact_keys(
        artifact,
        ("name", "sha256", "sizeBytes"),
        "build_identity_artifact",
    )
    try:
        identity = validate_artifact_build_identity(value["identity"])
        encoded = canonical_identity_bytes(identity)
    except ArtifactBuildIdentityError:
        _fail("build_identity_invalid")
    if (
        artifact["name"] != PACKAGED_IDENTITY_NAME
        or type(artifact["sha256"]) is not str
        or artifact["sha256"] != hashlib.sha256(encoded).hexdigest()
        or type(artifact["sizeBytes"]) is not int
        or isinstance(artifact["sizeBytes"], bool)
        or artifact["sizeBytes"] != len(encoded)
    ):
        _fail("build_identity_invalid")
    return {"artifact": dict(artifact), "identity": dict(identity)}


def _build_identity(arguments: argparse.Namespace) -> dict[str, Any]:
    embedded = getattr(arguments, "embedded_build_identity", None)
    if embedded is not None:
        return _validated_build_identity(embedded)
    try:
        identity, encoded = verify_artifact_build_identity(
            arguments.build_identity,
            arguments.product_truth,
        )
    except ArtifactBuildIdentityError:
        _fail("build_identity_invalid")
    return _validated_build_identity(
        {
            "artifact": {
                "name": PACKAGED_IDENTITY_NAME,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "sizeBytes": len(encoded),
            },
            "identity": identity,
        }
    )


def _validated_observer_source_evidence(value: Any) -> dict[str, Any]:
    try:
        validated = validate_observer_source_evidence(value)
    except ObserverSourceEvidenceError:
        _fail("observer_source_evidence_invalid")
    return json.loads(
        json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _observer_source_evidence(
    arguments: argparse.Namespace,
) -> dict[str, Any] | None:
    platform = arguments.platform
    source_path = getattr(arguments, "observer_source_evidence", None)
    embedded = getattr(arguments, "embedded_observer_source_evidence", None)
    allow_legacy = getattr(
        arguments, "allow_legacy_observer_source_evidence", False
    )
    if platform == "mac":
        if source_path is not None or embedded is not None:
            _fail("observer_source_evidence_not_applicable")
        return None
    if source_path is not None and embedded is not None:
        _fail("observer_source_evidence_invalid")
    if embedded is not None:
        value = _validated_observer_source_evidence(embedded)
    elif source_path is not None:
        try:
            value = verify_observer_source_evidence_file(Path(source_path))
        except ObserverSourceEvidenceError:
            _fail("observer_source_evidence_invalid")
        value = _validated_observer_source_evidence(value)
    elif allow_legacy:
        return None
    else:
        _fail("observer_source_evidence_required")
    expected_platform = "windows" if platform == "win" else "linux"
    if value["platform"] != expected_platform:
        _fail("observer_source_evidence_platform_invalid")
    return value


def _payload(arguments: argparse.Namespace) -> dict[str, Any]:
    _validate_scalar_metadata(arguments)
    row = MATRIX[(arguments.platform, arguments.arch)]
    observer_source_evidence = _observer_source_evidence(arguments)
    collector_path = Path(arguments.collector)
    artifact_path = Path(arguments.artifact)
    trust_posture_path = Path(arguments.trust_posture_report)
    collector_inspection = _inspect_regular(collector_path, "collector")
    artifact_inspection = _inspect_regular(artifact_path, "final_container")
    collector_format, collector_arch = _collector_identity(
        collector_inspection[2], row["collectorFormat"]
    )
    if collector_arch != arguments.arch:
        _fail("collector_arch_invalid")
    artifact_name = (
        f"UsageHub-{arguments.version}-{arguments.platform}-"
        f"{row['artifactArch']}.{row['extension']}"
    )
    _validate_container(
        artifact_inspection[2],
        artifact_inspection[3],
        row["containerFormat"],
        arguments.arch,
    )
    try:
        verify_distribution_trust(
            report=trust_posture_path,
            platform=arguments.platform,
            artifact=artifact_path,
        )
    except ValueError:
        _fail("distribution_trust_posture_invalid")
    checks = _checks(arguments.check, arguments.platform)
    build_identity = _build_identity(arguments)
    if (
        build_identity["identity"]["displayName"] != "UsageHub"
        or build_identity["identity"]["candidateVersion"] != arguments.version
    ):
        _fail("build_identity_invalid")
    final_check = {
        "mac": "finalMacContainerAudit",
        "win": "finalWindowsContainerAudit",
        "linux": "finalLinuxContainerAudit",
    }[arguments.platform]
    payload = {
        "artifacts": {
            "collector": _artifact_record(
                collector_path,
                label="collector",
                expected_name=row["collector"],
                file_format=collector_format,
                arch=collector_arch,
                inspection=collector_inspection,
            ),
            "finalContainer": _artifact_record(
                artifact_path,
                label="final_container",
                expected_name=artifact_name,
                file_format=row["containerFormat"],
                inspection=artifact_inspection,
            ),
            "distributionTrustPosture": _artifact_record(
                trust_posture_path,
                label="distribution_trust_posture",
                expected_name=(
                    "usagehub-distribution-trust-"
                    f"{arguments.platform}-{arguments.arch}.json"
                ),
                file_format="json",
            ),
        },
        "buildIdentity": build_identity,
        "checks": checks,
        "distributionTrust": _distribution_trust(arguments.platform),
        "privacy": {
            "finalExtractedPackageAudit": checks[final_check],
            "finalExtractedPackageAuditPolicy": "release-artifact-audit/v1",
            "frozenSmokeRealCredentialReads": 0,
            "frozenSmokeRealProviderNetworkCalls": 0,
            "policy": "native-ci-evidence-allowlist/v1",
        },
        "product": {"name": "UsageHub", "version": arguments.version},
        "runner": {
            "arch": arguments.runner_arch,
            "environment": arguments.runner_environment,
            "os": arguments.runner_os,
            "requestedLabel": arguments.runner_label,
        },
        "schemaVersion": 1,
        "source": {
            "event": arguments.source_event,
            "runAttempt": arguments.run_attempt,
            "runId": arguments.run_id,
            "sha": arguments.source_sha,
        },
        "target": {
            "arch": arguments.arch,
            "builderArch": arguments.arch,
            "collectorMember": f"collector/{row['collector']}",
            "containerFormat": row["containerFormat"],
            "platform": arguments.platform,
        },
        "toolchain": {
            "electronBuilder": arguments.electron_builder_version,
            "node": arguments.node_version,
            "pyInstaller": arguments.pyinstaller_version,
            "python": arguments.python_version,
        },
    }
    if observer_source_evidence is not None:
        payload["observerSourceEvidence"] = observer_source_evidence
    return payload


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _exact_keys(value: Any, keys: Iterable[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(f"{label}_schema_invalid")


def _validate_loaded_payload(payload: Any) -> dict[str, Any]:
    root_keys = {
        "artifacts",
        "buildIdentity",
        "checks",
        "distributionTrust",
        "privacy",
        "product",
        "runner",
        "schemaVersion",
        "source",
        "target",
        "toolchain",
    }
    if not isinstance(payload, dict) or frozenset(payload) not in {
        frozenset(root_keys),
        frozenset(root_keys | {"observerSourceEvidence"}),
    }:
        _fail("root_schema_invalid")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1:
        _fail("schema_version_invalid")
    _exact_keys(
        payload["artifacts"],
        ("collector", "distributionTrustPosture", "finalContainer"),
        "artifacts",
    )
    _exact_keys(
        payload["artifacts"]["collector"],
        ("arch", "format", "name", "sha256", "sizeBytes"),
        "collector",
    )
    _exact_keys(
        payload["artifacts"]["finalContainer"],
        ("format", "name", "sha256", "sizeBytes"),
        "final_container",
    )
    _exact_keys(
        payload["artifacts"]["distributionTrustPosture"],
        ("format", "name", "sha256", "sizeBytes"),
        "distribution_trust_posture",
    )
    payload["buildIdentity"] = _validated_build_identity(
        payload["buildIdentity"]
    )
    _exact_keys(payload["checks"], CHECK_NAMES, "checks")
    _exact_keys(
        payload["distributionTrust"],
        (
            "platformCodeSigning",
            "platformNotarization",
            "policy",
            "provenanceAttestation",
            "releaseEligible",
        ),
        "distribution_trust",
    )
    _exact_keys(
        payload["privacy"],
        (
            "finalExtractedPackageAudit",
            "finalExtractedPackageAuditPolicy",
            "frozenSmokeRealCredentialReads",
            "frozenSmokeRealProviderNetworkCalls",
            "policy",
        ),
        "privacy",
    )
    _exact_keys(payload["product"], ("name", "version"), "product")
    _exact_keys(
        payload["runner"],
        ("arch", "environment", "os", "requestedLabel"),
        "runner",
    )
    _exact_keys(payload["source"], ("event", "runAttempt", "runId", "sha"), "source")
    _exact_keys(
        payload["target"],
        ("arch", "builderArch", "collectorMember", "containerFormat", "platform"),
        "target",
    )
    _exact_keys(
        payload["toolchain"],
        ("electronBuilder", "node", "pyInstaller", "python"),
        "toolchain",
    )

    string_values = [
        *(payload["artifacts"]["collector"][key] for key in ("arch", "format", "name", "sha256")),
        *(payload["artifacts"]["finalContainer"][key] for key in ("format", "name", "sha256")),
        *(payload["artifacts"]["distributionTrustPosture"][key] for key in ("format", "name", "sha256")),
        *payload["checks"].values(),
        *(payload["privacy"][key] for key in (
            "finalExtractedPackageAudit",
            "finalExtractedPackageAuditPolicy",
            "policy",
        )),
        *payload["product"].values(),
        *payload["runner"].values(),
        *payload["source"].values(),
        *payload["target"].values(),
        *payload["toolchain"].values(),
    ]
    if any(not isinstance(value, str) for value in string_values):
        _fail("field_type_invalid")
    integer_values = [
        payload["artifacts"]["collector"]["sizeBytes"],
        payload["artifacts"]["finalContainer"]["sizeBytes"],
        payload["artifacts"]["distributionTrustPosture"]["sizeBytes"],
        payload["privacy"]["frozenSmokeRealCredentialReads"],
        payload["privacy"]["frozenSmokeRealProviderNetworkCalls"],
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in integer_values
    ):
        _fail("field_type_invalid")
    expected_distribution_trust = _distribution_trust(
        payload["target"]["platform"]
    )
    if any(
        type(payload["distributionTrust"][key])
        is not type(expected_distribution_trust[key])
        or payload["distributionTrust"][key] != expected_distribution_trust[key]
        for key in expected_distribution_trust
    ):
        _fail("distribution_trust_invalid")
    if any(
        not SHA256.fullmatch(record["sha256"])
        or record["sizeBytes"] <= 0
        for record in payload["artifacts"].values()
    ):
        _fail("artifact_metadata_invalid")
    if payload["product"] != {
        "name": payload["buildIdentity"]["identity"]["displayName"],
        "version": payload["buildIdentity"]["identity"]["candidateVersion"],
    }:
        _fail("build_identity_invalid")
    embedded_source_evidence = payload.get("observerSourceEvidence")
    target_platform = payload["target"]["platform"]
    if target_platform == "mac":
        if embedded_source_evidence is not None:
            _fail("observer_source_evidence_not_applicable")
    elif embedded_source_evidence is not None:
        validated_source_evidence = _validated_observer_source_evidence(
            embedded_source_evidence
        )
        expected_platform = (
            "windows" if target_platform == "win" else "linux"
        )
        if validated_source_evidence["platform"] != expected_platform:
            _fail("observer_source_evidence_platform_invalid")
        payload["observerSourceEvidence"] = validated_source_evidence
    return payload


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_key")
        value[key] = item
    return value


def _validate_json_complexity(value: Any) -> None:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > 16:
            _fail("evidence_depth_invalid")
        if nodes > 4096:
            _fail("evidence_complexity_invalid")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _preflight_json_depth(raw: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > 16:
                _fail("evidence_depth_invalid")
        elif character in "]}":
            depth -= 1


def _load_evidence(path: Path) -> tuple[dict[str, Any], str]:
    descriptor, metadata = _open_regular(path, "evidence")
    if metadata.st_size <= 0 or metadata.st_size > MAX_EVIDENCE_BYTES:
        os.close(descriptor)
        _fail("evidence_size_invalid")
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_EVIDENCE_BYTES:
                _fail("evidence_size_invalid")
        after = os.fstat(descriptor)
        if total != metadata.st_size or not _same_file(metadata, after):
            _fail("evidence_not_regular")
        raw = b"".join(chunks).decode("utf-8")
        _preflight_json_depth(raw)
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: _fail("number_invalid"),
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("evidence_json_invalid")
    finally:
        os.close(descriptor)
    _validate_json_complexity(payload)
    return _validate_loaded_payload(payload), raw


def _namespace_from_payload(
    payload: dict[str, Any],
    collector: str,
    artifact: str,
    trust_posture_report: str,
) -> argparse.Namespace:
    target = payload["target"]
    runner = payload["runner"]
    source = payload["source"]
    toolchain = payload["toolchain"]
    reverse_checks = [
        f"{name}={'skipped' if value == 'not_applicable' else 'success'}"
        for name, value in payload["checks"].items()
    ]
    return argparse.Namespace(
        platform=target["platform"],
        arch=target["arch"],
        runner_label=runner["requestedLabel"],
        runner_environment=runner["environment"],
        runner_os=runner["os"],
        runner_arch=runner["arch"],
        collector=collector,
        artifact=artifact,
        trust_posture_report=trust_posture_report,
        build_identity=None,
        product_truth=None,
        embedded_build_identity=payload["buildIdentity"],
        observer_source_evidence=None,
        embedded_observer_source_evidence=payload.get(
            "observerSourceEvidence"
        ),
        allow_legacy_observer_source_evidence=(
            "observerSourceEvidence" not in payload
        ),
        version=payload["product"]["version"],
        source_sha=source["sha"],
        source_event=source["event"],
        run_id=source["runId"],
        run_attempt=source["runAttempt"],
        python_version=toolchain["python"],
        node_version=toolchain["node"],
        pyinstaller_version=toolchain["pyInstaller"],
        electron_builder_version=toolchain["electronBuilder"],
        check=reverse_checks,
    )


def _write_output(path: Path, content: str) -> None:
    descriptor: int | None = None
    try:
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
        ):
            _fail("output_not_regular")
        flags = (
            os.O_RDWR
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if existing is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            existing is not None
            and (existing.st_dev, existing.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            _fail("output_not_regular")
        os.ftruncate(descriptor, 0)
        encoded = content.encode("ascii")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("output_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = bytearray()
        while len(observed) < len(encoded):
            chunk = os.read(descriptor, len(encoded) - len(observed))
            if not chunk:
                break
            observed.extend(chunk)
        after = os.fstat(descriptor)
        linked = path.lstat()
        if (
            bytes(observed) != encoded
            or after.st_size != len(encoded)
            or not stat.S_ISREG(linked.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (after.st_dev, after.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (linked.st_dev, linked.st_ino)
        ):
            _fail("output_write_failed")
    except EvidenceError:
        raise
    except (OSError, UnicodeError):
        _fail("output_write_failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def generate(arguments: argparse.Namespace) -> None:
    payload = _payload(arguments)
    _write_output(Path(arguments.output), _canonical(payload))
    print(f"native_ci_evidence_generated {arguments.platform}/{arguments.arch}")


def verify_native_ci_evidence(
    *,
    evidence: str | Path,
    collector: str | Path,
    artifact: str | Path,
    trust_posture_report: str | Path,
) -> dict[str, Any]:
    """Verify one evidence row without writing user-facing output."""

    evidence_path = Path(evidence)
    payload, raw = _load_evidence(evidence_path)
    if raw != _canonical(payload):
        _fail("evidence_not_canonical")
    expected = _payload(
        _namespace_from_payload(
            payload,
            str(collector),
            str(artifact),
            str(trust_posture_report),
        )
    )
    if payload != expected:
        _fail("evidence_mismatch")
    return payload


def verify(arguments: argparse.Namespace) -> None:
    payload = verify_native_ci_evidence(
        evidence=arguments.evidence,
        collector=arguments.collector,
        artifact=arguments.artifact,
        trust_posture_report=arguments.trust_posture_report,
    )
    print(
        f"native_ci_evidence_ok {payload['target']['platform']}/"
        f"{payload['target']['arch']}"
    )


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        _fail("arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    generate_parser = subparsers.add_parser("generate")
    for name, choices in (
        ("platform", tuple(sorted({key[0] for key in MATRIX}))),
        ("arch", ("x64", "arm64")),
    ):
        generate_parser.add_argument(f"--{name}", required=True, choices=choices)
    for name in (
        "runner-label",
        "runner-environment",
        "runner-os",
        "runner-arch",
        "collector",
        "artifact",
        "trust-posture-report",
        "version",
        "source-sha",
        "source-event",
        "run-id",
        "run-attempt",
        "python-version",
        "node-version",
        "pyinstaller-version",
        "electron-builder-version",
        "output",
    ):
        generate_parser.add_argument(f"--{name}", required=True)
    generate_parser.add_argument(
        "--build-identity",
        default=str(REPOSITORY_ROOT / CANONICAL_IDENTITY),
    )
    generate_parser.add_argument(
        "--product-truth",
        default=str(REPOSITORY_ROOT / PRODUCT_TRUTH),
    )
    generate_parser.add_argument("--observer-source-evidence")
    generate_parser.add_argument("--check", action="append", default=[])
    generate_parser.set_defaults(handler=generate)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--evidence", required=True)
    verify_parser.add_argument("--collector", required=True)
    verify_parser.add_argument("--artifact", required=True)
    verify_parser.add_argument("--trust-posture-report", required=True)
    verify_parser.set_defaults(handler=verify)
    return parser


def main(arguments: list[str]) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        parsed.handler(parsed)
    except EvidenceError as error:
        print(f"native_ci_evidence_invalid reason={error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
