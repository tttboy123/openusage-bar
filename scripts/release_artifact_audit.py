#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import mmap
import os
import posixpath
import plistlib
import re
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_artifact_build_identity import (
    ArtifactBuildIdentityError,
    CANONICAL_IDENTITY,
    PACKAGED_IDENTITY_NAME,
    PRODUCT_TRUTH,
    verify_packaged_build_identity,
)


ARCHIVE_PATTERN = re.compile(r"^OpenUsage-Bar-v(\d+\.\d+\.\d+)-macos-arm64\.zip$")
ALLOWED_SCRIPTS = frozenset({
    "activity_schema.py",
    "activity_install_process.sh", "install_app.sh",
    "install_location.sh", "install_app_transaction.sh", "rollback_app.sh",
    "uninstall_app.sh",
    "export_diagnostics.py", "privacy_scan.py", "verify_canary_surfaces.py",
    "verify_canary_candidate.py",
    "verify_local_api.py",
})
SENSITIVE_NAMES = frozenset({
    ".env", "providers.json", "gateway.json", "api.token", "gateway.token",
    "openusage.token", "activity.sqlite3", "activity.sqlite3-wal",
    "activity.sqlite3-shm", "gateway-cache.sqlite3",
    "gateway-cache.sqlite3-wal", "gateway-cache.sqlite3-shm",
    "gateway-telemetry.sqlite3", "gateway-telemetry.sqlite3-wal",
    "gateway-telemetry.sqlite3-shm", "keychain", "cookies", "credentials",
})
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_SYMLINK_TARGET_BYTES = 4096
MAX_DESKTOP_MEMBERS = 200_000
MAX_DESKTOP_DEPTH = 64
MAX_DESKTOP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_DESKTOP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
DESKTOP_SCAN_CHUNK_BYTES = 1024 * 1024
DESKTOP_SCAN_OVERLAP_BYTES = 512
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
HOME_PATH_PATTERN = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)")
DESKTOP_POSIX_HOME_PATH_PATTERN = re.compile(
    rb"/(?:Users|home)/[A-Za-z0-9._-]{1,255}/[^/\x00]"
)
WINDOWS_HOME_PATH_PATTERN = re.compile(
    rb"[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9._ -]{1,128}"
    rb"[\\/]+[^\\/\x00]",
    re.IGNORECASE,
)
DESKTOP_DATABASE_PATTERN = re.compile(
    r"(?:activity|gateway-cache|gateway-telemetry)\.sqlite3"
    r"(?:-(?:wal|shm)|-journal)?\Z",
    re.IGNORECASE,
)
RAW_CONTENT_CANARIES = (
    b"OPENUSAGE_RAW_PROMPT_CANARY_",
    b"OPENUSAGE_RAW_RESPONSE_CANARY_",
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_ASAR_HEADER_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_METADATA_BYTES = 64 * 1024
MAX_DESKTOP_ENTRY_BYTES = 64 * 1024


class ArtifactError(ValueError):
    def __init__(self, reason: str, member: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.member = member


SAFE_REASONS = frozenset({
    "archive", "architecture", "binary", "build_identity", "checksum",
    "collector", "home_path", "native_metadata", "oversized", "plist",
    "private_material", "signature", "symlink", "unexpected_member",
    "unsafe_path", "version_mismatch",
})


def _member_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def inspect_members(archive: zipfile.ZipFile, expected_root: str) -> None:
    total = 0
    seen: set[str] = set()
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name or name in seen or name.startswith("/")
            or ".." in path.parts or path.parts[0] != expected_root
        ):
            raise ArtifactError("unsafe_path")
        seen.add(name)
        total += info.file_size
        if (
            info.file_size < 0
            or info.file_size > MAX_MEMBER_BYTES
            or total > MAX_TOTAL_BYTES
        ):
            raise ArtifactError("oversized")
        mode = _member_mode(info)
        if stat.S_ISLNK(mode):
            if info.file_size > MAX_SYMLINK_TARGET_BYTES:
                raise ArtifactError("oversized")
            try:
                target = archive.read(info).decode("utf-8")
            except (UnicodeError, KeyError) as error:
                raise ArtifactError("symlink") from error
            app_prefix = f"{expected_root}/dist/OpenUsage Bar.app/"
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if target.startswith("/") or not name.startswith(app_prefix) or not resolved.startswith(app_prefix):
                raise ArtifactError("symlink")
            continue
        relative = path.parts[1:]
        if not relative:
            continue
        if info.is_dir() and relative[0] in {"dist", "scripts"}:
            continue
        allowed = (
            relative[0] == "dist"
            and len(relative) >= 2
            and relative[1] == "OpenUsage Bar.app"
        ) or (
            relative[0] == "scripts"
            and len(relative) == 2
            and relative[1] in ALLOWED_SCRIPTS
        ) or relative in {
            ("LICENSE",), ("THIRD_PARTY_NOTICES.md",), ("release-quick-start.md",),
            ("canary.md",),
        }
        if not allowed:
            raise ArtifactError("unexpected_member")
        lowered = {part.casefold() for part in relative}
        if lowered & SENSITIVE_NAMES or any(part.endswith(".log") for part in lowered):
            raise ArtifactError("private_material")
        if info.file_size <= 1024 * 1024 and not info.is_dir():
            payload = archive.read(info)
            if b"\0" not in payload and HOME_PATH_PATTERN.search(payload):
                raise ArtifactError("home_path", path.name)


def _verify_checksum(archive_path: Path) -> None:
    checksum_path = Path(f"{archive_path}.sha256")
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise ArtifactError("checksum")
    parts = checksum_path.read_text("ascii").strip().split()
    if len(parts) != 2 or parts[1].lstrip("*") != archive_path.name:
        raise ArtifactError("checksum")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", parts[0]) or parts[0] != digest:
        raise ArtifactError("checksum")


def _packaged_build_identity(
    resource_root: Path,
    *,
    canonical_identity: Path | None = None,
    product_truth: Path | None = None,
) -> dict[str, object]:
    try:
        identity, artifact = verify_packaged_build_identity(
            resource_root / PACKAGED_IDENTITY_NAME,
            canonical_identity or REPOSITORY_ROOT / CANONICAL_IDENTITY,
            product_truth or REPOSITORY_ROOT / PRODUCT_TRUTH,
        )
    except (ArtifactBuildIdentityError, OSError, UnicodeError) as error:
        raise ArtifactError("build_identity") from error
    return {"artifact": artifact, "identity": identity}


def _plist(path: Path) -> dict[str, object]:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, TypeError) as error:
        raise ArtifactError("plist") from error
    if type(value) is not dict:
        raise ArtifactError("plist")
    return value


