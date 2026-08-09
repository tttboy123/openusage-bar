#!/usr/bin/env python3
"""Assemble and offline-verify one local native release handoff bundle."""

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


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.native_ci_evidence import verify_native_ci_evidence
from scripts.verify_artifact_build_identity import (
    ArtifactBuildIdentityError,
    PACKAGED_IDENTITY_NAME,
    canonical_identity_bytes,
    validate_artifact_build_identity,
)


MAX_MANIFEST_BYTES = 64 * 1024
SCHEMA_VERSION = "release-handoff/v1"
OBJECT = "release.handoff_bundle"
POLICY = "release-handoff-local-nonclaim/v1"
FILE_ROLES = (
    "collector",
    "distributionTrustPosture",
    "finalContainer",
    "nativeCiEvidence",
)
TARGETS = {
    ("mac", "x64"),
    ("mac", "arm64"),
    ("win", "x64"),
    ("win", "arm64"),
    ("linux", "x64"),
    ("linux", "arm64"),
}
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
DECIMAL_ID = re.compile(r"[1-9][0-9]*")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
SAFE_NAME = re.compile(r"[^/\\\x00]{1,255}")
MANIFEST_NAME = re.compile(
    r"usagehub-release-handoff-(mac|win|linux)-(x64|arm64)\.json"
)
ALLOWED_REASONS = frozenset(
    {
        "arguments_invalid",
        "bundle_exists",
        "bundle_layout_invalid",
        "bundle_unavailable",
        "copy_failed",
        "file_metadata_mismatch",
        "input_collision",
        "input_invalid",
        "input_name_invalid",
        "manifest_binding_invalid",
        "manifest_invalid",
        "manifest_not_canonical",
        "native_evidence_invalid",
        "operation_failed",
        "output_write_failed",
    }
)


class ReleaseHandoffError(ValueError):
    """A path-free, allowlisted handoff contract failure."""


def _fail(reason: str) -> None:
    if reason not in ALLOWED_REASONS:
        reason = "operation_failed"
    raise ReleaseHandoffError(reason)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _safe_name(name: object) -> bool:
    return (
        type(name) is str
        and name not in {".", ".."}
        and SAFE_NAME.fullmatch(name) is not None
    )


