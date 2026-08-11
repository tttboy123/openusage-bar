#!/usr/bin/env python3
"""Generate and verify closed native final-container lifecycle evidence.

The evidence is deliberately a release non-claim.  ``generate`` executes an
independent platform driver; it never accepts a JSON assertion or trusts output
from the NSIS/AppImage under test as the lifecycle result.  An injected
executor exists only for unit tests of the assembler boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform_module
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


SCHEMA_VERSION = "native-lifecycle-evidence/v1"
OBJECT = "native.lifecycle"
MAX_REPORT_BYTES = 64 * 1024
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
OBSERVED_AT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
ARTIFACT_NAMES = {
    "win": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-win-x64\.exe\Z"
    ),
    "linux": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-linux-x86_64\.AppImage\Z"
    ),
}
SERVICE_MANAGERS = {"win": "task_scheduler", "linux": "systemd_user"}
CHECK_NAMES = (
    "install",
    "firstRun",
    "observeDefault",
    "serviceRegistered",
    "uninstallPreserve",
    "serviceRemoved",
    "reinstall",
    "uninstallDelete",
    "finalStateRemoved",
)
ROOT_KEYS = frozenset(
    {
        "schemaVersion",
        "object",
        "synthetic",
        "releaseEligible",
        "observedAt",
        "sourceCommit",
        "target",
        "artifact",
        "checks",
        "gateway",
        "privacy",
        "persistence",
    }
)
OBSERVATION_KEYS = frozenset(
    {"checks", "gateway", "privacy", "persistence"}
)
ALLOWED_ERRORS = frozenset(
    {
        "arguments_invalid",
        "artifact_changed",
        "artifact_invalid",
        "artifact_unavailable",
        "binding_invalid",
        "driver_failed",
        "driver_unavailable",
        "execution_not_real",
        "host_invalid",
        "operation_failed",
        "output_exists",
        "output_write_failed",
        "record_invalid",
        "report_invalid",
        "report_not_canonical",
        "report_unavailable",
    }
)


class LifecycleEvidenceError(ValueError):
    """A path-free, allowlisted lifecycle evidence error."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Collapse parser diagnostics so hostile values never reach stderr."""

    def error(self, message: str) -> None:
        del message
        _fail("arguments_invalid")


def _fail(reason: str) -> None:
    if reason not in ALLOWED_ERRORS:
        reason = "operation_failed"
    raise LifecycleEvidenceError(reason)


def _exact_keys(value: object, keys: frozenset[str] | tuple[str, ...]) -> bool:
    return type(value) is dict and set(value) == set(keys)


def _valid_observed_at(value: object) -> bool:
    if type(value) is not str or OBSERVED_AT.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return parsed.tzinfo is None


def validate_lifecycle_record(record: object) -> dict[str, Any]:
    """Validate and return one closed Windows/Linux x64 lifecycle record."""

    if not _exact_keys(record, ROOT_KEYS):
        _fail("record_invalid")
    assert isinstance(record, dict)
    if (
        type(record["schemaVersion"]) is not str
        or record["schemaVersion"] != SCHEMA_VERSION
        or type(record["object"]) is not str
        or record["object"] != OBJECT
        or record["synthetic"] is not False
        or record["releaseEligible"] is not False
        or not _valid_observed_at(record["observedAt"])
        or type(record["sourceCommit"]) is not str
        or SHA1.fullmatch(record["sourceCommit"]) is None
    ):
        _fail("record_invalid")

    target = record["target"]
    if not _exact_keys(target, ("platform", "arch", "serviceManager")):
        _fail("record_invalid")
    platform = target["platform"]
    if (
        type(platform) is not str
        or platform not in SERVICE_MANAGERS
        or type(target["arch"]) is not str
        or target["arch"] != "x64"
        or type(target["serviceManager"]) is not str
        or target["serviceManager"] != SERVICE_MANAGERS[platform]
    ):
        _fail("record_invalid")

    artifact = record["artifact"]
    if not _exact_keys(artifact, ("name", "sha256", "sizeBytes")):
        _fail("record_invalid")
    if (
        type(artifact["name"]) is not str
        or ARTIFACT_NAMES[platform].fullmatch(artifact["name"]) is None
        or type(artifact["sha256"]) is not str
        or SHA256.fullmatch(artifact["sha256"]) is None
        or type(artifact["sizeBytes"]) is not int
        or isinstance(artifact["sizeBytes"], bool)
        or artifact["sizeBytes"] <= 0
    ):
        _fail("record_invalid")

    checks = record["checks"]
    if not _exact_keys(checks, CHECK_NAMES) or any(
        type(checks[name]) is not str or checks[name] != "passed"
        for name in CHECK_NAMES
    ):
        _fail("record_invalid")

    gateway = record["gateway"]
    if not _exact_keys(
        gateway,
        ("defaultMode", "listenerActive", "cacheCreated", "telemetryCreated"),
    ) or (
        type(gateway["defaultMode"]) is not str
        or gateway["defaultMode"] != "observe"
        or gateway["listenerActive"] is not False
        or gateway["cacheCreated"] is not False
        or gateway["telemetryCreated"] is not False
    ):
        _fail("record_invalid")

    privacy = record["privacy"]
    if not _exact_keys(
        privacy, ("providerCredentialReads", "providerNetworkCalls")
    ) or any(
        type(privacy[name]) is not int or privacy[name] != 0
        for name in ("providerCredentialReads", "providerNetworkCalls")
    ):
        _fail("record_invalid")

    persistence = record["persistence"]
    if not _exact_keys(
        persistence,
        (
            "ledgerOnPreserve",
            "credentialsOnPreserve",
            "gatewayCacheOnPreserve",
            "gatewayTelemetryOnPreserve",
            "stateAfterDelete",
        ),
    ) or any(type(persistence[name]) is not str for name in persistence) or persistence != {
        "ledgerOnPreserve": "preserved",
        "credentialsOnPreserve": "not_created",
        "gatewayCacheOnPreserve": "not_created",
        "gatewayTelemetryOnPreserve": "not_created",
        "stateAfterDelete": "removed",
    }:
        _fail("record_invalid")
    return record