def _metadata(path: Path) -> tuple[str, str]:
    value = _plist(path)
    version = value.get("CFBundleShortVersionString")
    build = value.get("CFBundleVersion")
    if type(version) is not str or type(build) is not str:
        raise ArtifactError("plist")
    return version, build


def verify_versions(
    root: Path,
    expected_version: str,
    *,
    canonical_identity: Path | None = None,
    product_truth: Path | None = None,
) -> dict[str, object]:
    app = root / "dist/OpenUsage Bar.app"
    build_identity = _packaged_build_identity(
        app / "Contents/Resources",
        canonical_identity=canonical_identity,
        product_truth=product_truth,
    )
    identity = build_identity["identity"]
    plists = (
        app / "Contents/Info.plist",
        app / "Contents/Helpers/OpenUsage Activity.app/Contents/Info.plist",
        app / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist",
    )
    values = {_metadata(path) for path in plists}
    main = _plist(plists[0])
    if (
        expected_version != identity["candidateVersion"]
        or values
        != {(identity["candidateVersion"], identity["candidateBuild"])}
        or main.get("CFBundleDisplayName") != identity["displayName"]
    ):
        raise ArtifactError("version_mismatch")
    return build_identity


def verify_executable_names(root: Path) -> None:
    """Every bundle must declare a CFBundleExecutable that exists on disk."""
    app = root / "dist/OpenUsage Bar.app"
    bundles = (
        app,
        app / "Contents/Helpers/OpenUsage Activity.app",
        app / "Contents/Helpers/OpenUsage Provider Settings.app",
    )
    for bundle in bundles:
        info = bundle / "Contents/Info.plist"
        try:
            value = plistlib.loads(info.read_bytes())
            executable = value["CFBundleExecutable"]
        except (OSError, plistlib.InvalidFileException, KeyError, TypeError) as error:
            raise ArtifactError("plist") from error
        if not isinstance(executable, str) or not executable:
            raise ArtifactError("plist")
        candidate = bundle / "Contents/MacOS" / executable
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ArtifactError("binary")


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError as error:
        raise ArtifactError("binary") from error


