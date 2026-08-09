#!/usr/bin/env python3
"""Extract an electron-builder NSIS payload without running the installer."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


ARCHIVE_BY_ARCH = {
    "x64": r"$PLUGINSDIR\app-64.7z",
    "arm64": r"$PLUGINSDIR\app-arm64.7z",
}
REQUIRED_APP_MEMBERS = frozenset({
    "UsageHub.exe",
    "resources/app.asar",
    "resources/collector/openusage-collector.exe",
})
MAX_ARCHIVE_MEMBERS = 200_000
MAX_ARCHIVE_DEPTH = 64
MAX_INNER_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 15 * 60
SAFE_REASONS = frozenset({
    "architecture",
    "arguments",
    "extraction",
    "inner_layout",
    "installer",
    "outer_format",
    "outer_layout",
    "output",
    "tool",
    "tool_version",
})


class NsisPayloadError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ListedMember:
    path: str
    size: int


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise NsisPayloadError("arguments")


def expected_app_archive(arch: str) -> str:
    try:
        return ARCHIVE_BY_ARCH[arch]
    except KeyError as error:
        raise NsisPayloadError("architecture") from error


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("ascii", errors="replace")


def _technical_listing(
    output: bytes | str,
    *,
    expected_type: str,
    reason: str,
) -> list[dict[str, str]]:
    lines = _decode_output(output).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    separators = [index for index, line in enumerate(lines) if line == "----------"]
    if len(separators) != 1:
        raise NsisPayloadError(reason)
    separator = separators[0]

    archive_types = [
        line.removeprefix("Type = ")
        for line in lines[:separator]
        if line.startswith("Type = ")
    ]
    if archive_types != [expected_type]:
        raise NsisPayloadError(reason)

    members: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in lines[separator + 1 :]:
        if not line:
            if current:
                members.append(current)
                current = {}
            continue
        if " = " not in line:
            raise NsisPayloadError(reason)
        key, value = line.split(" = ", 1)
        if not key or key in current:
            raise NsisPayloadError(reason)
        current[key] = value
    if current:
        members.append(current)
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise NsisPayloadError(reason)
    return members


def _normalized_member_path(value: str, reason: str) -> str:
    if not value or "\0" in value:
        raise NsisPayloadError(reason)
    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(":" in part for part in raw_parts)
        or len(path.parts) > MAX_ARCHIVE_DEPTH
    ):
        raise NsisPayloadError(reason)
    return path.as_posix()


def _member_size(
    member: dict[str, str],
    reason: str,
    *,
    allow_empty: bool = False,
) -> int:
    raw_size = member.get("Size")
    if raw_size is None or re.fullmatch(r"[0-9]+", raw_size) is None:
        raise NsisPayloadError(reason)
    size = int(raw_size)
    if size < 0 or (size == 0 and not allow_empty) or size > MAX_INNER_ARCHIVE_BYTES:
        raise NsisPayloadError(reason)
    return size


def select_app_archive(output: bytes | str, arch: str) -> ListedMember:
    expected = expected_app_archive(arch)
    expected_normalized = _normalized_member_path(expected, "outer_layout")
    members = _technical_listing(
        output,
        expected_type="Nsis",
        reason="outer_format",
    )
    candidates: list[tuple[dict[str, str], str]] = []
    for member in members:
        raw_path = member.get("Path")
        if raw_path is None:
            raise NsisPayloadError("outer_layout")
        normalized = _normalized_member_path(raw_path, "outer_layout")
        if fnmatch.fnmatchcase(
            PurePosixPath(normalized).name.casefold(),
            "app-*.7z",
        ):
            candidates.append((member, normalized))
    if len(candidates) != 1 or candidates[0][1] != expected_normalized:
        raise NsisPayloadError("outer_layout")
    member, _normalized = candidates[0]
    if member.get("Folder") == "+" or "Symbolic Link" in member or "Hard Link" in member:
        raise NsisPayloadError("outer_layout")
    return ListedMember(path=member["Path"], size=_member_size(member, "outer_layout"))


def validate_inner_listing(output: bytes | str) -> None:
    members = _technical_listing(
        output,
        expected_type="7z",
        reason="inner_layout",
    )
    seen: set[str] = set()
    required: set[str] = set()
    total_size = 0
    for member in members:
        raw_path = member.get("Path")
        if raw_path is None:
            raise NsisPayloadError("inner_layout")
        normalized = _normalized_member_path(raw_path, "inner_layout")
        folded = normalized.casefold()
        if folded in seen:
            raise NsisPayloadError("inner_layout")
        seen.add(folded)
        if "Symbolic Link" in member or "Hard Link" in member:
            raise NsisPayloadError("inner_layout")
        is_folder = member.get("Folder") == "+"
        size = 0
        if not is_folder:
            size = _member_size(member, "inner_layout", allow_empty=True)
            total_size += size
            if total_size > MAX_INNER_ARCHIVE_BYTES:
                raise NsisPayloadError("inner_layout")
        if normalized in REQUIRED_APP_MEMBERS:
            if is_folder or size == 0:
                raise NsisPayloadError("inner_layout")
            required.add(normalized)
    if required != REQUIRED_APP_MEMBERS:
        raise NsisPayloadError("inner_layout")


def _run_seven_zip(
    seven_zip: Path,
    arguments: Sequence[str],
    *,
    reason: str,
) -> bytes:
    try:
        completed = subprocess.run(
            [str(seven_zip), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise NsisPayloadError(reason) from error
    if completed.returncode != 0:
        raise NsisPayloadError(reason)
    if isinstance(completed.stdout, str):
        return completed.stdout.encode("ascii", errors="replace")
    return completed.stdout or b""


def _sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise NsisPayloadError("installer") from error
    return digest.digest()


def _regular_file(path: Path, reason: str, *, expected_size: int | None = None) -> None:
    try:
        value = path.lstat()
    except OSError as error:
        raise NsisPayloadError(reason) from error
    if not stat.S_ISREG(value.st_mode) or value.st_size <= 0:
        raise NsisPayloadError(reason)
    if expected_size is not None and value.st_size != expected_size:
        raise NsisPayloadError(reason)


def _new_output_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if os.path.lexists(absolute):
        raise NsisPayloadError("output")
    try:
        parent = absolute.parent.lstat()
    except OSError as error:
        raise NsisPayloadError("output") from error
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise NsisPayloadError("output")
    return absolute


def _validate_outer_extraction(
    outer_root: Path,
    selected: ListedMember,
) -> Path:
    plugin_directory = outer_root / "$PLUGINSDIR"
    archive = plugin_directory / PurePosixPath(selected.path.replace("\\", "/")).name
    try:
        root_entries = list(os.scandir(outer_root))
        plugin_entries = list(os.scandir(plugin_directory))
        plugin_stat = plugin_directory.lstat()
    except OSError as error:
        raise NsisPayloadError("outer_layout") from error
    if (
        len(root_entries) != 1
        or root_entries[0].name != "$PLUGINSDIR"
        or stat.S_ISLNK(plugin_stat.st_mode)
        or not stat.S_ISDIR(plugin_stat.st_mode)
        or len(plugin_entries) != 1
        or plugin_entries[0].name != archive.name
    ):
        raise NsisPayloadError("outer_layout")
    _regular_file(archive, "outer_layout", expected_size=selected.size)
    return archive


def _validate_extracted_app(output: Path) -> None:
    try:
        output_stat = output.lstat()
    except OSError as error:
        raise NsisPayloadError("inner_layout") from error
    if stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode):
        raise NsisPayloadError("inner_layout")
    for relative in REQUIRED_APP_MEMBERS:
        _regular_file(output / PurePosixPath(relative), "inner_layout")


def _verify_seven_zip_version(
    seven_zip: Path,
    expected_version: str,
) -> None:
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2}", expected_version) is None:
        raise NsisPayloadError("tool_version")
    output = _run_seven_zip(seven_zip, ("i",), reason="tool_version")
    banner = _decode_output(output)
    pattern = rf"(?m)^7-Zip(?: \(z\))? {re.escape(expected_version)}(?:\s|$)"
    if re.search(pattern, banner) is None:
        raise NsisPayloadError("tool_version")


def extract_payload(
    *,
    seven_zip: Path,
    installer: Path,
    arch: str,
    output: Path,
    expected_seven_zip_version: str,
) -> Path:
    expected_app_archive(arch)
    seven_zip = Path(os.path.abspath(seven_zip))
    installer = Path(os.path.abspath(installer))
    output = _new_output_path(output)
    _regular_file(seven_zip, "tool")
    _regular_file(installer, "installer")
    if seven_zip.is_symlink():
        raise NsisPayloadError("tool")
    if installer.is_symlink() or installer.suffix.casefold() != ".exe":
        raise NsisPayloadError("installer")
    seven_zip = seven_zip.resolve(strict=True)
    installer = installer.resolve(strict=True)

    installer_digest = _sha256(installer)
    _verify_seven_zip_version(seven_zip, expected_seven_zip_version)
    outer_listing = _run_seven_zip(
        seven_zip,
        ("l", "-slt", "-tNsis", str(installer)),
        reason="outer_format",
    )
    selected = select_app_archive(outer_listing, arch)

    with tempfile.TemporaryDirectory(prefix="openusage-nsis-") as directory:
        outer_root = Path(directory)
        _run_seven_zip(
            seven_zip,
            (
                "x",
                "-bd",
                "-y",
                "-tNsis",
                f"-o{outer_root}",
                str(installer),
                selected.path,
            ),
            reason="extraction",
        )
        inner_archive = _validate_outer_extraction(outer_root, selected)
        inner_listing = _run_seven_zip(
            seven_zip,
            ("l", "-slt", "-t7z", str(inner_archive)),
            reason="inner_layout",
        )
        validate_inner_listing(inner_listing)
        try:
            output.mkdir(mode=0o700)
        except OSError as error:
            raise NsisPayloadError("output") from error
        _run_seven_zip(
            seven_zip,
            ("x", "-bd", "-y", "-t7z", f"-o{output}", str(inner_archive)),
            reason="extraction",
        )

    _validate_extracted_app(output)
    if _sha256(installer) != installer_digest:
        raise NsisPayloadError("installer")
    return output.resolve(strict=True)


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--seven-zip", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=tuple(ARCHIVE_BY_ARCH))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-seven-zip-version", required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        options = _parser().parse_args(arguments)
        extract_payload(
            seven_zip=options.seven_zip,
            installer=options.installer,
            arch=options.arch,
            output=options.output,
            expected_seven_zip_version=options.expected_seven_zip_version,
        )
    except NsisPayloadError as error:
        reason = error.reason if error.reason in SAFE_REASONS else "unknown"
        print(f"nsis_payload_invalid reason={reason}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError):
        print("nsis_payload_invalid reason=unknown", file=sys.stderr)
        return 1
    print("nsis_payload_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