def _canonical(record: dict[str, Any]) -> str:
    try:
        return json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError):
        _fail("record_invalid")


def _inspect_artifact(
    path: Path, *, expected_platform: str
) -> tuple[dict[str, Any], tuple[int, int, int, int, int]]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or ARTIFACT_NAMES[expected_platform].fullmatch(path.name) is None
        ):
            _fail("artifact_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        digest = hashlib.sha256()
        total = 0
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            ):
                _fail("artifact_invalid")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except LifecycleEvidenceError:
        raise
    except OSError:
        _fail("artifact_unavailable")
    if (
        total != metadata.st_size
        or (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        )
        != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    ):
        _fail("artifact_changed")
    return (
        {
            "name": path.name,
            "sha256": digest.hexdigest(),
            "sizeBytes": total,
        },
        (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        ),
    )


def _artifact_record(path: Path, *, expected_platform: str) -> dict[str, Any]:
    return _inspect_artifact(path, expected_platform=expected_platform)[0]


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
        raw = content.encode("ascii")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("output_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError:
        _fail("output_exists")
    except LifecycleEvidenceError:
        raise
    except (OSError, UnicodeError):
        _fail("output_write_failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


class LifecycleExecutor(Protocol):
    execution_class: str

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]: ...


class PlatformLifecycleDriver(Protocol):
    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]: ...


class _BuiltInPlatformDriver:
    """Independent native lifecycle driver.

    The current product does not yet expose an honest cross-platform delete
    lifecycle seam.  Refuse evidence rather than converting partial install or
    first-launch observations into nine passing checks.  The product seam is
    intentionally completed before this driver can emit a record.
    """

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]:
        del platform, arch, artifact, artifact_sha256
        _fail("driver_unavailable")


class NativeLifecycleExecutor:
    """Host-bound adapter around the independent platform lifecycle driver."""

    execution_class = "external_platform_driver"

    def __init__(
        self,
        *,
        driver: PlatformLifecycleDriver | None = None,
        host_platform: str | None = None,
        host_machine: str | None = None,
    ) -> None:
        self._driver = driver if driver is not None else _BuiltInPlatformDriver()
        self._host_platform = host_platform if host_platform is not None else sys.platform
        self._host_machine = (
            host_machine
            if host_machine is not None
            else host_platform_module.machine()
        )

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]:
        expected_host = "win32" if platform == "win" else "linux"
        machine = self._host_machine.casefold()
        if (
            self._host_platform != expected_host
            and not (
                expected_host == "linux"
                and self._host_platform.startswith("linux")
            )
        ) or arch != "x64" or machine not in {"x86_64", "amd64"}:
            _fail("host_invalid")
        try:
            observations = self._driver.execute(
                platform=platform,
                arch=arch,
                artifact=artifact,
                artifact_sha256=artifact_sha256,
            )
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_failed")
        if not _exact_keys(observations, OBSERVATION_KEYS):
            _fail("driver_failed")
        return observations


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed_at(clock: Callable[[], datetime]) -> str:
    try:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            _fail("record_invalid")
        utc = value.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except LifecycleEvidenceError:
        raise
    except Exception:
        _fail("record_invalid")


