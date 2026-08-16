import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.extract_nsis_payload as nsis


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"


def technical_listing(archive_type, members):
    lines = [
        "7-Zip 26.02 (x64)",
        "",
        "Listing archive: artifact",
        "",
        "--",
        "Path = artifact",
        f"Type = {archive_type}",
        "Physical Size = 123",
        "",
        "----------",
    ]
    for member in members:
        lines.extend((
            f"Path = {member['path']}",
            f"Size = {member.get('size', 1)}",
            f"Packed Size = {member.get('packed_size', 1)}",
        ))
        if member.get("folder"):
            lines.append("Folder = +")
        if member.get("symbolic_link"):
            lines.append(f"Symbolic Link = {member['symbolic_link']}")
        lines.append("")
    return "\n".join(lines).encode("ascii")


OUTER_X64 = technical_listing("Nsis", ({
    "path": r"$PLUGINSDIR\app-64.7z",
    "size": 5,
},))
INNER_WINDOWS = technical_listing("7z", (
    {"path": "UsageHub.exe", "size": 2},
    {"path": r"resources\app.asar", "size": 4},
    {
        "path": r"resources\collector\openusage-collector.exe",
        "size": 9,
    },
))


class NsisListingContractTests(unittest.TestCase):
    def test_architecture_maps_to_electron_builder_26153_member(self):
        self.assertEqual(
            nsis.expected_app_archive("x64"),
            r"$PLUGINSDIR\app-64.7z",
        )
        self.assertEqual(
            nsis.expected_app_archive("arm64"),
            r"$PLUGINSDIR\app-arm64.7z",
        )
        with self.assertRaises(nsis.NsisPayloadError) as raised:
            nsis.expected_app_archive("ia32")
        self.assertEqual(raised.exception.reason, "architecture")

    def test_outer_listing_requires_exactly_one_exact_architecture_member(self):
        selected = nsis.select_app_archive(OUTER_X64, "x64")
        self.assertEqual(selected.path, r"$PLUGINSDIR\app-64.7z")
        self.assertEqual(selected.size, 5)

        invalid_members = (
            (
                {"path": r"$PLUGINSDIR\app-arm64.7z", "size": 5},
            ),
            (
                {"path": r"$PLUGINSDIR\app-64.7z", "size": 5},
                {"path": r"$PLUGINSDIR\app-arm64.7z", "size": 5},
            ),
            (
                {"path": r"elsewhere\app-64.7z", "size": 5},
            ),
            (
                {"path": r"$PLUGINSDIR\APP-64.7Z", "size": 5},
            ),
            (
                {"path": r"$PLUGINSDIR\app-64.7z", "size": 5},
                {"path": r"$PLUGINSDIR\APP-arm64.7Z", "size": 5},
            ),
        )
        for members in invalid_members:
            with self.subTest(members=members):
                listing = technical_listing("Nsis", members)
                with self.assertRaises(nsis.NsisPayloadError) as raised:
                    nsis.select_app_archive(listing, "x64")
                self.assertEqual(raised.exception.reason, "outer_layout")

    def test_outer_listing_must_be_nsis_and_inner_listing_is_fail_closed(self):
        with self.assertRaises(nsis.NsisPayloadError) as raised:
            nsis.select_app_archive(
                technical_listing("PE", ({
                    "path": r"$PLUGINSDIR\app-64.7z",
                    "size": 5,
                },)),
                "x64",
            )
        self.assertEqual(raised.exception.reason, "outer_format")

        invalid_inner = (
            technical_listing("zip", (
                {"path": "UsageHub.exe"},
                {"path": r"resources\app.asar"},
                {"path": r"resources\collector\openusage-collector.exe"},
            )),
            technical_listing("7z", (
                {"path": "UsageHub.exe"},
                {"path": r"resources\app.asar"},
                {"path": r"resources\collector\openusage-collector.exe"},
                {"path": r"..\escaped.txt"},
            )),
            technical_listing("7z", (
                {"path": "UsageHub.exe"},
                {"path": r"resources\app.asar"},
            )),
            technical_listing("7z", (
                {"path": "UsageHub.exe", "symbolic_link": "target.exe"},
                {"path": r"resources\app.asar"},
                {"path": r"resources\collector\openusage-collector.exe"},
            )),
        )
        for listing in invalid_inner:
            with self.subTest(listing=listing):
                with self.assertRaises(nsis.NsisPayloadError) as raised:
                    nsis.validate_inner_listing(listing)
                self.assertEqual(raised.exception.reason, "inner_layout")

    def test_inner_listing_rejects_aggregate_expansion_over_limit(self):
        listing = technical_listing("7z", (
            {
                "path": "UsageHub.exe",
                "size": nsis.MAX_INNER_ARCHIVE_BYTES // 2,
            },
            {
                "path": r"resources\app.asar",
                "size": nsis.MAX_INNER_ARCHIVE_BYTES // 2,
            },
            {
                "path": r"resources\collector\openusage-collector.exe",
                "size": 1,
            },
        ))
        with self.assertRaises(nsis.NsisPayloadError) as raised:
            nsis.validate_inner_listing(listing)
        self.assertEqual(raised.exception.reason, "inner_layout")