def verify_binaries(root: Path) -> None:
    app = root / "dist/OpenUsage Bar.app"
    macho_files = [path for path in app.rglob("*") if path.is_file() and _is_macho(path)]
    if not macho_files:
        raise ArtifactError("binary")
    for path in macho_files:
        arches = subprocess.run(
            ["lipo", "-archs", str(path)], capture_output=True, text=True, check=False
        )
        values = set(arches.stdout.split())
        if arches.returncode != 0 or "arm64" not in values or not values <= {"arm64", "x86_64"}:
            raise ArtifactError("architecture")
        if subprocess.run(
            ["codesign", "--display", str(path)],
            capture_output=True, check=False,
        ).returncode != 0:
            raise ArtifactError("signature")
    if subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True, check=False,
    ).returncode != 0:
        raise ArtifactError("signature")


def _native_collector(path: Path, platform: str) -> bool:
    """Recognize the target operating system's executable container format."""

    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if platform == "darwin":
                return header[:4] in MACHO_MAGICS
            if platform == "linux":
                return (
                    len(header) >= 8
                    and header[:4] == b"\x7fELF"
                    and header[4] in {1, 2}
                    and header[5] in {1, 2}
                    and header[6] == 1
                )
            if platform != "win32" or len(header) < 64 or header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(header[0x3C:0x40], "little")
            size = path.stat().st_size
            if pe_offset < 64 or pe_offset > size - 4:
                return False
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\x00\x00"
    except OSError as error:
        raise ArtifactError("binary") from error


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(DESKTOP_SCAN_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise ArtifactError("collector") from error
    return digest.digest()


def _verify_built_collector(
    packaged_collector: Path,
    built_collector: Path,
    platform: str,
) -> None:
    try:
        built_stat = built_collector.lstat()
    except OSError as error:
        raise ArtifactError("collector") from error
    if (
        stat.S_ISLNK(built_stat.st_mode)
        or not stat.S_ISREG(built_stat.st_mode)
        or built_stat.st_size > MAX_DESKTOP_MEMBER_BYTES
        or (
            platform != "win32"
            and built_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            == 0
        )
    ):
        raise ArtifactError("collector")
    if not _native_collector(built_collector, platform):
        raise ArtifactError("binary")
    if _file_digest(packaged_collector) != _file_digest(built_collector):
        raise ArtifactError("collector")


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


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise ArtifactError("native_metadata")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _json_without_duplicate_keys(encoded: bytes) -> object:
    def reject(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ArtifactError("native_metadata")
            value[key] = item
        return value

    try:
        return json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=reject,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ArtifactError("native_metadata")
            ),
        )
    except ArtifactError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ArtifactError("native_metadata") from error


