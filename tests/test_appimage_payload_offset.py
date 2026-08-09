import io
import os
import re
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.appimage_payload_offset import (
    AppImagePayloadError,
    appimage_payload_offset,
    main,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"
PINNED_SQUASHFS_TOOLS = "1:4.6.1-1build1"


def _squashfs_payload(*, bytes_used: int = 96) -> bytes:
    payload = bytearray(max(96, bytes_used))
    struct.pack_into(
        "<IIIIIHHHHHHQQ",
        payload,
        0,
        0x73717368,
        1,
        0,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        bytes_used,
    )
    return bytes(payload)


def _appimage(*, machine: int = 62, squashfs: bytes | None = None) -> tuple[bytes, int]:
    ident = bytearray(16)
    ident[:7] = b"\x7fELF\x02\x01\x01"
    ident[8:11] = b"AI\x02"
    section_offset = 64
    section_size = 64
    section_count = 2
    section_data_offset = section_offset + section_size * section_count
    section_data_size = 16
    payload_offset = section_data_offset + section_data_size

    image = bytearray(payload_offset)
    image[:64] = struct.pack(
        "<16sHHIQQQIHHHHHH",
        bytes(ident),
        2,
        machine,
        1,
        0,
        0,
        section_offset,
        0,
        64,
        0,
        0,
        section_size,
        section_count,
        0,
    )
    image[section_offset + section_size : section_offset + section_size * 2] = (
        struct.pack(
            "<IIQQQQIIQQ",
            0,
            1,
            0,
            0,
            section_data_offset,
            section_data_size,
            0,
            0,
            1,
            0,
        )
    )
    image.extend(_squashfs_payload() if squashfs is None else squashfs)
    return bytes(image), payload_offset


class AppImagePayloadOffsetTests(unittest.TestCase):
    def _write(self, directory: str, payload: bytes, name: str = "UsageHub.AppImage") -> Path:
        path = Path(directory) / name
        path.write_bytes(payload)
        return path

    def test_returns_the_appended_squashfs_offset_without_executing_the_image(self):
        payload, expected = _appimage()
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, payload)

            observed = appimage_payload_offset(path, "x64")

        self.assertEqual(observed, expected)

    def test_accepts_arm64_and_rejects_an_architecture_name_mismatch(self):
        payload, expected = _appimage(machine=183)
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, payload)
            self.assertEqual(appimage_payload_offset(path, "arm64"), expected)
            with self.assertRaisesRegex(AppImagePayloadError, "architecture"):
                appimage_payload_offset(path, "x64")

    def test_rejects_non_type_two_magic_and_non_squashfs_payloads(self):
        payload, offset = _appimage()
        cases = {
            "appimage": payload[:8] + b"AI\x01" + payload[11:],
            "squashfs": payload[:offset] + b"nope" + payload[offset + 4 :],
        }
        with tempfile.TemporaryDirectory() as directory:
            for reason, candidate in cases.items():
                with self.subTest(reason=reason):
                    path = self._write(directory, candidate, f"{reason}.AppImage")
                    with self.assertRaisesRegex(AppImagePayloadError, reason):
                        appimage_payload_offset(path, "x64")

    def test_rejects_section_tables_and_squashfs_lengths_outside_the_file(self):
        payload, offset = _appimage()
        bad_table = bytearray(payload)
        struct.pack_into("<Q", bad_table, 40, len(payload) + 1)
        bad_length = bytearray(payload)
        struct.pack_into("<Q", bad_length, offset + 40, len(payload) + 1)
        cases = {
            "bounds-table": bytes(bad_table),
            "bounds-payload": bytes(bad_length),
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, candidate in cases.items():
                with self.subTest(name=name):
                    path = self._write(directory, candidate, f"{name}.AppImage")
                    with self.assertRaisesRegex(AppImagePayloadError, "bounds"):
                        appimage_payload_offset(path, "x64")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_a_symlink_instead_of_following_it(self):
        payload, _ = _appimage()
        with tempfile.TemporaryDirectory() as directory:
            target = self._write(directory, payload, "target.AppImage")
            link = Path(directory) / "linked.AppImage"
            link.symlink_to(target)

            with self.assertRaisesRegex(AppImagePayloadError, "file"):
                appimage_payload_offset(link, "x64")

    def test_cli_emits_only_the_decimal_offset_or_one_closed_error(self):
        payload, expected = _appimage()
        with tempfile.TemporaryDirectory() as directory:
            valid = self._write(directory, payload)
            output = io.StringIO()
            error = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(["--appimage", str(valid), "--architecture", "x64"])
            self.assertEqual((code, output.getvalue(), error.getvalue()), (0, f"{expected}\n", ""))

            missing = Path(directory) / "private-machine-name.AppImage"
            output = io.StringIO()
            error = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(["--appimage", str(missing), "--architecture", "x64"])
            self.assertEqual(code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(error.getvalue(), "appimage_payload_invalid reason=file\n")
            self.assertNotIn(str(missing), error.getvalue())

    def test_cli_rejects_unknown_or_reordered_arguments_without_argparse_output(self):
        for arguments in (
            [],
            ["--architecture", "x64", "--appimage", "UsageHub.AppImage"],
            ["--appimage", "UsageHub.AppImage", "--architecture", "riscv64"],
        ):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                error = io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = main(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(error.getvalue(), "appimage_payload_invalid reason=input\n")


class LinuxFinalContainerWorkflowTests(unittest.TestCase):
    def test_linux_final_container_is_extracted_without_running_the_appimage(self):
        workflow = WORKFLOW.read_text("utf-8")
        self.assertEqual(workflow.count('"scripts/appimage_payload_offset.py"'), 2)
        self.assertEqual(workflow.count('"tests/test_appimage_payload_offset.py"'), 2)
        contracts_start = workflow.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts_end = workflow.index("\n      - name:", contracts_start + 1)
        active_targets = {
            line.strip().removesuffix("\\").strip()
            for line in workflow[contracts_start:contracts_end].splitlines()
            if line.strip().startswith("tests.")
        }
        self.assertIn("tests.test_appimage_payload_offset", active_targets)
        self.assertIn(
            f"squashfs-tools={PINNED_SQUASHFS_TOOLS}",
            workflow,
        )
        self.assertNotIn("--appimage-extract", workflow)
        self.assertNotIn("--appimage-offset", workflow)

        resolve = workflow.index("- name: Resolve final artifact")
        audit = workflow.index("- name: Audit final Linux AppImage payload")
        upload = workflow.index("- name: Upload artifact")
        self.assertLess(resolve, audit)
        self.assertLess(audit, upload)

        final_gate = workflow[audit:upload]
        self.assertIn("if: matrix.platform == 'linux'", final_gate)
        self.assertIn(
            "FINAL_ARTIFACT: ${{ steps.artifact.outputs.path }}",
            final_gate,
        )
        self.assertIn(
            'snapshot_root="$(mktemp -d '
            '"$RUNNER_TEMP/usagehub-appimage-snapshot.XXXXXX")"',
            final_gate,
        )
        self.assertIn('/bin/cp "$FINAL_ARTIFACT" "$snapshot"', final_gate)
        self.assertIn('/bin/chmod 0400 "$snapshot"', final_gate)
        self.assertEqual(
            final_gate.count('/usr/bin/cmp -s "$FINAL_ARTIFACT" "$snapshot"'),
            1,
        )
        self.assertEqual(
            final_gate.count('/usr/bin/sha256sum "$snapshot"'),
            2,
        )
        self.assertIn(
            'test "$final_snapshot_digest" = "$snapshot_digest"',
            final_gate,
        )
        self.assertIn("python scripts/appimage_payload_offset.py", final_gate)
        self.assertIn('--appimage "$snapshot"', final_gate)
        self.assertIn('--architecture "${{ matrix.arch }}"', final_gate)
        self.assertIn("unsquashfs", final_gate)
        self.assertIn('-offset "$payload_offset"', final_gate)
        self.assertIn('-dest "$appRoot" "$snapshot"', final_gate)
        self.assertIn("python scripts/release_artifact_audit.py", final_gate)
        self.assertIn('--desktop-package "$appRoot"', final_gate)
        self.assertIn(
            '--built-collector "./dist-collector/${{ matrix.collector }}"',
            final_gate,
        )
        artifact_reference = (
            r"\$\{\{\s*steps\.artifact\.outputs\.path\s*\}\}"
        )
        logical_gate = re.sub(r"\\\s*\n\s*", " ", final_gate)
        self.assertIsNone(
            re.search(
                rf"(?m)^\s*(?:(?:exec|bash|sh|zsh|env|command|sudo|timeout|"
                rf"python3?|node|source|\.)\s+)?"
                rf"[\"']?{artifact_reference}[\"']?(?:\s|$)",
                logical_gate,
            ),
            "the AppImage artifact must only be data passed to the parser and "
            "unsquashfs, never a command or shell input",
        )
        self.assertIn('--artifact "$snapshot"', final_gate)
        self.assertIn("printf 'path=%s\\n' \"$snapshot\"", final_gate)


if __name__ == "__main__":
    unittest.main()