def generate_lifecycle_evidence(
    *,
    platform: str,
    arch: str,
    artifact: str | Path,
    source_commit: str,
    output: str | Path,
    executor: LifecycleExecutor | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Execute the native lifecycle, then exclusively write its record."""

    if platform not in SERVICE_MANAGERS or arch != "x64":
        _fail("arguments_invalid")
    if type(source_commit) is not str or SHA1.fullmatch(source_commit) is None:
        _fail("arguments_invalid")
    artifact_path = Path(artifact)
    output_path = Path(output)
    before, before_signature = _inspect_artifact(
        artifact_path, expected_platform=platform
    )
    active_executor = executor if executor is not None else NativeLifecycleExecutor()
    expected_execution_class = (
        "unit_test_injected" if executor is not None else "external_platform_driver"
    )
    if active_executor.execution_class != expected_execution_class:
        _fail("execution_not_real")
    try:
        observations = active_executor.execute(
            platform=platform,
            arch=arch,
            artifact=artifact_path,
            artifact_sha256=before["sha256"],
        )
    except LifecycleEvidenceError:
        raise
    except Exception:
        _fail("driver_failed")
    after, after_signature = _inspect_artifact(
        artifact_path, expected_platform=platform
    )
    if after != before or after_signature != before_signature:
        _fail("artifact_changed")
    if not _exact_keys(observations, OBSERVATION_KEYS):
        _fail("record_invalid")
    record: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "object": OBJECT,
        "synthetic": False,
        "releaseEligible": False,
        "observedAt": _observed_at(clock),
        "sourceCommit": source_commit,
        "target": {
            "platform": platform,
            "arch": arch,
            "serviceManager": SERVICE_MANAGERS[platform],
        },
        "artifact": before,
        **observations,
    }
    validate_lifecycle_record(record)
    content = _canonical(record)
    if len(content.encode("ascii")) > MAX_REPORT_BYTES:
        _fail("record_invalid")
    _write_exclusive(output_path, content)
    return record


def _read_canonical_report(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_REPORT_BYTES
        ):
            _fail("report_unavailable")
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
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            ):
                _fail("report_unavailable")
            raw = os.read(descriptor, MAX_REPORT_BYTES + 1)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(raw) != metadata.st_size
            or (
                finished.st_dev,
                finished.st_ino,
                finished.st_size,
                finished.st_mtime_ns,
                finished.st_ctime_ns,
            )
            != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        ):
            _fail("report_unavailable")
        text = raw.decode("ascii")
        value = json.loads(text)
    except LifecycleEvidenceError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        MemoryError,
        RecursionError,
    ):
        _fail("report_invalid")
    record = validate_lifecycle_record(value)
    if text != _canonical(record):
        _fail("report_not_canonical")
    return record


def verify_lifecycle_evidence(
    *,
    report: str | Path,
    artifact: str | Path,
    expected_source_commit: str,
    expected_platform: str,
    expected_arch: str,
) -> dict[str, Any]:
    """Rehash the final container and bind a canonical lifecycle record."""

    if (
        expected_platform not in SERVICE_MANAGERS
        or expected_arch != "x64"
        or type(expected_source_commit) is not str
        or SHA1.fullmatch(expected_source_commit) is None
    ):
        _fail("arguments_invalid")
    record = _read_canonical_report(Path(report))
    artifact_record = _artifact_record(
        Path(artifact), expected_platform=expected_platform
    )
    if (
        record["sourceCommit"] != expected_source_commit
        or record["target"]
        != {
            "platform": expected_platform,
            "arch": expected_arch,
            "serviceManager": SERVICE_MANAGERS[expected_platform],
        }
        or record["artifact"] != artifact_record
    ):
        _fail("binding_invalid")
    return record


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="native_lifecycle_evidence.py")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--platform", choices=tuple(SERVICE_MANAGERS), required=True)
    generate.add_argument("--arch", choices=("x64",), required=True)
    generate.add_argument("--artifact", required=True)
    generate.add_argument("--source-commit", required=True)
    generate.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--report", required=True)
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--expected-source-commit", required=True)
    verify.add_argument("--expected-platform", choices=tuple(SERVICE_MANAGERS), required=True)
    verify.add_argument("--expected-arch", choices=("x64",), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "generate":
            # CLI generation is intentionally not injectable.  Only the
            # built-in independent platform driver may create CI evidence.
            record = generate_lifecycle_evidence(
                platform=arguments.platform,
                arch=arguments.arch,
                artifact=arguments.artifact,
                source_commit=arguments.source_commit,
                output=arguments.output,
            )
            action = "generated"
        else:
            record = verify_lifecycle_evidence(
                report=arguments.report,
                artifact=arguments.artifact,
                expected_source_commit=arguments.expected_source_commit,
                expected_platform=arguments.expected_platform,
                expected_arch=arguments.expected_arch,
            )
            action = "verified"
        print(
            f"native_lifecycle_{action} "
            f"{record['target']['platform']}/{record['target']['arch']} "
            "synthetic=false releaseEligible=false"
        )
        return 0
    except LifecycleEvidenceError as error:
        print(f"native_lifecycle_invalid reason={error}", file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception:
        print("native_lifecycle_invalid reason=operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