def _asar_package_metadata(path: Path) -> dict[str, object]:
    descriptor: int | None = None
    try:
        linked = path.lstat()
        if (
            stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or linked.st_size < 16
            or linked.st_size > MAX_DESKTOP_MEMBER_BYTES
        ):
            raise ArtifactError("native_metadata")
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
            raise ArtifactError("native_metadata")
        prefix = _read_exact(descriptor, 16)
        pickle_payload, header_size, header_payload, string_size = struct.unpack(
            "<IIII", prefix
        )
        if (
            pickle_payload != 4
            or header_size < 8
            or header_size > MAX_ASAR_HEADER_BYTES
            or header_payload != header_size - 4
            or string_size <= 0
            or string_size > header_payload - 4
            or 8 + header_size > opened.st_size
        ):
            raise ArtifactError("native_metadata")
        header = _json_without_duplicate_keys(_read_exact(descriptor, string_size))
        if type(header) is not dict or type(header.get("files")) is not dict:
            raise ArtifactError("native_metadata")
        record = header["files"].get("package.json")
        if type(record) is not dict or record.get("unpacked") is True:
            raise ArtifactError("native_metadata")
        member_size = record.get("size")
        offset = record.get("offset")
        if (
            type(member_size) is not int
            or isinstance(member_size, bool)
            or member_size <= 0
            or member_size > MAX_PACKAGE_METADATA_BYTES
            or type(offset) is not str
            or re.fullmatch(r"(?:0|[1-9][0-9]*)", offset) is None
        ):
            raise ArtifactError("native_metadata")
        member_start = 8 + header_size + int(offset)
        if member_start < 8 + header_size or member_start + member_size > opened.st_size:
            raise ArtifactError("native_metadata")
        os.lseek(descriptor, member_start, os.SEEK_SET)
        encoded = _read_exact(descriptor, member_size)
        integrity = record.get("integrity")
        if integrity is not None and (
            type(integrity) is not dict
            or integrity.get("algorithm") != "SHA256"
            or integrity.get("hash") != hashlib.sha256(encoded).hexdigest()
        ):
            raise ArtifactError("native_metadata")
        after = os.fstat(descriptor)
        final_link = path.lstat()
        # Compare descriptor snapshots with each other. Windows can expose
        # different timestamp semantics for a path stat and an open handle.
        if (
            not _same_open_file(opened, after)
            or stat.S_ISLNK(final_link.st_mode)
            or not stat.S_ISREG(final_link.st_mode)
            or (linked.st_dev, linked.st_ino)
            != (final_link.st_dev, final_link.st_ino)
        ):
            raise ArtifactError("native_metadata")
    except ArtifactError:
        raise
    except (OSError, KeyError, TypeError, ValueError, struct.error) as error:
        raise ArtifactError("native_metadata") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    metadata = _json_without_duplicate_keys(encoded)
    if type(metadata) is not dict:
        raise ArtifactError("native_metadata")
    return metadata


def _verify_asar_package_metadata(
    resource_root: Path,
    identity: dict[str, object],
) -> None:
    metadata = _asar_package_metadata(resource_root / "app.asar")
    if (
        metadata.get("name") != "usagehub-desktop"
        or metadata.get("version") != identity["candidateVersion"]
        or metadata.get("buildVersion") != identity["candidateBuild"]
        or metadata.get("buildNumber") != identity["candidateBuild"]
    ):
        raise ArtifactError("version_mismatch")


def _pe_version_strings(path: Path) -> dict[str, set[str]]:
    keys = ("FileVersion", "ProductName", "ProductVersion")
    descriptor: int | None = None
    mapped: mmap.mmap | None = None
    try:
        linked = path.lstat()
        if (
            stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or linked.st_size <= 0
            or linked.st_size > MAX_DESKTOP_MEMBER_BYTES
        ):
            raise ArtifactError("native_metadata")
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
            raise ArtifactError("native_metadata")
        mapped = mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)
        values = {key: set() for key in keys}
        for key in keys:
            marker = (key + "\0").encode("utf-16le")
            position = 0
            while True:
                position = mapped.find(marker, position)
                if position < 0:
                    break
                start = position - 6
                if start >= 0:
                    length, value_length, value_type = struct.unpack_from(
                        "<HHH", mapped, start
                    )
                    value_start = (position + len(marker) + 3) & ~3
                    value_end = value_start + value_length * 2
                    if (
                        value_type == 1
                        and length >= value_end - start
                        and start + length <= len(mapped)
                    ):
                        try:
                            value = mapped[value_start:value_end].decode("utf-16le")
                        except UnicodeError:
                            value = ""
                        value = value.rstrip("\0")
                        if value:
                            values[key].add(value)
                position += len(marker)
        after = os.fstat(descriptor)
        final_link = path.lstat()
        if (
            not _same_open_file(opened, after)
            or stat.S_ISLNK(final_link.st_mode)
            or not stat.S_ISREG(final_link.st_mode)
            or (linked.st_dev, linked.st_ino)
            != (final_link.st_dev, final_link.st_ino)
        ):
            raise ArtifactError("native_metadata")
        return values
    except ArtifactError:
        raise
    except (OSError, ValueError, struct.error) as error:
        raise ArtifactError("native_metadata") from error
    finally:
        if mapped is not None:
            mapped.close()
        if descriptor is not None:
            os.close(descriptor)


