import plistlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.verify_action_pins import action_pin_issues, verify_action_pin_repository


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_metadata.py"
RELEASE_STATE = Path("openusage_bar/resources/release-state.v1.json")
OFFICIAL_ACTION_REFERENCE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(actions/[A-Za-z0-9_./-]+)@([^\s#]+)",
    re.MULTILINE,
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
HASH_PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*"
    r"(?: --hash=sha256:[0-9a-f]{64})+$"
)


def unpinned_official_actions(source):
    return [
        f"{repository}@{reference}"
        for repository, reference in OFFICIAL_ACTION_REFERENCE.findall(source)
        if FULL_COMMIT_SHA.fullmatch(reference) is None
    ]


def setup_metadata_for(platform):
    program = (
        "import json, runpy, sys, types\n"
        "captured = {}\n"
        "setuptools = types.ModuleType('setuptools')\n"
        "setuptools.setup = lambda **kwargs: captured.update(kwargs)\n"
        "sys.modules['setuptools'] = setuptools\n"
        f"sys.platform = {platform!r}\n"
        f"runpy.run_path({str(ROOT / 'setup.py')!r}, run_name='__main__')\n"
        "print(json.dumps(captured))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class LinuxCredentialReleaseMetadataTests(unittest.TestCase):
    REQUIRED_LINUX_PACKAGES = {
        "cffi",
        "cryptography",
        "jeepney",
        "pycparser",
        "secretstorage",
    }

    def test_linux_credential_lock_is_complete_and_hash_pinned(self):
        lock = ROOT / "requirements-linux.txt"

        self.assertTrue(lock.is_file(), "requirements-linux.txt must be committed")
        lines = [
            line.strip()
            for line in lock.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(lines, "requirements-linux.txt must not be empty")
        for line in lines:
            with self.subTest(requirement=line):
                self.assertRegex(line, HASH_PINNED_REQUIREMENT)

        package_names = {
            line.split("==", 1)[0].lower().replace("_", "-").replace(".", "-")
            for line in lines
        }
        self.assertTrue(
            self.REQUIRED_LINUX_PACKAGES.issubset(package_names),
            "Linux credential lock must include secretstorage and its transitive "
            "dependencies",
        )

    def test_setup_exposes_linux_scoped_credential_extra(self):
        metadata = setup_metadata_for("linux")
        extras = metadata.get("extras_require", {})

        self.assertIn("linux-credentials", extras)
        requirements = extras["linux-credentials"]
        self.assertIsInstance(requirements, list)
        package_names = []
        for value in requirements:
            with self.subTest(requirement=value):
                requirement, separator, marker = value.partition(";")
                self.assertEqual(separator, ";")
                self.assertRegex(
                    marker.strip(),
                    r"^sys_platform\s*==\s*(['\"])linux\1$",
                )
                package_names.append(
                    re.split(r"[<>=!~\[\s]", requirement.strip(), maxsplit=1)[0]
                    .lower()
                    .replace("_", "-")
                    .replace(".", "-")
                )
        self.assertEqual(package_names.count("secretstorage"), 1)

    def test_dependency_audit_requires_and_scans_linux_lock(self):
        source = (ROOT / "scripts/audit_dependencies.sh").read_text(encoding="utf-8")
        normalized = source.replace("\\\n", " ")
        preflight = normalized.partition("|| {")[0]
        audit_commands = [
            line for line in normalized.splitlines() if "-m pip_audit" in line
        ]

        self.assertRegex(
            source,
            r'(?m)^LINUX_LOCK="?\$ROOT/requirements-linux\.txt"?$',
        )
        self.assertIn('-f "$LINUX_LOCK"', preflight)
        self.assertTrue(
            any('--requirement "$LINUX_LOCK"' in line for line in audit_commands),
            "dependency audit must scan requirements-linux.txt",
        )


@unittest.skipUnless(sys.platform == "darwin", "macOS release metadata test")
class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        (self.repo / "swift_app/Resources").mkdir(parents=True)
        (self.repo / "openusage_bar").mkdir()
        (self.repo / "openusage_bar/resources").mkdir()
        (self.repo / "docs").mkdir(exist_ok=True)
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
        (self.repo / "docs/release-quick-start.md").write_text(
            f"OpenUsage Bar {version}\n"
            f"Download OpenUsage-Bar-v{version}-macos-arm64.dmg.\n",
            encoding="utf-8",
        )
        for readme in ("README.md", "README.en.md"):
            (self.repo / readme).write_text(
                f"<!-- openusage-release-version: {version} -->\n"
                f"[Download](https://github.com/tttboy123/openusage-bar/releases/"
                f"download/v{version}/OpenUsage-Bar-v{version}-macos-arm64.dmg)\n",
                encoding="utf-8",
            )
        release_state = {
            "schemaVersion": 1,
            "currentVersion": version,
            "buildVersion": build,
            "channel": "rc",
            "apiVersion": "1.0",
            "canary": {
                "qualifiedMachines": 0,
                "targetMachines": 5,
                "clock": "not_started",
            },
        }
        (self.repo / RELEASE_STATE).write_text(
            json.dumps(release_state, indent=2) + "\n", encoding="utf-8"
        )
        (self.repo / "ROADMAP.md").write_text(
            "<!-- openusage-release-state: "
            f"version={version} channel=rc api=1.0 "
            "canary=0/5 clock=not_started -->\n",
            encoding="utf-8",
        )
        self.write_setup_metadata(version, version)
        for application in ("web", "desktop"):
            self.write_package_metadata(application, version)
        (self.repo / "docs/canary.md").write_text(
            f"The current candidate version is `{version}`:\n\n"
            f"OpenUsage-Bar-v{version}-macos-arm64.zip\n"
            f"--source-ref refs/tags/v{version}\n"
            f"scripts/verify_canary_candidate.py --version {version}\n",
            encoding="utf-8",
        )

    def write_setup_metadata(self, non_darwin_version, darwin_version):
        (self.repo / "setup.py").write_text(
            "import sys\n\n"
            "from setuptools import setup\n\n"
            f"common = {{'name': 'openusage-bar', 'version': "
            f"{non_darwin_version!r}}}\n"
            "if sys.platform == 'darwin':\n"
            f"    common.update(version={darwin_version!r})\n"
            "setup(**common)\n",
            encoding="utf-8",
        )

    def write_package_metadata(self, application, version):
        directory = self.repo / application
        directory.mkdir(exist_ok=True)
        package_name = f"usagehub-{application}"
        package = {"name": package_name, "version": version, "private": True}
        lock = {
            "name": package_name,
            "version": version,
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": package_name, "version": version}},
        }
        (directory / "package.json").write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        (directory / "package-lock.json").write_text(
            json.dumps(lock, indent=2) + "\n", encoding="utf-8"
        )

    def release_version(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        return state["currentVersion"]

    def release_build(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        return state["buildVersion"]

    def previous_release_version(self):
        result = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=-version:refname"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        )
        return result.stdout.splitlines()[0].removeprefix("v")

    def write_json_version(self, relative_path, keys, version):
        path = self.repo / relative_path
        payload = json.loads(path.read_text("utf-8"))
        target = payload
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = version
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

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
        self.assertEqual(
            result.stdout,
            f"release_metadata_ok version={self.release_version()} "
            f"build={self.release_build()}\n",
        )

    def test_stale_non_darwin_setup_version_fails(self):
        candidate = self.release_version()
        stale = self.previous_release_version()
        self.write_setup_metadata(stale, candidate)

        result = self.run_verifier()

        self.assertNotEqual(
            result.returncode,
            0,
            "setup.py non-Darwin version drift was accepted: "
            f"expected {candidate}, found {stale}; stdout={result.stdout!r}",
        )

    def test_stale_web_package_versions_fail(self):
        self.assert_stale_package_versions_fail("web")

    def test_stale_desktop_package_versions_fail(self):
        self.assert_stale_package_versions_fail("desktop")

    def assert_stale_package_versions_fail(self, application):
        candidate = self.release_version()
        stale = self.previous_release_version()
        cases = (
            (Path(application) / "package.json", ("version",)),
            (Path(application) / "package-lock.json", ("version",)),
            (
                Path(application) / "package-lock.json",
                ("packages", "", "version"),
            ),
        )
        for relative_path, keys in cases:
            field = ".".join(key or "<root>" for key in keys)
            with self.subTest(
                application=application, file=str(relative_path), field=field
            ):
                self.write_package_metadata(application, candidate)
                self.write_json_version(relative_path, keys, stale)

                result = self.run_verifier()

                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"{relative_path}:{field} version drift was accepted: "
                    f"expected {candidate}, found {stale}; stdout={result.stdout!r}",
                )

    def test_stale_canary_candidate_version_fails(self):
        candidate = self.release_version()
        stale = self.previous_release_version()
        path = self.repo / "docs/canary.md"
        source = path.read_text("utf-8")
        self.assertGreater(source.count(candidate), 1)
        path.write_text(source.replace(candidate, stale, 1), encoding="utf-8")

        result = self.run_verifier()

        self.assertNotEqual(
            result.returncode,
            0,
            "docs/canary.md candidate version drift was accepted: "
            f"expected {candidate}, found {stale}; stdout={result.stdout!r}",
        )

    def test_mismatched_helper_version_fails(self):
        self.write_metadata("0.4.0", "4", activity_version="0.4.1")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_reused_build_number_fails(self):
        self.write_metadata("0.4.0", "3")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_missing_changelog_entry_fails(self):
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_stale_release_guide_version_fails(self):
        (self.repo / "docs/release-quick-start.md").write_text(
            "OpenUsage Bar 0.3.0\n"
            "Download OpenUsage-Bar-v0.3.0-macos-arm64.dmg.\n",
            encoding="utf-8",
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_stale_chinese_readme_release_version_fails(self):
        (self.repo / "README.md").write_text(
            "<!-- openusage-release-version: 0.3.0 -->\n"
            "[Download](https://github.com/tttboy123/openusage-bar/releases/"
            "download/v0.3.0/OpenUsage-Bar-v0.3.0-macos-arm64.dmg)\n",
            encoding="utf-8",
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_stale_english_readme_download_version_fails(self):
        (self.repo / "README.en.md").write_text(
            "<!-- openusage-release-version: 0.4.0 -->\n"
            "[Download](https://github.com/tttboy123/openusage-bar/releases/"
            "download/v0.3.0/OpenUsage-Bar-v0.3.0-macos-arm64.dmg)\n",
            encoding="utf-8",
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_release_state_version_must_match_bundle(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        state["currentVersion"] = "0.3.0"
        (self.repo / RELEASE_STATE).write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_release_state_build_must_match_bundle(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        state["buildVersion"] = "3"
        (self.repo / RELEASE_STATE).write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_release_state_rejects_unknown_fields(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        state["private"] = "value"
        (self.repo / RELEASE_STATE).write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_release_state_rejects_invalid_canary_counts(self):
        state = json.loads((self.repo / RELEASE_STATE).read_text("utf-8"))
        state["canary"]["qualifiedMachines"] = 6
        (self.repo / RELEASE_STATE).write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.assertNotEqual(self.run_verifier().returncode, 0)

    def test_roadmap_release_state_marker_must_match(self):
        (self.repo / "ROADMAP.md").write_text(
            "<!-- openusage-release-state: version=0.3.0 channel=rc "
            "api=1.0 canary=0/5 clock=not_started -->\n",
            encoding="utf-8",
        )
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
        (self.repo / "docs").mkdir(exist_ok=True)
        (self.repo / "docs/release.md").write_text("clarification\n", encoding="utf-8")
        self.commit("docs only")
        result = self.run_verifier(
            "--tag", "v0.4.0", "--expected-commit", tagged
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(sys.platform == "darwin", "macOS release workflow test")
class CommittedWorkflowMetadataTests(unittest.TestCase):
    def test_mismatched_human_version_comment_is_rejected(self):
        commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        source = f"- uses: actions/checkout@{commit} # v5\n"
        approved = {
            ("actions/checkout", commit): "v7.0.1",
        }

        self.assertEqual(
            action_pin_issues(source, approved),
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 "
                "must use version comment v7.0.1, found v5"
            ],
        )

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


@unittest.skipUnless(sys.platform == "darwin", "macOS action pin test")
class ActionPinRepositoryTests(unittest.TestCase):
    def test_exact_manifest_pin_and_version_comment_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f"- uses: actions/checkout@{commit} # v7.0.1\n",
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "pins": [{
                        "repository": "actions/checkout",
                        "commit": commit,
                        "version": "v7.0.1",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(verify_action_pin_repository(root), [])

    def test_unapproved_full_sha_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f"- uses: actions/checkout@{commit} # v7.0.1\n",
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({"schemaVersion": 1, "pins": []}),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "ci.yml: actions/checkout@"
                    "3d3c42e5aac5ba805825da76410c181273ba90b1 "
                    "is not present in .github/action-pins.json"
                ],
            )

    def test_quoted_unapproved_action_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f'- uses: "actions/checkout@{commit}" # v7.0.1\n',
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({"schemaVersion": 1, "pins": []}),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "ci.yml: actions/checkout@"
                    "3d3c42e5aac5ba805825da76410c181273ba90b1 "
                    "is not present in .github/action-pins.json"
                ],
            )

    def test_stale_manifest_pin_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".github/workflows").mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (root / ".github/action-pins.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "pins": [{
                        "repository": "actions/checkout",
                        "commit": commit,
                        "version": "v7.0.1",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "action-pins.json: actions/checkout@"
                    "3d3c42e5aac5ba805825da76410c181273ba90b1 is not used "
                    "by a committed workflow"
                ],
            )

    def test_non_full_sha_is_rejected_even_when_listed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            (workflow_directory / "ci.yml").write_text(
                "- uses: actions/checkout@v7.0.1 # v7.0.1\n",
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "pins": [{
                        "repository": "actions/checkout",
                        "commit": "v7.0.1",
                        "version": "v7.0.1",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "action-pins.json: pin 1 commit must be a full lowercase "
                    "40-character SHA"
                ],
            )

    def test_manifest_version_must_be_an_exact_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f"- uses: actions/checkout@{commit} # v7\n",
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "pins": [{
                        "repository": "actions/checkout",
                        "commit": commit,
                        "version": "v7",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "action-pins.json: pin 1 version must be an exact vX.Y.Z "
                    "release tag"
                ],
            )

    def test_trailing_comment_text_cannot_hide_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f"- uses: actions/checkout@{commit} # v5 stale comment\n",
                encoding="utf-8",
            )
            (root / ".github/action-pins.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "pins": [{
                        "repository": "actions/checkout",
                        "commit": commit,
                        "version": "v7.0.1",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "ci.yml: actions/checkout@"
                    "3d3c42e5aac5ba805825da76410c181273ba90b1 "
                    "must use version comment v7.0.1, found v5 stale comment"
                ],
            )

    def test_duplicate_manifest_pin_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow_directory = root / ".github/workflows"
            workflow_directory.mkdir(parents=True)
            commit = "3d3c42e5aac5ba805825da76410c181273ba90b1"
            (workflow_directory / "ci.yml").write_text(
                f"- uses: actions/checkout@{commit} # v7.0.1\n",
                encoding="utf-8",
            )
            pin = {
                "repository": "actions/checkout",
                "commit": commit,
                "version": "v7.0.1",
            }
            (root / ".github/action-pins.json").write_text(
                json.dumps({"schemaVersion": 1, "pins": [pin, pin]}),
                encoding="utf-8",
            )

            self.assertEqual(
                verify_action_pin_repository(root),
                [
                    "action-pins.json: duplicate pin actions/checkout@"
                    "3d3c42e5aac5ba805825da76410c181273ba90b1"
                ],
            )

    def test_committed_workflows_match_action_pin_manifest(self):
        self.assertEqual(verify_action_pin_repository(ROOT), [])


if __name__ == "__main__":
    unittest.main()
