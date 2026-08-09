#!/usr/bin/env python3
"""Generate a fail-closed, path-free release readiness report."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_artifact_build_identity import (
    ArtifactBuildIdentityError,
    verify_repository_artifact_build_identity,
)
from scripts.verify_product_version_truth import (
    ProductVersionTruthError,
    verify_product_version_truth,
)


SCHEMA_VERSION = "release-readiness/v1"
OBJECT = "release.readiness"
EVIDENCE_SCHEMA_VERSION = "release-readiness-evidence/v1"
MAX_EVIDENCE_BYTES = 64 * 1024
BLOCKERS = (
    "authoritative_release_verifier_missing",
    "candidate_not_release_eligible",
    "canary_evidence_missing",
    "clean_reference_performance_missing",
    "native_runner_evidence_missing",
    "protected_release_environment_unverified",
    "signing_notarization_evidence_missing",
    "ui_parity_evidence_missing",
    "windows_linux_native_evidence_missing",
)
ALLOWED_REASONS = frozenset(
    {
        "arguments_invalid",
        "evidence_invalid",
        "local_checks_invalid",
        "output_exists",
        "output_invalid",
        "output_write_failed",
    }
)


class ReleaseReadinessError(ValueError):
    """A sanitized release-readiness failure."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        _fail("arguments_invalid")


def _fail(reason: str) -> None:
    if reason not in ALLOWED_REASONS:
        reason = "local_checks_invalid"
    raise ReleaseReadinessError(reason)


