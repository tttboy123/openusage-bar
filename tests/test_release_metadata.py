import plistlib
import re
import subprocess
import tempfile
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_metadata.py"
OFFICIAL_ACTION_REFERENCE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(actions/[A-Za-z0-9_./-]+)@([^\s#]+)",
    re.MULTILINE,
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def unpinned_official_actions(source):
    return [
        f"{repository}@{reference}"
        for repository, reference in OFFICIAL_ACTION_REFERENCE.findall(source)
        if FULL_COMMIT_SHA.fullmatch(reference) is None
    ]


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        (self.repo / "swift_app/Resources").mkdir(parents=True)
        (self.repo / "openusage_bar").mkdir()
        self.write_metadata("0.3.0", "3")
        self.commit("release 0.3.0")
        subprocess.run(["git", "tag", "v0.3.0"], cwd=self.repo, check=True)
        self.write_metadata("0.4.0", "4")
        self.commit("prepare 0.4.0")

    def tearDown(self):
        self.temporary.cleanup()

    def write_metadata(self, version, build, *, activity_version=None):
        for name in (
            "OpenUsageBar-Info.plist", "OpenUsageActivity-Info.plist",
            "OpenUsageProviderSettings-Info.plist",
        ):
            selected = (
                activity_version
                if name.startswith("OpenUsageActivity") and activity_version
                else version
            )
            (self.repo / "swift_app/Resources" / name).write_bytes(plistlib.dumps({
                "CFBundleShortVersionString": selected, "CFBundleVersion": build,
            }))
        (self.repo / "openusage_bar/bundle_config.py").write_text(
            f'APP_VERSION = "{version}"\nBUILD_VERSION = "{build}"\n', encoding="utf-8"
        )
        (self.repo / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {version} - 2026-07-18\n", encoding="utf-8"
        )

    def commit(self, message):
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Tests", "-c", "user.email=tests@localhost",
             "commit", "-q", "-m", message], cwd=self.repo, check=True,
        )

    def run_verifier(self, *arguments):
        environment = os.environ.copy()
        environment.pop("GITHUB_REF_NAME", None)
        environment.pop("GITHUB_SHA", None)
        return subprocess.run(
            [str(ROOT / ".build-venv/bin/python"), str(VERIFIER),
             "--root", str(self.repo), *arguments],
            capture_output=True, text=True, check=False, env=environment,
        )

    def test_valid_untagged_release_metadata_passes(self):
        result = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "release_metadata_ok version=0.4.0 build=4\n")

    def test_mismatched_helper_version_fails(self):
        self.write_metadata("0.4.0", "4", activity_version="0.4.1")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_reused_build_number_fails(self):
        self.write_metadata("0.4.0", "3")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_missing_changelog_entry_fails(self):
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_tag_not_reachable_from_main_fails(self):
        subprocess.run(
            ["git", "checkout", "-q", "--orphan", "release"], cwd=self.repo, check=True
        )
        self.write_metadata("0.4.0", "4")
        self.commit("detached release")
        subprocess.run(["git", "tag", "v0.4.0"], cwd=self.repo, check=True)
        self.assertNotEqual(self.run_verifier("--tag", "v0.4.0").returncode, 0)

    def test_moved_tag_record_fails(self):
        subprocess.run(["git", "tag", "v0.4.0"], cwd=self.repo, check=True)
        previous = subprocess.run(
            ["git", "rev-parse", "HEAD^"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(
            self.run_verifier(
                "--tag", "v0.4.0", "--expected-commit", previous
            ).returncode,
            0,
        )

    def test_docs_only_commit_after_tag_is_valid(self):
        subprocess.run(["git", "tag", "v0.4.0"], cwd=self.repo, check=True)
        tagged = subprocess.run(
            ["git", "rev-parse", "v0.4.0^{}"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (self.repo / "docs").mkdir()
        (self.repo / "docs/release.md").write_text("clarification\n", encoding="utf-8")
        self.commit("docs only")
        result = self.run_verifier(
            "--tag", "v0.4.0", "--expected-commit", tagged
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CommittedWorkflowMetadataTests(unittest.TestCase):
    def test_full_sha_pin_accepts_new_action_major_version_comment(self):
        source = (
            "- uses: actions/setup-python@"
            "5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0\n"
        )
        self.assertEqual(unpinned_official_actions(source), [])

    def test_tag_reference_is_rejected_even_with_an_expected_version_comment(self):
        source = "- uses: actions/setup-python@v6 # v6\n"
        self.assertEqual(
            unpinned_official_actions(source),
            ["actions/setup-python@v6"],
        )

    def test_each_official_action_reference_is_checked(self):
        source = (
            "- uses: actions/checkout@"
            "93cb6efe18208431cddfb8368fd83d5badbf9bfd # v5\n"
            "- uses: actions/checkout@v5 # unpinned duplicate\n"
        )
        self.assertEqual(
            unpinned_official_actions(source),
            ["actions/checkout@v5"],
        )

    def test_official_action_subpath_is_checked(self):
        source = "- uses: actions/cache/restore@v4\n"
        self.assertEqual(
            unpinned_official_actions(source),
            ["actions/cache/restore@v4"],
        )

    def test_official_actions_are_pinned_to_full_commit_shas(self):
        workflow_directory = ROOT / ".github/workflows"
        workflows = sorted(
            (*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml")),
            key=lambda path: path.name,
        )
        self.assertTrue(workflows, "at least one committed workflow is required")
        for workflow in workflows:
            source = workflow.read_text("utf-8")
            self.assertEqual(
                unpinned_official_actions(source),
                [],
                f"{workflow.name} must pin every official action",
            )


if __name__ == "__main__":
    unittest.main()