def _windows_build_matches(value: str, build: object) -> bool:
    if type(build) is not str or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value) is None:
        return False
    parts = [int(part) for part in value.split(".")]
    return parts[0] == int(build) and all(part == 0 for part in parts[1:])


def _verify_windows_native_metadata(
    package_root: Path,
    identity: dict[str, object],
) -> None:
    executable = package_root / "UsageHub.exe"
    if not _native_collector(executable, "win32"):
        raise ArtifactError("native_metadata")
    values = _pe_version_strings(executable)
    expected_product_version = (
        f"{identity['candidateVersion']}.{identity['candidateBuild']}"
    )
    if (
        values["ProductName"] != {identity["displayName"]}
        or values["ProductVersion"] != {expected_product_version}
        or not values["FileVersion"]
        or any(
            not _windows_build_matches(value, identity["candidateBuild"])
            for value in values["FileVersion"]
        )
    ):
        raise ArtifactError("version_mismatch")


def _electron_resource_root(package_root: Path, platform: str) -> Path:
    if platform == "darwin":
        candidate = package_root / "Contents/Resources"
    else:
        candidate = package_root / "resources"
    try:
        metadata = candidate.lstat()
    except OSError:
        metadata = None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactError("native_metadata")
        return candidate
    if platform != "linux":
        raise ArtifactError("native_metadata")

    library_root = package_root / "usr/lib"
    candidates: list[Path] = []
    try:
        library_metadata = library_root.lstat()
        if stat.S_ISLNK(library_metadata.st_mode) or not stat.S_ISDIR(
            library_metadata.st_mode
        ):
            raise ArtifactError("native_metadata")
        with os.scandir(library_root) as applications:
            for application in applications:
                app_metadata = application.stat(follow_symlinks=False)
                if application.is_symlink() or not stat.S_ISDIR(app_metadata.st_mode):
                    continue
                resources = Path(application.path) / "resources"
                try:
                    resources_metadata = resources.lstat()
                except OSError:
                    continue
                if (
                    not stat.S_ISLNK(resources_metadata.st_mode)
                    and stat.S_ISDIR(resources_metadata.st_mode)
                    and (resources / "app.asar").is_file()
                ):
                    candidates.append(resources)
    except ArtifactError:
        raise
    except OSError as error:
        raise ArtifactError("native_metadata") from error
    if len(candidates) != 1:
        raise ArtifactError("native_metadata")
    return candidates[0]


def _verify_mac_native_metadata(
    package_root: Path,
    identity: dict[str, object],
) -> None:
    metadata = _plist(package_root / "Contents/Info.plist")
    if (
        metadata.get("CFBundleDisplayName") != identity["displayName"]
        or metadata.get("CFBundleName") != identity["displayName"]
        or metadata.get("CFBundleShortVersionString")
        != identity["candidateVersion"]
        or metadata.get("CFBundleVersion") != identity["candidateBuild"]
    ):
        raise ArtifactError("version_mismatch")


def _desktop_entry(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_DESKTOP_ENTRY_BYTES
        ):
            raise ArtifactError("native_metadata")
        lines = path.read_text(encoding="utf-8").splitlines()
    except ArtifactError:
        raise
    except (OSError, UnicodeError) as error:
        raise ArtifactError("native_metadata") from error
    section: str | None = None
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Desktop Entry" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise ArtifactError("native_metadata")
        values[key] = value
    return values


def _verify_linux_final_metadata(
    desktop_entries: list[Path],
    identity: dict[str, object],
) -> None:
    if len(desktop_entries) != 1:
        raise ArtifactError("native_metadata")
    values = _desktop_entry(desktop_entries[0])
    if (
        values.get("Name") != identity["displayName"]
        or values.get("X-AppImage-Version") != identity["candidateBuild"]
    ):
        raise ArtifactError("version_mismatch")


