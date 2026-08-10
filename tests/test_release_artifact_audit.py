import io
import hashlib
import inspect
import json
import os
import plistlib
import stat
import struct
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import scripts.release_artifact_audit as audit_module
from scripts.release_artifact_audit import (
    ArtifactError,
    inspect_members,
    main,
    verify_executable_names,
    verify_versions,
)


ROOT_NAME = "OpenUsage-Bar-v0.4.0-macos-arm64"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_IDENTITY = (
    REPOSITORY_ROOT / "openusage_bar/resources/artifact-build-identity.v1.json"
)
PACKAGED_IDENTITY_NAME = "product-build-identity.v1.json"
PACKAGE_METADATA = {
    "buildNumber": "28",
    "buildVersion": "28",
    "name": "usagehub-desktop",
    "version": "0.8.6",
}


def archive_with(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in entries:
            if isinstance(value, zipfile.ZipInfo):
                archive.writestr(value, b"../../../../outside")
            else:
                archive.writestr(name, value)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def native_collector_bytes(platform, marker):
    if platform == "darwin":
        return b"\xcf\xfa\xed\xfe" + marker
    if platform == "linux":
        return b"\x7fELF\x02\x01\x01\x00" + marker
    image = bytearray(128)
    image[:2] = b"MZ"
    image[0x3C:0x40] = (64).to_bytes(4, "little")
    image[64:68] = b"PE\x00\x00"
    image[80 : 80 + len(marker)] = marker
    return bytes(image)


def _write_asar(path: Path, *, extra_payload: bytes = b"") -> None:
    package = json.dumps(
        PACKAGE_METADATA,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    header = json.dumps(
        {
            "files": {
                "package.json": {
                    "integrity": {
                        "algorithm": "SHA256",
                        "hash": hashlib.sha256(package).hexdigest(),
                    },
                    "offset": "0",
                    "size": len(package),
                }
            }
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    header_size = len(header) + 8
    path.write_bytes(
        struct.pack("<IIII", 4, header_size, header_size - 4, len(header))
        + header
        + package
        + extra_payload
    )


def _windows_versioned_executable() -> bytes:
    image = bytearray(native_collector_bytes("win32", b"usagehub-app"))
    for key, value in (
        ("FileVersion", "28"),
        ("ProductName", "UsageHub"),
        ("ProductVersion", "0.8.6.28"),
    ):
        while len(image) % 4:
            image.append(0)
        start = len(image)
        image.extend(b"\0" * 6)
        image.extend((key + "\0").encode("utf-16le"))
        while len(image) % 4:
            image.append(0)
        encoded = (value + "\0").encode("utf-16le")
        image.extend(encoded)
        struct.pack_into(
            "<HHH",
            image,
            start,
            len(image) - start,
            len(encoded) // 2,
            1,
        )
    return bytes(image)


def write_desktop_package(
    root: Path,
    platform: str,
    *,
    marker: bytes = b"fixture",
    asar_extra: bytes = b"",
) -> Path:
    resource_root = (
        root / "Contents/Resources" if platform == "darwin" else root / "resources"
    )
    collector_name = (
        "openusage-collector.exe" if platform == "win32" else "openusage-collector"
    )
    collector = resource_root / "collector" / collector_name
    collector.parent.mkdir(parents=True)
    collector.write_bytes(native_collector_bytes(platform, marker))
    collector.chmod(0o755)
    (resource_root / PACKAGED_IDENTITY_NAME).write_bytes(
        CANONICAL_IDENTITY.read_bytes()
    )
    _write_asar(resource_root / "app.asar", extra_payload=asar_extra)

    if platform == "darwin":
        (root / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleDisplayName": "UsageHub",
                    "CFBundleName": "UsageHub",
                    "CFBundleShortVersionString": "0.8.6",
                    "CFBundleVersion": "28",
                }
            )
        )
    elif platform == "win32":
        (root / "UsageHub.exe").write_bytes(_windows_versioned_executable())
    else:
        desktop = root / "usr/share/applications/usagehub.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(
            "[Desktop Entry]\n"
            "Name=UsageHub\n"
            "X-AppImage-Version=28\n",
            encoding="utf-8",
        )
    return collector


class ReleaseArtifactAuditTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-native file identity contract")
    def test_windows_path_and_descriptor_metadata_share_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.asar"
            _write_asar(path)
            linked = path.lstat()
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            try:
                opened = os.fstat(descriptor)
                payload = os.read(descriptor, linked.st_size)
                after = os.fstat(descriptor)
                final_link = path.lstat()
            finally:
                os.close(descriptor)

        identity_fields = (
            "st_dev",
            "st_ino",
            "st_size",
        )
        fields = (
            *identity_fields,
            "st_mtime_ns",
            "st_ctime_ns",
        )
        self.assertEqual(
            tuple(getattr(linked, field) for field in identity_fields),
            tuple(getattr(opened, field) for field in identity_fields),
        )
        self.assertEqual(len(payload), linked.st_size)
        self.assertEqual(
            tuple(getattr(opened, field) for field in fields),
            tuple(getattr(after, field) for field in fields),
        )
        self.assertEqual(
            (linked.st_dev, linked.st_ino),
            (final_link.st_dev, final_link.st_ino),
        )

    def test_cli_reports_only_a_safe_rule_identifier(self):
        error = io.StringIO()
        with redirect_stderr(error):
            result = main(["missing.zip"])

        self.assertEqual(result, 1)
        self.assertEqual(error.getvalue(), "release_artifact_invalid reason=archive\n")

    def test_home_path_error_carries_only_the_validated_member_basename(self):
        archive = archive_with(((
            f"{ROOT_NAME}/release-quick-start.md", "/Users/example/private"
        ),))

        with self.assertRaises(ArtifactError) as raised:
            inspect_members(archive, ROOT_NAME)

        self.assertEqual(raised.exception.reason, "home_path")
        self.assertEqual(raised.exception.member, "release-quick-start.md")

    def test_documented_members_are_accepted(self):
        archive = archive_with((
            (f"{ROOT_NAME}/LICENSE", "license"),
            (f"{ROOT_NAME}/canary.md", "canary"),
            (f"{ROOT_NAME}/release-quick-start.md", "docs"),
            (
                f"{ROOT_NAME}/scripts/export_diagnostics.py",
                'DENIED_HOME_PREFIXES = ("/Users/", "/home/")',
            ),
            (
                f"{ROOT_NAME}/scripts/verify_canary_surfaces.py",
                'REPORT_SCHEMA = "openusage-canary-surfaces-1"',
            ),
            (
                f"{ROOT_NAME}/scripts/verify_canary_candidate.py",
                'REPOSITORY = "tttboy123/openusage-bar"',
            ),
            (f"{ROOT_NAME}/scripts/activity_schema.py", "EXPECTED_SCHEMA = {}"),
            (f"{ROOT_NAME}/scripts/install_app.sh", "#!/bin/zsh"),
            (f"{ROOT_NAME}/dist/OpenUsage Bar.app/Contents/Info.plist", "plist"),
        ))
        inspect_members(archive, ROOT_NAME)

    def test_traversal_absolute_duplicate_and_unexpected_members_fail(self):
        cases = (
            [(f"{ROOT_NAME}/../private", "x")],
            [("/absolute", "x")],
            [(f"{ROOT_NAME}/notes.txt", "x")],
            [(f"{ROOT_NAME}/LICENSE", "x"), (f"{ROOT_NAME}/LICENSE", "y")],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(ArtifactError):
                    inspect_members(archive_with(entries), ROOT_NAME)

    def test_symlink_private_material_and_home_paths_fail(self):
        link = zipfile.ZipInfo(f"{ROOT_NAME}/dist/OpenUsage Bar.app/Contents/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = (
            [(link.filename, link)],
            [(f"{ROOT_NAME}/dist/OpenUsage Bar.app/activity.sqlite3", "x")],
            [(f"{ROOT_NAME}/release-quick-start.md", "/Users/example/private")],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(ArtifactError):
                    inspect_members(archive_with(entries), ROOT_NAME)

    def test_oversized_symlink_target_is_rejected_before_payload_allocation(self):
        link = zipfile.ZipInfo(
            f"{ROOT_NAME}/dist/OpenUsage Bar.app/Contents/oversized-link"
        )
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        link.file_size = audit_module.MAX_SYMLINK_TARGET_BYTES + 1

        class OversizedSymlinkArchive:
            def infolist(self):
                return [link]

            def read(self, _info):
                raise AssertionError("oversized symlink payload must not be read")

        with self.assertRaises(ArtifactError) as raised:
            inspect_members(OversizedSymlinkArchive(), ROOT_NAME)

        self.assertEqual(raised.exception.reason, "oversized")

    def test_three_bundle_versions_must_match_archive_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "dist/OpenUsage Bar.app"
            paths = (
                app / "Contents/Info.plist",
                app / "Contents/Helpers/OpenUsage Activity.app/Contents/Info.plist",
                app / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist",
            )
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(plistlib.dumps({
                    "CFBundleShortVersionString": "0.4.1" if index == 2 else "0.4.0",
                    "CFBundleVersion": "4",
                }))
            with self.assertRaises(ArtifactError):
                verify_versions(root, "0.4.0")

    def test_executable_names_must_match_declared_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "dist/OpenUsage Bar.app"
            bundles = (
                app,
                app / "Contents/Helpers/OpenUsage Activity.app",
                app / "Contents/Helpers/OpenUsage Provider Settings.app",
            )
            for bundle in bundles:
                info = bundle / "Contents/Info.plist"
                info.parent.mkdir(parents=True, exist_ok=True)
                info.write_bytes(plistlib.dumps({
                    "CFBundleShortVersionString": "0.4.0",
                    "CFBundleVersion": "4",
                    "CFBundleExecutable": "OpenUsage",
                }))
                executable = bundle / "Contents/MacOS/OpenUsage"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"\xcf\xfa\xed\xfe")
                executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            verify_executable_names(root)

            mismatch = (
                app
                / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/Info.plist"
            )
            mismatch.write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": "0.4.0",
                "CFBundleVersion": "4",
                "CFBundleExecutable": "Missing Launcher",
            }))
            with self.assertRaises(ArtifactError):
                verify_executable_names(root)

    def test_desktop_package_requires_the_os_native_collector_member(self):
        cases = (
            ("darwin", "Contents/Resources", "openusage-collector"),
            ("win32", "resources", "openusage-collector.exe"),
            ("linux", "resources", "openusage-collector"),
        )
        if os.name == "nt":
            cases = (cases[1],)
        for platform, resources, executable_name in cases:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector = write_desktop_package(
                    root,
                    platform,
                    marker=b"member",
                )
                self.assertEqual(
                    collector,
                    root / resources / "collector" / executable_name,
                )

                audit_module.inspect_desktop_package(root, platform)

                wrong_platform = {
                    "darwin": "linux",
                    "win32": "darwin",
                    "linux": "win32",
                }[platform]
                collector.write_bytes(
                    native_collector_bytes(wrong_platform, b"wrong-format")
                )
                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(root, platform)
                self.assertEqual(raised.exception.reason, "binary")

                collector.write_bytes(native_collector_bytes(platform, b"member"))
                collector.rename(collector.with_name("wrong-collector-name"))
                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(root, platform)
                self.assertEqual(raised.exception.reason, "collector")

    def test_desktop_package_requires_sha256_match_to_built_native_collector(self):
        signature = inspect.signature(audit_module.inspect_desktop_package)
        self.assertIn(
            "built_collector",
            signature.parameters,
            "desktop package audit must SHA-256 compare the exact built Collector",
        )
        cases = (
            ("darwin", "Contents/Resources", "openusage-collector"),
            ("win32", "resources", "openusage-collector.exe"),
            ("linux", "resources", "openusage-collector"),
        )
        if os.name == "nt":
            cases = (cases[1],)
        for platform, resources, executable_name in cases:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "package"
                packaged = write_desktop_package(
                    root,
                    platform,
                    marker=b"same-build",
                )
                built = base / "dist-collector" / executable_name
                built.parent.mkdir(parents=True)
                native = native_collector_bytes(platform, b"same-build")
                built.write_bytes(native)
                built.chmod(0o755)

                audit_module.inspect_desktop_package(
                    root,
                    platform,
                    built_collector=built,
                )

                packaged.write_bytes(native_collector_bytes(platform, b"different"))
                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(
                        root,
                        platform,
                        built_collector=built,
                    )
                self.assertEqual(raised.exception.reason, "collector")

                wrong_platform = {
                    "darwin": "linux",
                    "win32": "darwin",
                    "linux": "win32",
                }[platform]
                non_native = native_collector_bytes(
                    wrong_platform,
                    b"wrong-format",
                )
                packaged.write_bytes(non_native)
                built.write_bytes(non_native)
                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(
                        root,
                        platform,
                        built_collector=built,
                    )
                self.assertEqual(raised.exception.reason, "binary")

    def test_desktop_package_requires_sha256_match_to_bundled_settings_helper(self):
        signature = inspect.signature(audit_module.inspect_desktop_package)
        self.assertIn("built_settings", signature.parameters)
        cases = (
            ("darwin", "Contents/Resources", "openusage-settings"),
            ("win32", "resources", "openusage-settings.exe"),
            ("linux", "resources", "openusage-settings"),
        )
        if os.name == "nt":
            cases = (cases[1],)
        for platform, resources, executable_name in cases:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "package"
                write_desktop_package(root, platform)
                packaged = root / resources / "settings" / executable_name
                packaged.parent.mkdir(parents=True)
                native = native_collector_bytes(platform, b"settings-same-build")
                packaged.write_bytes(native)
                packaged.chmod(0o755)
                built = base / "dist-settings" / executable_name
                built.parent.mkdir(parents=True)
                built.write_bytes(native)
                built.chmod(0o755)

                audit_module.inspect_desktop_package(
                    root,
                    platform,
                    built_settings=built,
                )

                packaged.write_bytes(
                    native_collector_bytes(platform, b"settings-different")
                )
                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(
                        root,
                        platform,
                        built_settings=built,
                    )
                self.assertEqual(raised.exception.reason, "collector")

    def test_desktop_package_cli_requires_built_collector_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "package"
            platform = "win32" if os.name == "nt" else "linux"
            collector_name = (
                "openusage-collector.exe"
                if platform == "win32"
                else "openusage-collector"
            )
            collector = write_desktop_package(
                root,
                platform,
                marker=b"same-build",
            )
            built = base / "dist-collector" / collector_name
            built.parent.mkdir(parents=True)
            native = native_collector_bytes(platform, b"same-build")
            built.write_bytes(native)
            built.chmod(0o755)

            output = io.StringIO()
            error = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main([
                    "--desktop-package",
                    str(root),
                    collector_name,
                ])
            self.assertEqual(
                result,
                2,
                "desktop package CLI must reject an audit without --built-collector",
            )
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(error.getvalue(), "release_artifact_invalid\n")

            output = io.StringIO()
            error = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main([
                    "--desktop-package",
                    str(root),
                    collector_name,
                    "--built-collector",
                    str(built),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), "release_artifact_ok\n")
            self.assertEqual(error.getvalue(), "")

    def test_desktop_package_rejects_runtime_secret_and_database_members(self):
        platform = "win32" if os.name == "nt" else "linux"
        private_names = (
            ".env",
            "api.token",
            "gateway.token",
            "providers.json",
            "activity.sqlite3",
            "gateway-cache.sqlite3",
            "gateway-telemetry.sqlite3",
        )
        for private_name in private_names:
            with self.subTest(private_name=private_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_desktop_package(root, platform, marker=b"privacy")
                (root / private_name).write_text("private", encoding="utf-8")

                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(root, platform)
                self.assertEqual(raised.exception.reason, "private_material")

    def test_desktop_package_rejects_home_paths_and_raw_content_canaries(self):
        platform = "win32" if os.name == "nt" else "linux"
        private_payloads = (
            (b"built at /Users/release/private", "home_path"),
            (b"built at C:\\Users\\release\\private", "home_path"),
            (b"OPENUSAGE_RAW_PROMPT_CANARY_5bc16d65", "private_material"),
            (b"OPENUSAGE_RAW_RESPONSE_CANARY_d2f5479a", "private_material"),
        )
        for payload, reason in private_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_desktop_package(
                    root,
                    platform,
                    marker=b"privacy",
                    asar_extra=payload,
                )

                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(root, platform)
                self.assertEqual(raised.exception.reason, reason)

    def test_desktop_package_binds_canonical_identity_digest_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform = "win32" if os.name == "nt" else "linux"
            write_desktop_package(root, platform)

            observed = audit_module.inspect_desktop_package(
                root,
                platform,
                require_final_native_metadata=platform == "linux",
            )

            canonical = CANONICAL_IDENTITY.read_bytes()
            self.assertEqual(
                observed["artifact"],
                {
                    "name": PACKAGED_IDENTITY_NAME,
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                    "sizeBytes": len(canonical),
                },
            )
            self.assertEqual(
                (
                    observed["identity"]["displayName"],
                    observed["identity"]["candidateVersion"],
                    observed["identity"]["candidateBuild"],
                ),
                ("UsageHub", "0.8.6", "28"),
            )

    def test_desktop_package_rejects_identity_and_native_metadata_tamper(self):
        cases = ("identity", "mac", "windows", "linux")
        if os.name == "nt":
            cases = ("identity", "windows")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                platform = {
                    "identity": "win32" if os.name == "nt" else "linux",
                    "mac": "darwin",
                    "windows": "win32",
                    "linux": "linux",
                }[case]
                write_desktop_package(root, platform)
                if case == "identity":
                    identity = root / "resources" / PACKAGED_IDENTITY_NAME
                    identity.write_bytes(identity.read_bytes() + b" ")
                    expected_reason = "build_identity"
                elif case == "mac":
                    info = root / "Contents/Info.plist"
                    payload = plistlib.loads(info.read_bytes())
                    payload["CFBundleVersion"] = "29"
                    info.write_bytes(plistlib.dumps(payload))
                    expected_reason = "version_mismatch"
                elif case == "windows":
                    executable = root / "UsageHub.exe"
                    executable.write_bytes(
                        executable.read_bytes().replace(
                            "UsageHub".encode("utf-16le"),
                            "OtherHub".encode("utf-16le"),
                        )
                    )
                    expected_reason = "version_mismatch"
                else:
                    desktop = root / "usr/share/applications/usagehub.desktop"
                    desktop.write_text(
                        "[Desktop Entry]\n"
                        "Name=UsageHub\n"
                        "X-AppImage-Version=29\n",
                        encoding="utf-8",
                    )
                    expected_reason = "version_mismatch"

                with self.assertRaises(ArtifactError) as raised:
                    audit_module.inspect_desktop_package(
                        root,
                        platform,
                        require_final_native_metadata=platform == "linux",
                    )

                self.assertEqual(raised.exception.reason, expected_reason)


if __name__ == "__main__":
    unittest.main()
