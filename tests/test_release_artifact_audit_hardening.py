import tempfile
import unittest
from pathlib import Path

from scripts.release_artifact_audit import ArtifactError, inspect_desktop_package
from tests.test_release_artifact_audit import write_desktop_package


class DesktopArtifactAuditHardeningTests(unittest.TestCase):
    def test_collector_directory_rejects_every_extra_regular_or_link_member(self):
        for extra_kind in ("regular", "symlink"):
            with self.subTest(extra_kind=extra_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector = write_desktop_package(root, "linux")
                extra = collector.with_name("unexpected-collector-member")
                if extra_kind == "regular":
                    extra.write_bytes(b"unexpected")
                else:
                    extra.symlink_to(collector.name)

                with self.assertRaises(ArtifactError) as raised:
                    inspect_desktop_package(root, "linux")

                self.assertEqual(raised.exception.reason, "collector")


if __name__ == "__main__":
    unittest.main()
