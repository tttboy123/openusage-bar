#!/usr/bin/env python3
"""Observe platform distribution trust without making a release claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openusage_bar.bounded_process import run_bounded


POLICY = "distribution-trust-posture/v1"
OBJECT = "distribution.trust_posture"
MAX_REPORT_BYTES = 64 * 1024
MAX_TOOL_OUTPUT_BYTES = 64 * 1024
TOOL_TIMEOUT_SECONDS = 10.0
_SAFE_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+ -]{0,159}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_NAMES = {
    "mac": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-mac-(?:arm64|x64)\.dmg"
    ),
    "win": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-win-(?:arm64|x64)\.exe"
    ),
    "linux": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-linux-(?:arm64|x64|x86_64)\.AppImage"
    ),
}
_ROOT_KEYS = frozenset(
    {
        "artifact",
        "object",
        "platformCodeSigning",
        "platformNotarization",
        "policy",
        "provenanceAttestation",
        "releaseEligible",
        "schemaVersion",
        "targetPlatform",
    }
)
_ARTIFACT_KEYS = frozenset({"name", "sha256", "sizeBytes"})
_SIGNING_ENUMS = {
    "mac": frozenset(
        {
            "ad_hoc_strict_invalid",
            "ad_hoc_strict_valid",
            "developer_id_style_strict_invalid",
            "developer_id_style_strict_valid",
            "other_style_strict_invalid",
            "other_style_strict_valid",
            "unknown",
            "unsigned",
        }
    ),
    "win": frozenset({"invalid", "unknown", "unsigned", "valid"}),
    "linux": frozenset({"not_applicable"}),
}
_NOTARIZATION_ENUMS = {
    "mac": frozenset({"not_stapled", "stapled", "unknown"}),
    "win": frozenset({"not_applicable"}),
    "linux": frozenset({"not_applicable"}),
}

Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _inspection_error() -> ValueError:
    return ValueError("distribution trust inspection failed")


def _verification_error() -> ValueError:
    return ValueError("distribution trust posture invalid")


def _validate_package_root(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise _inspection_error() from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _inspection_error()


def _artifact_format_valid(
    handle: BinaryIO,
    *,
    platform: str,
    name: str,
) -> bool:
    handle.seek(0)
    prefix = handle.read(4096)
    if platform == "mac":
        if len(prefix) < 512:
            return False
        handle.seek(-512, os.SEEK_END)
        return handle.read(4) == b"koly"
    if platform == "win":
        if len(prefix) < 64 or prefix[:2] != b"MZ":
            return False
        pe_offset = int.from_bytes(prefix[60:64], "little")
        return (
            pe_offset <= len(prefix) - 4
            and prefix[pe_offset : pe_offset + 4] == b"PE\0\0"
        )
    if len(prefix) < 20 or prefix[:7] != b"\x7fELF\x02\x01\x01":
        return False
    machine = int.from_bytes(prefix[18:20], "little")
    expected = 183 if "-arm64.AppImage" in name else 62
    return machine == expected


def _artifact_identity(
    path: Path,
    *,
    inspection: bool,
    platform: str,
) -> dict[str, object]:
    error = _inspection_error if inspection else _verification_error
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise error()
        if (
            platform not in _ARTIFACT_NAMES
            or metadata.st_size <= 0
            or _SAFE_ARTIFACT_NAME.fullmatch(path.name) is None
            or _ARTIFACT_NAMES[platform].fullmatch(path.name) is None
        ):
            raise error()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise error()
            if not _artifact_format_valid(
                handle,
                platform=platform,
                name=path.name,
            ):
                raise error()
            handle.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            finished = os.fstat(handle.fileno())
            if (
                finished.st_dev != opened.st_dev
                or finished.st_ino != opened.st_ino
                or finished.st_size != opened.st_size
            ):
                raise error()
    except ValueError:
        raise
    except (OSError, OverflowError):
        raise error() from None
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "sizeBytes": metadata.st_size,
    }


def _bounded_text(value: object) -> str | None:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif isinstance(value, str):
        text = value
    else:
        return None
    if len(text.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
        return None
    return text


def _run_tool(
    runner: Runner,
    command: Sequence[str],
) -> tuple[int, str, str] | None:
    try:
        completed = runner(
            list(command),
            timeout=TOOL_TIMEOUT_SECONDS,
            stdout_limit=MAX_TOOL_OUTPUT_BYTES,
            stderr_limit=MAX_TOOL_OUTPUT_BYTES,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = _bounded_text(completed.stdout)
        stderr = _bounded_text(completed.stderr)
        if (
            type(completed.returncode) is not int
            or stdout is None
            or stderr is None
        ):
            return None
        return completed.returncode, stdout, stderr
    except Exception:
        return None


def _mac_observation(
    package_root: Path,
    artifact: Path,
    runner: Runner,
) -> tuple[str, str]:
    display = _run_tool(
        runner,
        (
            "/usr/bin/codesign",
            "--display",
            "--verbose=4",
            str(package_root),
        ),
    )
    verification = _run_tool(
        runner,
        (
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            str(package_root),
        ),
    )
    stapler = _run_tool(
        runner,
        (
            "/usr/bin/xcrun",
            "stapler",
            "validate",
            str(artifact),
        ),
    )

    if display is None or verification is None:
        signing = "unknown"
    elif display[0] != 0:
        display_failure = f"{display[1]}\n{display[2]}".casefold()
        signing = (
            "unsigned"
            if "not signed at all" in display_failure
            else "unknown"
        )
    else:
        display_text = f"{display[1]}\n{display[2]}".casefold()
        if "signature=adhoc" in display_text:
            style = "ad_hoc"
        elif "authority=developer id application:" in display_text:
            style = "developer_id_style"
        elif "authority=" in display_text:
            style = "other_style"
        else:
            style = "unknown"
        if style == "unknown":
            signing = "unknown"
        else:
            validity = (
                "strict_valid" if verification[0] == 0 else "strict_invalid"
            )
            signing = f"{style}_{validity}"

    if stapler is None:
        notarization = "unknown"
    else:
        if stapler[0] == 0:
            notarization = "stapled"
        else:
            stapler_failure = f"{stapler[1]}\n{stapler[2]}".casefold()
            notarization = (
                "not_stapled"
                if "does not have a ticket stapled" in stapler_failure
                or "not stapled" in stapler_failure
                else "unknown"
            )
    return signing, notarization


def _windows_observation(artifact: Path, runner: Runner) -> tuple[str, str]:
    command = (
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$ErrorActionPreference='Stop';"
            "$signature=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature "
            "-LiteralPath $args[0];"
            "[Console]::Out.Write($signature.Status.ToString())"
        ),
        str(artifact),
    )
    outcome = _run_tool(runner, command)
    if outcome is None or outcome[0] != 0:
        signing = "unknown"
    else:
        values = outcome[1].splitlines()
        if len(values) != 1:
            signing = "unknown"
        else:
            signing = {
                "Valid": "valid",
                "NotSigned": "unsigned",
                "HashMismatch": "invalid",
                "NotTrusted": "invalid",
                "UnknownError": "invalid",
            }.get(values[0].strip(), "unknown")
    return signing, "not_applicable"


def _mac_package_bound_to_artifact(
    package_root: Path,
    artifact: Path,
    runner: Runner,
) -> bool:
    outcome = _run_tool(
        runner,
        ("/usr/bin/hdiutil", "info", "-plist"),
    )
    if outcome is None or outcome[0] != 0 or outcome[2]:
        return False
    try:
        payload = plistlib.loads(outcome[1].encode("utf-8"))
        artifact_path = artifact.resolve(strict=True)
        package_path = package_root.resolve(strict=True)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    if type(payload) is not dict or type(payload.get("images")) is not list:
        return False
    for image in payload["images"]:
        if (
            type(image) is not dict
            or image.get("writeable") is not False
            or type(image.get("image-path")) is not str
            or type(image.get("system-entities")) is not list
        ):
            continue
        try:
            image_path = Path(image["image-path"]).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if image_path != artifact_path:
            continue
        for entity in image["system-entities"]:
            if type(entity) is not dict or type(entity.get("mount-point")) is not str:
                continue
            try:
                mounted_app = (
                    Path(entity["mount-point"]).resolve(strict=True)
                    / "UsageHub.app"
                ).resolve(strict=True)
            except (OSError, ValueError):
                continue
            if mounted_app == package_path:
                return True
    return False


def inspect_distribution_trust(
    *,
    platform: str,
    package_root: str | Path,
    artifact: str | Path,
    runner: Runner = run_bounded,
) -> dict[str, object]:
    """Return a path-free observation bound to the final container bytes."""

    if platform not in _SIGNING_ENUMS:
        raise _inspection_error()
    package_path = Path(package_root)
    artifact_path = Path(artifact)
    _validate_package_root(package_path)
    before = _artifact_identity(
        artifact_path,
        inspection=True,
        platform=platform,
    )

    if platform == "mac":
        signing, notarization = _mac_observation(
            package_path, artifact_path, runner
        )
    elif platform == "win":
        signing, notarization = _windows_observation(artifact_path, runner)
    else:
        signing, notarization = "not_applicable", "not_applicable"

    after = _artifact_identity(
        artifact_path,
        inspection=True,
        platform=platform,
    )
    if before != after:
        raise _inspection_error()
    return {
        "artifact": before,
        "object": OBJECT,
        "platformCodeSigning": signing,
        "platformNotarization": notarization,
        "policy": POLICY,
        "provenanceAttestation": "not_verified",
        "releaseEligible": False,
        "schemaVersion": POLICY,
        "targetPlatform": platform,
    }


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _verification_error()
        value[key] = item
    return value


def _read_report(path: Path) -> tuple[dict[str, object], str]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_REPORT_BYTES
        ):
            raise _verification_error()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise _verification_error()
            raw_bytes = handle.read(MAX_REPORT_BYTES + 1)
        if len(raw_bytes) != metadata.st_size or len(raw_bytes) > MAX_REPORT_BYTES:
            raise _verification_error()
        raw = raw_bytes.decode("utf-8")
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _verification_error()
            ),
        )
    except ValueError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise _verification_error() from None
    if type(payload) is not dict:
        raise _verification_error()
    return payload, raw


def _validate_payload(
    payload: dict[str, object],
    *,
    platform: str,
    identity: dict[str, object],
) -> None:
    if platform not in _SIGNING_ENUMS or set(payload) != _ROOT_KEYS:
        raise _verification_error()
    artifact = payload.get("artifact")
    if type(artifact) is not dict or set(artifact) != _ARTIFACT_KEYS:
        raise _verification_error()
    artifact_name = artifact.get("name")
    artifact_sha256 = artifact.get("sha256")
    artifact_size = artifact.get("sizeBytes")
    if (
        type(artifact_name) is not str
        or _SAFE_ARTIFACT_NAME.fullmatch(artifact_name) is None
        or type(artifact_sha256) is not str
        or _SHA256.fullmatch(artifact_sha256) is None
        or type(artifact_size) is not int
        or artifact_size <= 0
        or artifact != identity
        or payload.get("object") != OBJECT
        or payload.get("policy") != POLICY
        or payload.get("schemaVersion") != POLICY
        or payload.get("targetPlatform") != platform
        or payload.get("provenanceAttestation") != "not_verified"
        or type(payload.get("releaseEligible")) is not bool
        or payload.get("releaseEligible") is not False
    ):
        raise _verification_error()
    signing = payload.get("platformCodeSigning")
    notarization = payload.get("platformNotarization")
    if (
        type(signing) is not str
        or signing not in _SIGNING_ENUMS[platform]
        or type(notarization) is not str
        or notarization not in _NOTARIZATION_ENUMS[platform]
    ):
        raise _verification_error()


def verify_distribution_trust(
    *,
    report: str | Path,
    platform: str,
    artifact: str | Path,
) -> dict[str, object]:
    """Verify one canonical report against the current artifact bytes."""

    try:
        identity = _artifact_identity(
            Path(artifact),
            inspection=False,
            platform=platform,
        )
        payload, raw = _read_report(Path(report))
        _validate_payload(payload, platform=platform, identity=identity)
        if raw != _canonical(payload):
            raise _verification_error()
        return payload
    except ValueError as error:
        if str(error) == "distribution trust posture invalid":
            raise
        raise _verification_error() from None
    except Exception:
        raise _verification_error() from None


def _write_report(path: Path, content: str) -> None:
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise _inspection_error()
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("ascii"))
    except ValueError:
        raise
    except OSError:
        raise _inspection_error() from None


def write_distribution_trust_report(
    *,
    platform: str,
    package_root: str | Path,
    artifact: str | Path,
    output: str | Path,
    runner: Runner = run_bounded,
) -> dict[str, object]:
    """Inspect and persist only an operational, artifact-bound probe."""

    package_path = Path(package_root)
    artifact_path = Path(artifact)
    _validate_package_root(package_path)
    _artifact_identity(
        artifact_path,
        inspection=True,
        platform=platform,
    )
    if platform == "mac" and not _mac_package_bound_to_artifact(
        package_path,
        artifact_path,
        runner,
    ):
        raise _inspection_error()
    report = inspect_distribution_trust(
        platform=platform,
        package_root=package_path,
        artifact=artifact_path,
        runner=runner,
    )
    if report["platformCodeSigning"] == "unknown" or (
        platform == "mac" and report["platformNotarization"] == "unknown"
    ):
        raise _inspection_error()
    _write_report(Path(output), _canonical(report))
    return report


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("distribution trust posture invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument(
        "--platform", required=True, choices=tuple(_SIGNING_ENUMS)
    )
    inspect_parser.add_argument("--package-root", required=True)
    inspect_parser.add_argument("--artifact", required=True)
    inspect_parser.add_argument("--output", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument(
        "--platform", required=True, choices=tuple(_SIGNING_ENUMS)
    )
    verify_parser.add_argument("--report", required=True)
    verify_parser.add_argument("--artifact", required=True)
    return parser


def main(arguments: list[str]) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command == "inspect":
            write_distribution_trust_report(
                platform=parsed.platform,
                package_root=parsed.package_root,
                artifact=parsed.artifact,
                output=parsed.output,
            )
        else:
            verify_distribution_trust(
                report=parsed.report,
                platform=parsed.platform,
                artifact=parsed.artifact,
            )
    except (ValueError, OSError):
        print("distribution_trust_posture_invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