def _exact_object(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        _fail("evidence_invalid")
    return value


def _load_evidence(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_EVIDENCE_BYTES
        ):
            _fail("evidence_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                _fail("evidence_invalid")
            raw = os.read(descriptor, MAX_EVIDENCE_BYTES + 1)
            if len(raw) != metadata.st_size:
                _fail("evidence_invalid")
        finally:
            os.close(descriptor)
        payload = json.loads(raw.decode("utf-8"))
    except ReleaseReadinessError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("evidence_invalid")
    return _validate_evidence(payload)


def _validate_evidence(payload: Any) -> dict[str, Any]:
    evidence = _exact_object(
        payload,
        (
            "canary",
            "cleanReferencePerformance",
            "nativeRunnerEvidence",
            "protectedReleaseEnvironment",
            "schemaVersion",
            "signing",
            "uiParity",
        ),
    )
    if evidence["schemaVersion"] != EVIDENCE_SCHEMA_VERSION:
        _fail("evidence_invalid")
    native = _exact_object(
        evidence["nativeRunnerEvidence"],
        ("linux", "mac", "windows"),
    )
    signing = _exact_object(
        evidence["signing"],
        (
            "linuxSignatureVerified",
            "macDeveloperIdNotarized",
            "windowsAuthenticodePublisherPinned",
        ),
    )
    canary = _exact_object(
        evidence["canary"],
        ("gatewayCohort", "observationCohort"),
    )
    ui = _exact_object(
        evidence["uiParity"],
        ("electron", "screenReader", "swift", "web"),
    )
    booleans = [
        evidence["cleanReferencePerformance"],
        evidence["protectedReleaseEnvironment"],
        *native.values(),
        *signing.values(),
        *canary.values(),
        *ui.values(),
    ]
    if any(type(value) is not bool for value in booleans):
        _fail("evidence_invalid")
    return evidence


def _empty_evidence() -> dict[str, Any]:
    return {
        "canary": {"gatewayCohort": False, "observationCohort": False},
        "cleanReferencePerformance": False,
        "nativeRunnerEvidence": {
            "linux": False,
            "mac": False,
            "windows": False,
        },
        "protectedReleaseEnvironment": False,
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "signing": {
            "linuxSignatureVerified": False,
            "macDeveloperIdNotarized": False,
            "windowsAuthenticodePublisherPinned": False,
        },
        "uiParity": {
            "electron": False,
            "screenReader": False,
            "swift": False,
            "web": False,
        },
    }


def _status(condition: bool) -> str:
    return "passed" if condition else "missing"


def build_report(root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    try:
        truth = verify_product_version_truth(root)
        verify_repository_artifact_build_identity(root)
    except (ProductVersionTruthError, ArtifactBuildIdentityError):
        _fail("local_checks_invalid")

    candidate = truth["candidate"]
    baseline = truth["publishedBaseline"]
    native = evidence["nativeRunnerEvidence"]
    signing = evidence["signing"]
    canary = evidence["canary"]
    ui = evidence["uiParity"]

    native_all = native["mac"] and native["windows"] and native["linux"]
    windows_linux = native["windows"] and native["linux"]
    signing_all = (
        signing["macDeveloperIdNotarized"]
        and signing["windowsAuthenticodePublisherPinned"]
        and signing["linuxSignatureVerified"]
    )
    canary_all = canary["observationCohort"] and canary["gatewayCohort"]
    ui_all = ui["web"] and ui["electron"] and ui["swift"] and ui["screenReader"]
    release_eligible = (
        candidate["releaseStage"] == "prerelease_ready"
        and candidate["publicationStatus"] == "not_published"
        and candidate["releaseEligible"] is True
        and candidate["publicationReceipt"] is None
    )

    blockers = ["authoritative_release_verifier_missing"]
    if not release_eligible:
        blockers.append("candidate_not_release_eligible")
    if not canary_all:
        blockers.append("canary_evidence_missing")
    if evidence["cleanReferencePerformance"] is not True:
        blockers.append("clean_reference_performance_missing")
    if not native_all:
        blockers.append("native_runner_evidence_missing")
    if evidence["protectedReleaseEnvironment"] is not True:
        blockers.append("protected_release_environment_unverified")
    if not signing_all:
        blockers.append("signing_notarization_evidence_missing")
    if not ui_all:
        blockers.append("ui_parity_evidence_missing")
    if not windows_linux:
        blockers.append("windows_linux_native_evidence_missing")

    return {
        "blockers": blockers,
        "decisionAuthority": "inventory_only",
        "externalEvidence": {
            "canary": _status(canary_all),
            "cleanReferencePerformance": _status(
                evidence["cleanReferencePerformance"] is True
            ),
            "nativeRunnerEvidence": _status(native_all),
            "protectedReleaseEnvironment": _status(
                evidence["protectedReleaseEnvironment"] is True
            ),
            "signingAndNotarization": _status(signing_all),
            "uiParity": _status(ui_all),
            "windowsLinuxNativeEvidence": _status(windows_linux),
        },
        "localChecks": {
            "artifactBuildIdentity": "passed",
            "productVersionTruth": "passed",
        },
        "object": OBJECT,
        "product": {
            "candidateBuild": candidate["build"],
            "candidateVersion": candidate["version"],
            "channel": candidate["channel"],
            "name": truth["product"]["displayName"],
            "publishedBaseline": baseline["tag"],
        },
        "releaseEligible": release_eligible,
        "schemaVersion": SCHEMA_VERSION,
        "status": "ready" if not blockers else "blocked",
    }


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _write_exclusive(path: Path, content: str) -> None:
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            _fail("output_exists")
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            encoded = content.encode("ascii")
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("output_write_failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ReleaseReadinessError:
        raise
    except (OSError, UnicodeError):
        _fail("output_write_failed")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(arguments: list[str]) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.output is not None:
            try:
                parsed.output.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                _fail("output_invalid")
            else:
                _fail("output_exists")
        if parsed.evidence is not None:
            _fail("arguments_invalid")
        evidence = _empty_evidence()
        report = build_report(parsed.root, evidence)
        raw = _canonical(report)
        if parsed.output is not None:
            _write_exclusive(parsed.output, raw)
    except ReleaseReadinessError as error:
        print(f"release_readiness_invalid reason={error}", file=sys.stderr)
        return 1

    blockers = len(report["blockers"])
    if report["status"] == "ready":
        print("release_readiness_ready blockers=0")
        return 0
    print(f"release_readiness_blocked blockers={blockers}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