def _lstat(path: Path, reason: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        _fail(reason)


def _require_directory(path: Path) -> None:
    metadata = _lstat(path, "bundle_unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("bundle_unavailable")


def _require_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("bundle_unavailable")
    _fail("bundle_exists")


def _open_regular(path: Path, reason: str) -> tuple[int, os.stat_result]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail(reason)
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
            os.close(descriptor)
            _fail(reason)
        return descriptor, opened
    except ReleaseHandoffError:
        raise
    except OSError:
        _fail(reason)


def _same_open_file(before: os.stat_result, after: os.stat_result) -> bool:
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


def _file_record(path: Path, *, reason: str) -> dict[str, Any]:
    descriptor, before = _open_regular(path, reason)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        _fail(reason)
    finally:
        os.close(descriptor)
    if total <= 0 or total != before.st_size or not _same_open_file(before, after):
        _fail(reason)
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "sizeBytes": total,
    }


def _copy_regular(source: Path, destination: Path) -> None:
    source_descriptor, before = _open_regular(source, "input_invalid")
    destination_descriptor: int | None = None
    total = 0
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    _fail("copy_failed")
                total += written
                view = view[written:]
        after = os.fstat(source_descriptor)
        os.fsync(destination_descriptor)
    except ReleaseHandoffError:
        raise
    except OSError:
        _fail("copy_failed")
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
    if total <= 0 or total != before.st_size or not _same_open_file(before, after):
        _fail("copy_failed")


def _write_exclusive(path: Path, content: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        encoded = content.encode("ascii")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("output_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except ReleaseHandoffError:
        raise
    except (OSError, UnicodeError):
        _fail("output_write_failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _exact_keys(value: Any, keys: Iterable[str]) -> None:
    if type(value) is not dict or set(value) != set(keys):
        _fail("manifest_invalid")


def _validate_manifest(payload: Any) -> dict[str, Any]:
    _exact_keys(
        payload,
        (
            "buildIdentity",
            "files",
            "object",
            "policy",
            "product",
            "provenanceAttestation",
            "releaseEligible",
            "schemaVersion",
            "source",
            "target",
        ),
    )
    if (
        payload["schemaVersion"] != SCHEMA_VERSION
        or payload["object"] != OBJECT
        or payload["policy"] != POLICY
        or payload["provenanceAttestation"] != "not_verified"
        or type(payload["releaseEligible"]) is not bool
        or payload["releaseEligible"] is not False
    ):
        _fail("manifest_invalid")

    build_identity = payload["buildIdentity"]
    _exact_keys(build_identity, ("artifact", "identity"))
    artifact_identity = build_identity["artifact"]
    _exact_keys(artifact_identity, ("name", "sha256", "sizeBytes"))
    try:
        identity = validate_artifact_build_identity(build_identity["identity"])
        identity_bytes = canonical_identity_bytes(identity)
    except ArtifactBuildIdentityError:
        _fail("manifest_invalid")
    if (
        artifact_identity["name"] != PACKAGED_IDENTITY_NAME
        or type(artifact_identity["sha256"]) is not str
        or artifact_identity["sha256"] != hashlib.sha256(identity_bytes).hexdigest()
        or type(artifact_identity["sizeBytes"]) is not int
        or isinstance(artifact_identity["sizeBytes"], bool)
        or artifact_identity["sizeBytes"] != len(identity_bytes)
    ):
        _fail("manifest_invalid")

    _exact_keys(payload["product"], ("name", "version"))
    if (
        payload["product"]["name"] != "UsageHub"
        or type(payload["product"]["version"]) is not str
        or VERSION.fullmatch(payload["product"]["version"]) is None
    ):
        _fail("manifest_invalid")
    if payload["product"] != {
        "name": identity["displayName"],
        "version": identity["candidateVersion"],
    }:
        _fail("manifest_invalid")

    _exact_keys(payload["source"], ("event", "runAttempt", "runId", "sha"))
    source = payload["source"]
    if (
        type(source["event"]) is not str
        or source["event"] not in {"push", "pull_request", "workflow_dispatch"}
        or type(source["runAttempt"]) is not str
        or DECIMAL_ID.fullmatch(source["runAttempt"]) is None
        or type(source["runId"]) is not str
        or DECIMAL_ID.fullmatch(source["runId"]) is None
        or type(source["sha"]) is not str
        or SHA1.fullmatch(source["sha"]) is None
    ):
        _fail("manifest_invalid")

    _exact_keys(payload["target"], ("arch", "platform"))
    target = payload["target"]
    if (
        type(target["platform"]) is not str
        or type(target["arch"]) is not str
        or (target["platform"], target["arch"]) not in TARGETS
    ):
        _fail("manifest_invalid")

    _exact_keys(payload["files"], FILE_ROLES)
    names: list[str] = []
    for role in FILE_ROLES:
        record = payload["files"][role]
        _exact_keys(record, ("name", "sha256", "sizeBytes"))
        if (
            not _safe_name(record["name"])
            or type(record["sha256"]) is not str
            or SHA256.fullmatch(record["sha256"]) is None
            or type(record["sizeBytes"]) is not int
            or isinstance(record["sizeBytes"], bool)
            or record["sizeBytes"] <= 0
        ):
            _fail("manifest_invalid")
        names.append(record["name"])
    if len(set(names)) != len(names):
        _fail("manifest_invalid")
    return payload


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("manifest_invalid")
        value[key] = item
    return value


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    descriptor, metadata = _open_regular(path, "manifest_invalid")
    try:
        if metadata.st_size <= 0 or metadata.st_size > MAX_MANIFEST_BYTES:
            _fail("manifest_invalid")
        encoded = bytearray()
        while len(encoded) <= MAX_MANIFEST_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    64 * 1024,
                    MAX_MANIFEST_BYTES + 1 - len(encoded),
                ),
            )
            if not chunk:
                break
            encoded.extend(chunk)
        after = os.fstat(descriptor)
        linked = _lstat(path, "manifest_invalid")
        if (
            len(encoded) > MAX_MANIFEST_BYTES
            or len(encoded) != metadata.st_size
            or not _same_open_file(metadata, after)
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (linked.st_dev, linked.st_ino)
        ):
            _fail("manifest_invalid")
        raw = bytes(encoded).decode("utf-8")
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: _fail("manifest_invalid"),
        )
    except ReleaseHandoffError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("manifest_invalid")
    finally:
        os.close(descriptor)
    validated = _validate_manifest(payload)
    if raw != _canonical(validated):
        _fail("manifest_not_canonical")
    return validated, raw


def _bundle_entries(bundle: Path) -> dict[str, Path]:
    metadata = _lstat(bundle, "bundle_unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("bundle_unavailable")
    entries: dict[str, Path] = {}
    try:
        with os.scandir(bundle) as iterator:
            for entry in iterator:
                if entry.name in entries or not _safe_name(entry.name):
                    _fail("bundle_layout_invalid")
                entry_metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or not stat.S_ISREG(entry_metadata.st_mode):
                    _fail("bundle_layout_invalid")
                entries[entry.name] = bundle / entry.name
    except ReleaseHandoffError:
        raise
    except OSError:
        _fail("bundle_unavailable")
    if len(entries) != 5:
        _fail("bundle_layout_invalid")
    return entries


def _native_payload(
    *,
    evidence: Path,
    collector: Path,
    artifact: Path,
    trust_posture_report: Path,
) -> dict[str, Any]:
    try:
        return verify_native_ci_evidence(
            evidence=evidence,
            collector=collector,
            artifact=artifact,
            trust_posture_report=trust_posture_report,
        )
    except (ValueError, OSError, UnicodeError):
        _fail("native_evidence_invalid")


def _plain_native_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record["name"],
        "sha256": record["sha256"],
        "sizeBytes": record["sizeBytes"],
    }


def verify_release_handoff(bundle_dir: str | Path) -> dict[str, Any]:
    """Offline-verify one exact five-file handoff directory."""

    bundle = Path(bundle_dir)
    entries = _bundle_entries(bundle)
    manifest_names = [name for name in entries if MANIFEST_NAME.fullmatch(name)]
    if len(manifest_names) != 1:
        _fail("bundle_layout_invalid")
    manifest_name = manifest_names[0]
    manifest, manifest_raw = _load_manifest(entries[manifest_name])
    platform = manifest["target"]["platform"]
    arch = manifest["target"]["arch"]
    if manifest_name != f"usagehub-release-handoff-{platform}-{arch}.json":
        _fail("manifest_binding_invalid")

    expected_names = {
        manifest_name,
        *(manifest["files"][role]["name"] for role in FILE_ROLES),
    }
    if set(entries) != expected_names:
        _fail("bundle_layout_invalid")

    observed: dict[str, dict[str, Any]] = {}
    for role in FILE_ROLES:
        record = manifest["files"][role]
        actual = _file_record(
            entries[record["name"]],
            reason="file_metadata_mismatch",
        )
        if actual != record:
            _fail("file_metadata_mismatch")
        observed[role] = actual

    expected_evidence_name = f"usagehub-native-evidence-{platform}-{arch}.json"
    if manifest["files"]["nativeCiEvidence"]["name"] != expected_evidence_name:
        _fail("manifest_binding_invalid")
    native = _native_payload(
        evidence=entries[manifest["files"]["nativeCiEvidence"]["name"]],
        collector=entries[manifest["files"]["collector"]["name"]],
        artifact=entries[manifest["files"]["finalContainer"]["name"]],
        trust_posture_report=entries[
            manifest["files"]["distributionTrustPosture"]["name"]
        ],
    )
    if (
        manifest["buildIdentity"] != native["buildIdentity"]
        or manifest["product"] != native["product"]
        or manifest["source"] != native["source"]
        or manifest["target"]
        != {
            "arch": native["target"]["arch"],
            "platform": native["target"]["platform"],
        }
        or manifest["files"]["collector"]
        != _plain_native_record(native["artifacts"]["collector"])
        or manifest["files"]["finalContainer"]
        != _plain_native_record(native["artifacts"]["finalContainer"])
        or manifest["files"]["distributionTrustPosture"]
        != _plain_native_record(native["artifacts"]["distributionTrustPosture"])
    ):
        _fail("manifest_binding_invalid")

    for role in FILE_ROLES:
        record = manifest["files"][role]
        if (
            _file_record(
                entries[record["name"]],
                reason="file_metadata_mismatch",
            )
            != observed[role]
        ):
            _fail("file_metadata_mismatch")
    final_entries = _bundle_entries(bundle)
    if set(final_entries) != expected_names:
        _fail("bundle_layout_invalid")
    final_manifest, final_raw = _load_manifest(final_entries[manifest_name])
    if final_manifest != manifest or final_raw != manifest_raw:
        _fail("manifest_invalid")
    return manifest


def _preflight_inputs(
    *,
    collector: Path,
    artifact: Path,
    evidence: Path,
    trust_posture_report: Path,
) -> dict[str, Any]:
    paths = (collector, artifact, evidence, trust_posture_report)
    if any(not _safe_name(path.name) for path in paths):
        _fail("input_name_invalid")
    if len({path.name for path in paths}) != len(paths):
        _fail("input_collision")
    for path in paths:
        _file_record(path, reason="input_invalid")
    native = _native_payload(
        evidence=evidence,
        collector=collector,
        artifact=artifact,
        trust_posture_report=trust_posture_report,
    )
    platform = native["target"]["platform"]
    arch = native["target"]["arch"]
    if evidence.name != f"usagehub-native-evidence-{platform}-{arch}.json":
        _fail("input_name_invalid")
    return native


def _cleanup_created(paths: list[Path], bundle: Path) -> None:
    for path in reversed(paths):
        try:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                path.unlink()
        except OSError:
            pass
    try:
        bundle.rmdir()
    except OSError:
        pass


def assemble_release_handoff(
    *,
    bundle_dir: str | Path,
    collector: str | Path,
    artifact: str | Path,
    evidence: str | Path,
    trust_posture_report: str | Path,
) -> dict[str, Any]:
    """Copy one verified native row into a deterministic local handoff."""

    bundle = Path(bundle_dir)
    collector_path = Path(collector)
    artifact_path = Path(artifact)
    evidence_path = Path(evidence)
    posture_path = Path(trust_posture_report)
    native = _preflight_inputs(
        collector=collector_path,
        artifact=artifact_path,
        evidence=evidence_path,
        trust_posture_report=posture_path,
    )
    _require_directory(bundle.parent)
    _require_absent(bundle)
    try:
        os.mkdir(bundle, 0o700)
    except FileExistsError:
        _fail("bundle_exists")
    except OSError:
        _fail("bundle_unavailable")

    created: list[Path] = []
    sources = {
        "collector": collector_path,
        "distributionTrustPosture": posture_path,
        "finalContainer": artifact_path,
        "nativeCiEvidence": evidence_path,
    }
    try:
        bundled: dict[str, Path] = {}
        for role in FILE_ROLES:
            destination = bundle / sources[role].name
            created.append(destination)
            _copy_regular(sources[role], destination)
            bundled[role] = destination

        platform = native["target"]["platform"]
        arch = native["target"]["arch"]
        manifest = {
            "buildIdentity": native["buildIdentity"],
            "files": {
                role: _file_record(
                    bundled[role],
                    reason="file_metadata_mismatch",
                )
                for role in FILE_ROLES
            },
            "object": OBJECT,
            "policy": POLICY,
            "product": native["product"],
            "provenanceAttestation": "not_verified",
            "releaseEligible": False,
            "schemaVersion": SCHEMA_VERSION,
            "source": native["source"],
            "target": {"arch": arch, "platform": platform},
        }
        manifest_path = bundle / (
            f"usagehub-release-handoff-{platform}-{arch}.json"
        )
        created.append(manifest_path)
        _write_exclusive(manifest_path, _canonical(manifest))
        return verify_release_handoff(bundle)
    except Exception:
        _cleanup_created(created, bundle)
        raise


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
    assemble_parser = subparsers.add_parser("assemble")
    for name in (
        "bundle-dir",
        "collector",
        "artifact",
        "evidence",
        "trust-posture-report",
    ):
        assemble_parser.add_argument(f"--{name}", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle-dir", required=True)
    return parser


def main(arguments: list[str]) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command == "assemble":
            payload = assemble_release_handoff(
                bundle_dir=parsed.bundle_dir,
                collector=parsed.collector,
                artifact=parsed.artifact,
                evidence=parsed.evidence,
                trust_posture_report=parsed.trust_posture_report,
            )
            action = "assembled"
        else:
            payload = verify_release_handoff(parsed.bundle_dir)
            action = "verified"
        print(
            f"release_handoff_{action} {payload['target']['platform']}/"
            f"{payload['target']['arch']} files=5 releaseEligible=false"
        )
    except ReleaseHandoffError as error:
        print(f"release_handoff_invalid reason={error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "release_handoff_invalid reason=operation_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