def inspect_desktop_package(
    root: Path,
    platform: str,
    *,
    built_collector: Path | None = None,
    built_settings: Path | None = None,
    canonical_identity: Path | None = None,
    product_truth: Path | None = None,
    require_final_native_metadata: bool = False,
) -> dict[str, object]:
    """Audit one unpacked Electron package without following external links."""

    if platform not in {"darwin", "win32", "linux"}:
        raise ArtifactError("collector")
    try:
        if root.is_symlink() or not root.is_dir():
            raise ArtifactError("collector")
        package_root = root.resolve(strict=True)
    except OSError as error:
        raise ArtifactError("collector") from error

    resource_root = _electron_resource_root(package_root, platform)
    build_identity = _packaged_build_identity(
        resource_root,
        canonical_identity=canonical_identity,
        product_truth=product_truth,
    )
    identity = build_identity["identity"]
    if platform == "darwin":
        _verify_mac_native_metadata(package_root, identity)
    elif platform == "win32":
        _verify_windows_native_metadata(package_root, identity)
    else:
        _verify_asar_package_metadata(resource_root, identity)
    collector_name = (
        "openusage-collector.exe" if platform == "win32"
        else "openusage-collector"
    )
    collector_directory = resource_root / "collector"
    try:
        directory_stat = collector_directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ArtifactError("collector")
        with os.scandir(collector_directory) as entries:
            first = next(entries, None)
            second = next(entries, None)
        if first is None or second is not None or first.name != collector_name:
            raise ArtifactError("collector")
        collector_stat = first.stat(follow_symlinks=False)
    except OSError as error:
        raise ArtifactError("collector") from error
    if (
        not stat.S_ISREG(collector_stat.st_mode)
        or collector_stat.st_size > MAX_DESKTOP_MEMBER_BYTES
        or (
            platform != "win32"
            and collector_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            == 0
        )
    ):
        raise ArtifactError("collector")

    packaged_collector = Path(first.path)
    if not _native_collector(packaged_collector, platform):
        raise ArtifactError("binary")
    if built_collector is not None:
        _verify_built_collector(
            packaged_collector,
            built_collector,
            platform,
        )
    if built_settings is not None:
        settings_name = (
            "openusage-settings.exe" if platform == "win32"
            else "openusage-settings"
        )
        settings_directory = resource_root / "settings"
        try:
            directory_stat = settings_directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise ArtifactError("collector")
            with os.scandir(settings_directory) as entries:
                first = next(entries, None)
                second = next(entries, None)
            if first is None or second is not None or first.name != settings_name:
                raise ArtifactError("collector")
            settings_stat = first.stat(follow_symlinks=False)
        except OSError as error:
            raise ArtifactError("collector") from error
        if (
            not stat.S_ISREG(settings_stat.st_mode)
            or settings_stat.st_size > MAX_DESKTOP_MEMBER_BYTES
            or (
                platform != "win32"
                and settings_stat.st_mode
                & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                == 0
            )
        ):
            raise ArtifactError("collector")
        packaged_settings = Path(first.path)
        if not _native_collector(packaged_settings, platform):
            raise ArtifactError("binary")
        _verify_built_collector(packaged_settings, built_settings, platform)

    desktop_entries: list[Path] = []
    for member, _size in _desktop_files(package_root):
        if platform == "linux" and member.suffix == ".desktop":
            desktop_entries.append(member)
        if _private_desktop_name(member.name):
            raise ArtifactError("private_material")
        reason = _scan_desktop_content(member)
        if reason is not None:
            raise ArtifactError(reason)
    if platform == "linux" and require_final_native_metadata:
        _verify_linux_final_metadata(desktop_entries, identity)
    return build_identity


def _desktop_files(root: Path):
    pending: list[tuple[Path, int]] = [(root, 0)]
    member_count = 0
    total_bytes = 0
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_DESKTOP_DEPTH:
            raise ArtifactError("unsafe_path")
        try:
            entries = os.scandir(directory)
            with entries:
                for entry in entries:
                    member_count += 1
                    if member_count > MAX_DESKTOP_MEMBERS:
                        raise ArtifactError("oversized")
                    member = Path(entry.path)
                    if _private_desktop_name(entry.name):
                        raise ArtifactError("private_material")
                    try:
                        member_stat = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise ArtifactError("binary") from error
                    if stat.S_ISLNK(member_stat.st_mode):
                        try:
                            target = member.resolve(strict=True)
                        except OSError as error:
                            raise ArtifactError("symlink") from error
                        if not _contained_path(root, target):
                            raise ArtifactError("symlink")
                        continue
                    if stat.S_ISDIR(member_stat.st_mode):
                        pending.append((member, depth + 1))
                        continue
                    if not stat.S_ISREG(member_stat.st_mode):
                        raise ArtifactError("unexpected_member")
                    size = member_stat.st_size
                    total_bytes += size
                    if (
                        size > MAX_DESKTOP_MEMBER_BYTES
                        or total_bytes > MAX_DESKTOP_TOTAL_BYTES
                    ):
                        raise ArtifactError("oversized")
                    yield member, size
        except ArtifactError:
            raise
        except OSError as error:
            raise ArtifactError("binary") from error


