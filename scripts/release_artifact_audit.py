#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import posixpath
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


ARCHIVE_PATTERN = re.compile(r"^OpenUsage-Bar-v(\d+\.\d+\.\d+)-macos-arm64\.zip$")
ALLOWED_SCRIPTS = frozenset({
    "activity_install_process.sh", "install_app.sh",
    "install_app_transaction.sh", "rollback_app.sh", "uninstall_app.sh",
    "export_diagnostics.py", "privacy_scan.py", "verify_local_api.py",
})
SENSITIVE_NAMES = frozenset({
    ".env", "providers.json", "activity.sqlite3", "activity.sqlite3-wal",
    "activity.sqlite3-shm", "keychain", "cookies", "credentials",
})
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}
HOME_PATH_PATTERN = re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\b)")


class ArtifactError(ValueError):
    pass


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
        mode = _member_mode(info)
        if stat.S_ISLNK(mode):
            try:
                target = archive.read(info).decode("utf-8")
            except (UnicodeError, KeyError) as error:
                raise ArtifactError("symlink") from error
            app_prefix = f"{expected_root}/dist/OpenUsage Bar.app/"
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if target.startswith("/") or not name.startswith(app_prefix) or not resolved.startswith(app_prefix):
                raise ArtifactError("symlink")
            continue
        total += info.file_size
        if info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
            raise ArtifactError("oversized")
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
                raise ArtifactError("home_path")


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


def _metadata(path: Path) -> tuple[str, str]:
    try:
        value = plistlib.loads(path.read_bytes())
        version = value["CFBundleShortVersionString"]
        build = value["CFBundleVersion"]
    except (OSError, plistlib.InvalidFileException, KeyError, TypeError) as error:
        raise ArtifactError("plist") from error
    if not isinstance(version, str) or not isinstance(build, str):
        raise ArtifactError("plist")
    return version, build


def verify_versions(root: Path, expected_version: str) -> None:
    app = root / "dist/OpenUsage Bar.app"
    plists = (
        app / "Contents/Info.plist",
        app / "Contents/Helpers/OpenUsage Activity.app/Contents/Info.plist",
        app / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist",
    )
    values = {_metadata(path) for path in plists}
    if len(values) != 1 or next(iter(values))[0] != expected_version:
        raise ArtifactError("version_mismatch")


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
            verify_binaries(root)


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("release_artifact_invalid", file=sys.stderr)
        return 2
    try:
        audit(Path(arguments[0]))
    except (ArtifactError, OSError, UnicodeError, zipfile.BadZipFile):
        print("release_artifact_invalid", file=sys.stderr)
        return 1
    print("release_artifact_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
