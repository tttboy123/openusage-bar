import tempfile
import unittest
from pathlib import Path

from scripts.release_artifact_audit import ArtifactError, inspect_desktop_package
from tests.test_release_artifact_audit import write_desktop_package


ONE_MIB = 1024 * 1024
UPSTREAM_ELECTRON_URL_CONTEXT = (
    b"https://dns.quad9.net/dns-query\0Quad9 (9.9.9.9)\0"
    b"https://www.quad9.net/home/privacy/\0Quickline\0"
)


def write_native_macos_package(root: Path, payload: bytes) -> None:
    write_desktop_package(root, "darwin", asar_extra=payload)


class DesktopArtifactAuditURLPathTests(unittest.TestCase):
    def test_upstream_electron_url_route_is_not_a_local_home_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_native_macos_package(root, UPSTREAM_ELECTRON_URL_CONTEXT)

            inspect_desktop_package(root, "darwin")

    def test_actual_home_paths_are_rejected_even_across_scan_chunks(self):
        home_paths = (
            b"/home/release/private",
            b"/Users/release/private",
            b"C:\\Users\\release\\private",
        )
        for home_path in home_paths:
            for crosses_boundary in (False, True):
                with self.subTest(
                    home_path=home_path,
                    crosses_boundary=crosses_boundary,
                ), tempfile.TemporaryDirectory() as directory:
                    if crosses_boundary:
                        split_at = len(home_path) // 2
                        payload = b"x" * (ONE_MIB - split_at) + home_path
                    else:
                        payload = b"built at " + home_path
                    root = Path(directory)
                    write_native_macos_package(root, payload)

                    with self.assertRaises(ArtifactError) as raised:
                        inspect_desktop_package(root, "darwin")

                    self.assertEqual(raised.exception.reason, "home_path")


if __name__ == "__main__":
    unittest.main()