def _contained_path(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except (OSError, ValueError):
        return False


def _private_desktop_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in SENSITIVE_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(".token")
        or lowered.endswith(".log")
        or DESKTOP_DATABASE_PATTERN.fullmatch(lowered) is not None
    )


def _scan_desktop_content(path: Path) -> str | None:
    overlap = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(DESKTOP_SCAN_CHUNK_BYTES)
                if not chunk:
                    return None
                window = overlap + chunk
                if (
                    DESKTOP_POSIX_HOME_PATH_PATTERN.search(window)
                    or WINDOWS_HOME_PATH_PATTERN.search(window)
                ):
                    return "home_path"
                if any(canary in window for canary in RAW_CONTENT_CANARIES):
                    return "private_material"
                overlap = window[-DESKTOP_SCAN_OVERLAP_BYTES:]
    except OSError as error:
        raise ArtifactError("binary") from error


def _desktop_platform(root: Path, collector_name: str) -> str:
    if collector_name == "openusage-collector.exe":
        return "win32"
    if collector_name != "openusage-collector":
        raise ArtifactError("collector")
    return "darwin" if (root / "Contents/Resources").is_dir() else "linux"


def audit(path: Path) -> None:
    path = path.resolve()
    match = ARCHIVE_PATTERN.fullmatch(path.name)
    if path.is_symlink() or not path.is_file() or match is None:
        raise ArtifactError("archive")
    _verify_checksum(path)
    expected_root = path.name[:-4]
    with zipfile.ZipFile(path) as archive:
        inspect_members(archive, expected_root)
        with tempfile.TemporaryDirectory() as directory:
            extracted = subprocess.run(
                ["/usr/bin/ditto", "-x", "-k", str(path), directory],
                capture_output=True, check=False,
            )
            if extracted.returncode != 0:
                raise ArtifactError("archive")
            root = Path(directory) / expected_root
            verify_versions(root, match.group(1))
            verify_executable_names(root)
            verify_binaries(root)


def main(arguments: list[str]) -> int:
    require_final_native_metadata = bool(
        arguments and arguments[-1] == "--require-final-native-metadata"
    )
    desktop_core = arguments[:-1] if require_final_native_metadata else arguments
    desktop_arguments = (
        len(desktop_core) in {5, 7}
        and desktop_core[0] == "--desktop-package"
        and desktop_core[3] == "--built-collector"
        and (
            len(desktop_core) == 5
            or desktop_core[5] == "--built-settings"
        )
    )
    if len(arguments) != 1 and not desktop_arguments:
        print("release_artifact_invalid", file=sys.stderr)
        return 2
    try:
        if desktop_arguments:
            root = Path(arguments[1])
            inspect_desktop_package(
                root,
                _desktop_platform(root, arguments[2]),
                built_collector=Path(arguments[4]),
                built_settings=(
                    Path(desktop_core[6]) if len(desktop_core) == 7 else None
                ),
                require_final_native_metadata=require_final_native_metadata,
            )
        else:
            audit(Path(arguments[0]))
    except ArtifactError as error:
        reason = error.reason
        if reason not in SAFE_REASONS:
            reason = "unknown"
        member = error.member
        if member is not None and re.fullmatch(r"[A-Za-z0-9._ -]{1,100}", member):
            print(
                f"release_artifact_invalid reason={reason} member={member}",
                file=sys.stderr,
            )
        else:
            print(f"release_artifact_invalid reason={reason}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, zipfile.BadZipFile):
        print("release_artifact_invalid reason=unknown", file=sys.stderr)
        return 1
    print("release_artifact_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