class NsisExtractionTests(unittest.TestCase):
    def test_extracts_with_pinned_7zip_without_executing_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seven_zip = root / "7z.exe"
            installer = root / "UsageHub.exe"
            output = root / "app-root"
            seven_zip.write_bytes(b"trusted-tool")
            installer.write_bytes(b"MZ-built-installer")
            before = hashlib.sha256(installer.read_bytes()).digest()

            def fake_run(arguments, **kwargs):
                command = tuple(str(value) for value in arguments)
                if command[1:] == ("i",):
                    return subprocess.CompletedProcess(
                        arguments,
                        0,
                        stdout=b"7-Zip 26.02 (x64) : Copyright\r\n",
                        stderr=b"",
                    )
                if command[1] == "l" and "-tNsis" in command:
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout=OUTER_X64, stderr=b""
                    )
                if command[1] == "x" and "-tNsis" in command:
                    outer = Path(next(arg[2:] for arg in command if arg.startswith("-o")))
                    archive = outer / "$PLUGINSDIR" / "app-64.7z"
                    archive.parent.mkdir(parents=True)
                    archive.write_bytes(b"inner")
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout=b"Everything is Ok\r\n", stderr=b""
                    )
                if command[1] == "l" and "-t7z" in command:
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout=INNER_WINDOWS, stderr=b""
                    )
                if command[1] == "x" and "-t7z" in command:
                    app_root = Path(next(arg[2:] for arg in command if arg.startswith("-o")))
                    files = {
                        "UsageHub.exe": b"MZ",
                        "resources/app.asar": b"asar",
                        "resources/collector/openusage-collector.exe": b"collector",
                    }
                    for relative, payload in files.items():
                        target = app_root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(payload)
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout=b"Everything is Ok\r\n", stderr=b""
                    )
                self.fail(f"unexpected 7-Zip invocation: {command!r}")

            with mock.patch.object(nsis.subprocess, "run", side_effect=fake_run) as run:
                result = nsis.extract_payload(
                    seven_zip=seven_zip,
                    installer=installer,
                    arch="x64",
                    output=output,
                    expected_seven_zip_version="26.02",
                )

            self.assertEqual(result, output.resolve())
            self.assertEqual(hashlib.sha256(installer.read_bytes()).digest(), before)
            self.assertTrue((output / "UsageHub.exe").is_file())
            self.assertEqual(run.call_count, 5)
            for call in run.call_args_list:
                arguments = tuple(str(value) for value in call.args[0])
                self.assertEqual(arguments[0], str(seven_zip.resolve()))
                self.assertNotEqual(arguments[0], str(installer.resolve()))
                self.assertNotIn("/S", arguments)
                self.assertIs(call.kwargs.get("shell"), False)

    def test_rejects_tool_version_drift_before_parsing_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seven_zip = root / "7z.exe"
            installer = root / "UsageHub.exe"
            output = root / "app-root"
            seven_zip.write_bytes(b"tool")
            installer.write_bytes(b"installer")
            completed = subprocess.CompletedProcess(
                [seven_zip, "i"],
                0,
                stdout=b"7-Zip 26.03 (x64)\r\n",
                stderr=b"",
            )
            with mock.patch.object(nsis.subprocess, "run", return_value=completed) as run:
                with self.assertRaises(nsis.NsisPayloadError) as raised:
                    nsis.extract_payload(
                        seven_zip=seven_zip,
                        installer=installer,
                        arch="x64",
                        output=output,
                        expected_seven_zip_version="26.02",
                    )
            self.assertEqual(raised.exception.reason, "tool_version")
            self.assertEqual(run.call_count, 1)
            self.assertFalse(output.exists())


class WindowsWorkflowContractTests(unittest.TestCase):
    def test_windows_final_container_gate_is_pinned_and_ordered_before_upload(self):
        workflow = WORKFLOW.read_text("utf-8")
        self.assertEqual(workflow.count('"scripts/extract_nsis_payload.py"'), 2)
        self.assertEqual(workflow.count('"tests/test_extract_nsis_payload.py"'), 2)
        self.assertIn("tests.test_extract_nsis_payload", workflow)
        self.assertIn('builderVersion -cne "26.15.3"', workflow)
        self.assertIn('--expected-seven-zip-version "26.02"', workflow)
        self.assertIn("npx electron-builder", workflow)
        self.assertNotIn("/S", workflow)

        resolve = workflow.index("- name: Resolve final artifact")
        audit = workflow.index("- name: Audit final Windows NSIS payload")
        upload = workflow.index("- name: Upload artifact")
        self.assertLess(resolve, audit)
        self.assertLess(audit, upload)

        final_gate = workflow[audit:upload]
        self.assertIn("if: matrix.platform == 'win'", final_gate)
        self.assertIn(
            "FINAL_ARTIFACT: ${{ steps.artifact.outputs.path }}",
            final_gate,
        )
        self.assertIn('"usagehub-nsis-snapshot-"', final_gate)
        self.assertIn(
            "Copy-Item -LiteralPath $env:FINAL_ARTIFACT -Destination $snapshot",
            final_gate,
        )
        self.assertIn(
            "[System.IO.File]::SetAttributes($snapshot, "
            "[System.IO.FileAttributes]::ReadOnly)",
            final_gate,
        )
        self.assertEqual(
            final_gate.count(
                "Get-FileHash -Algorithm SHA256 -LiteralPath "
                "$env:FINAL_ARTIFACT"
            ),
            1,
        )
        self.assertEqual(
            final_gate.count(
                "Get-FileHash -Algorithm SHA256 -LiteralPath $snapshot"
            ),
            2,
        )
        self.assertIn("$finalSnapshotHash -cne $snapshotHash", final_gate)
        self.assertIn("python scripts/extract_nsis_payload.py", final_gate)
        self.assertIn('--installer "$snapshot"', final_gate)
        self.assertIn('--arch "${{ matrix.arch }}"', final_gate)
        self.assertIn("python scripts/release_artifact_audit.py", final_gate)
        self.assertIn('--desktop-package "$appRoot"', final_gate)
        self.assertIn('--built-collector "./dist-collector/${{ matrix.collector }}"', final_gate)
        self.assertIn('--artifact "$snapshot"', final_gate)
        self.assertIn('"path=$snapshot" | Out-File', final_gate)


if __name__ == "__main__":
    unittest.main()
