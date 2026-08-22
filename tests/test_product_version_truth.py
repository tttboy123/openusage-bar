from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_product_version_truth.py"
CONTRACT_PATH = Path("openusage_bar/resources/product-version-truth.v1.json")
CONTRACT = ROOT / CONTRACT_PATH
RELEASE_STATE = Path("openusage_bar/resources/release-state.v1.json")

FIXTURE_FILES = (
    Path("README.md"),
    Path("README.en.md"),
    Path("ROADMAP.md"),
    Path("setup.py"),
    Path("desktop/main.js"),
    Path("desktop/package.json"),
    Path("desktop/package-lock.json"),
    Path("desktop/product_version_truth.js"),
    Path("desktop/runtime_capability.js"),
    Path("openusage_bar/bundle_config.py"),
    Path("openusage_bar/gateway/api.py"),
    Path("openusage_bar/gateway/response.py"),
    Path("openusage_bar/gateway/runtime.py"),
    Path("openusage_bar/resources/gateway-api-v1.schema.json"),
    Path("openusage_bar/resources/gateway-response-v1.schema.json"),
    Path("openusage_bar/resources/local-api-v1.schema.json"),
    Path("openusage_bar/resources/product-version-truth.v1.json"),
    Path("openusage_bar/resources/release-state.v1.json"),
    Path("openusage_bar/resources/runtime-capability-v1.schema.json"),
    Path("openusage_bar/runtime_capabilities.py"),
    Path("openusage_bar/ui.py"),
    Path("openusage_bar/web_dashboard.py"),
    Path("scripts/distribution_trust_posture.py"),
    Path("scripts/native_ci_evidence.py"),
    Path("scripts/release_artifact_audit.py"),
    Path("scripts/release_handoff.py"),
    Path("scripts/verify_canary_candidate.py"),
    Path("swift_app/Resources/OpenUsageActivity-Info.plist"),
    Path("swift_app/Resources/OpenUsageBar-Info.plist"),
    Path("swift_app/Resources/OpenUsageProviderSettings-Info.plist"),
    Path("swift_app/Sources/OpenUsageActivity/ActivityViews.swift"),
    Path("swift_app/Sources/OpenUsageBar/MenuLogic.swift"),
    Path("swift_app/Sources/UsageCore/ProductVersionTruth.swift"),
    Path("web/index.html"),
    Path("web/package.json"),
    Path("web/package-lock.json"),
    Path("web/src/App.tsx"),
    Path("web/src/components/MenuBarPopover.tsx"),
    Path("web/src/components/TrayMenu.tsx"),
    Path("web/src/productVersionTruth.ts"),
    Path("web/src/runtimeCapability.ts"),
)


def _run_verifier(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(old) < 1:
        raise AssertionError(f"fixture marker is missing: {old!r}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


@contextmanager
def _repository_fixture() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix="product-version-truth-fixture-"
    ) as directory:
        root = Path(directory)
        for relative in FIXTURE_FILES:
            source = ROOT / relative
            if not source.is_file():
                raise AssertionError(f"required fixture source is missing: {relative}")
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["git", "config", "maintenance.auto", "false"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["git", "config", "gc.auto", "0"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Product Version Truth Tests",
                "-c",
                "user.email=tests@localhost",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["git", "tag", "v0.7.1"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        yield root


class ProductVersionTruthTestCase(unittest.TestCase):
    def assert_invalid(
        self,
        result: subprocess.CompletedProcess[str],
        *private_values: str,
    ) -> None:
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "product_version_truth_invalid\n")
        self.assertNotIn("Traceback", result.stderr)
        for value in private_values:
            self.assertNotIn(value, result.stdout + result.stderr)

    def assert_json_mutations_invalid(
        self,
        relative: Path,
        mutations: tuple[
            tuple[str, Callable[[dict[str, object]], None]], ...
        ],
    ) -> None:
        with _repository_fixture() as root:
            path = root / relative
            original = path.read_bytes()
            for label, mutate in mutations:
                with self.subTest(mutation=label):
                    path.write_bytes(original)
                    payload = json.loads(original)
                    mutate(payload)
                    _write_json(path, payload)
                    self.assert_invalid(_run_verifier(root))
            path.write_bytes(original)

    def assert_text_mutations_invalid(
        self,
        mutations: tuple[tuple[str, Path, str, str], ...],
    ) -> None:
        with _repository_fixture() as root:
            for label, relative, old, new in mutations:
                with self.subTest(mutation=label):
                    path = root / relative
                    original = path.read_bytes()
                    try:
                        _replace_once(path, old, new)
                        self.assert_invalid(_run_verifier(root))
                    finally:
                        path.write_bytes(original)


class ProductVersionTruthCommittedContractTests(ProductVersionTruthTestCase):
    def test_committed_contract_is_closed_and_separates_version_domains(self):
        self.assertTrue(CONTRACT.is_file())
        self.assertTrue(SCRIPT.is_file())
        payload = json.loads(CONTRACT.read_text(encoding="utf-8"))

        self.assertEqual(
            set(payload),
            {
                "candidate",
                "interfaces",
                "product",
                "publishedBaseline",
                "releaseTracks",
                "schemaVersion",
            },
        )
        self.assertEqual(payload["schemaVersion"], "product-version-truth/v1")
        self.assertEqual(payload["product"]["displayName"], "UsageHub")
        self.assertEqual(
            payload["candidate"],
            {
                "build": "29",
                "canary": {
                    "clock": "not_started",
                    "qualifiedMachines": 0,
                    "targetMachines": 5,
                },
                "channel": "rc",
                "publicationReceipt": None,
                "publicationStatus": "not_published",
                "releaseEligible": False,
                "releaseStage": "candidate",
                "version": "0.8.7",
            },
        )
        self.assertEqual(payload["publishedBaseline"]["version"], "0.7.1")
        self.assertEqual(payload["publishedBaseline"]["tag"], "v0.7.1")
        self.assertNotEqual(
            payload["candidate"]["version"],
            payload["publishedBaseline"]["version"],
        )
        self.assertEqual(
            payload["interfaces"],
            {
                "gateway": "gateway.openusage/v1",
                "localApi": "1.0",
                "runtimeCapability": "runtime-capability.openusage/v1",
            },
        )
        self.assertEqual(len(set(payload["interfaces"].values())), 3)

    def test_actual_repository_verifies_with_safe_candidate_summary(self):
        result = _run_verifier(ROOT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "product_version_truth_ok product=UsageHub candidate=0.8.7 "
            "build=29 channel=rc published=v0.7.1\n",
        )

    def test_current_candidate_cannot_cross_the_release_publication_gate(self):
        result = _run_verifier(ROOT, "--require-release-eligible")

        self.assert_invalid(result)
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        gate = (
            ".build-venv/bin/python scripts/verify_product_version_truth.py "
            "--require-release-eligible"
        )
        self.assertIn(gate, workflow)
        self.assertLess(workflow.index(gate), workflow.index("Attest release assets"))
        self.assertLess(
            workflow.index(gate), workflow.index("Probe pre-release existence")
        )
        self.assertLess(
            workflow.index(gate), workflow.index("Create pre-release when absent")
        )

    def test_copied_repository_verifies_without_platform_dependencies(self):
        with _repository_fixture() as root:
            result = _run_verifier(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("candidate=0.8.7", result.stdout)


class ProductVersionTruthClosedContractTests(ProductVersionTruthTestCase):
    def test_unknown_top_level_and_nested_fields_are_rejected(self):
        def add_root(payload: dict[str, object]) -> None:
            payload["privatePath"] = "/private/version"

        def add_candidate(payload: dict[str, object]) -> None:
            payload["candidate"]["published"] = True

        def add_canary(payload: dict[str, object]) -> None:
            payload["candidate"]["canary"]["deviceId"] = "private-device"

        def add_product(payload: dict[str, object]) -> None:
            payload["product"]["marketingAlias"] = "UsageHub Pro"

        def add_track(payload: dict[str, object]) -> None:
            first_track = next(iter(payload["releaseTracks"].values()))
            first_track["unreviewedPolicy"] = "publish"

        self.assert_json_mutations_invalid(
            CONTRACT_PATH,
            (
                ("top-level", add_root),
                ("candidate", add_candidate),
                ("canary", add_canary),
                ("product", add_product),
                ("release-track", add_track),
            ),
        )

    def test_candidate_cannot_be_eligible_or_claim_publication(self):
        def eligible(payload: dict[str, object]) -> None:
            payload["candidate"]["releaseEligible"] = True

        def published_stage(payload: dict[str, object]) -> None:
            payload["candidate"]["releaseStage"] = "published"

        def published_status(payload: dict[str, object]) -> None:
            payload["candidate"]["publicationStatus"] = "published"

        self.assert_json_mutations_invalid(
            CONTRACT_PATH,
            (
                ("release-eligible", eligible),
                ("published-stage", published_stage),
                ("published-status", published_status),
            ),
        )

    def test_unpublished_candidate_cannot_claim_the_stable_channel(self):
        def stable_channel(payload: dict[str, object]) -> None:
            payload["candidate"]["channel"] = "stable"

        self.assert_json_mutations_invalid(
            CONTRACT_PATH,
            (("stable-channel", stable_channel),),
        )

    def test_candidate_must_be_newer_than_the_published_baseline(self):
        def same_as_candidate(payload: dict[str, object]) -> None:
            version = payload["candidate"]["version"]
            payload["publishedBaseline"]["version"] = version
            payload["publishedBaseline"]["tag"] = f"v{version}"

        def newer_than_candidate(payload: dict[str, object]) -> None:
            payload["publishedBaseline"]["version"] = "0.9.0"
            payload["publishedBaseline"]["tag"] = "v0.9.0"

        self.assert_json_mutations_invalid(
            CONTRACT_PATH,
            (
                ("equal", same_as_candidate),
                ("baseline-newer", newer_than_candidate),
            ),
        )

    def test_local_gateway_and_runtime_versions_cannot_be_conflated(self):
        def local_is_gateway(payload: dict[str, object]) -> None:
            payload["interfaces"]["localApi"] = payload["interfaces"]["gateway"]

        def gateway_is_runtime(payload: dict[str, object]) -> None:
            payload["interfaces"]["gateway"] = payload["interfaces"][
                "runtimeCapability"
            ]

        def runtime_is_local(payload: dict[str, object]) -> None:
            payload["interfaces"]["runtimeCapability"] = payload["interfaces"][
                "localApi"
            ]

        self.assert_json_mutations_invalid(
            CONTRACT_PATH,
            (
                ("local-is-gateway", local_is_gateway),
                ("gateway-is-runtime", gateway_is_runtime),
                ("runtime-is-local", runtime_is_local),
            ),
        )


class ProductVersionTruthReleaseStateMachineTests(ProductVersionTruthTestCase):
    PROJECTION_PATHS = (
        Path("desktop/product_version_truth.js"),
        Path("web/src/productVersionTruth.ts"),
        Path("swift_app/Sources/UsageCore/ProductVersionTruth.swift"),
    )
    MARKER_PATHS = (Path("README.md"), Path("README.en.md"), Path("ROADMAP.md"))

    def transition(
        self,
        root: Path,
        *,
        stage: str,
        publication: str,
        eligible: bool,
        receipt: dict[str, object] | None,
    ) -> None:
        contract = root / CONTRACT_PATH
        payload = json.loads(contract.read_text(encoding="utf-8"))
        candidate = payload["candidate"]
        candidate["releaseStage"] = stage
        candidate["publicationStatus"] = publication
        candidate["releaseEligible"] = eligible
        candidate["publicationReceipt"] = receipt
        _write_json(contract, payload)

        for relative in self.PROJECTION_PATHS:
            source = (root / relative).read_text(encoding="utf-8")
            source = source.replace(
                'releaseStage: "candidate"', f'releaseStage: "{stage}"', 1
            )
            source = source.replace(
                'publicationStatus: "not_published"',
                f'publicationStatus: "{publication}"',
                1,
            )
            source = source.replace(
                "releaseEligible: false",
                f"releaseEligible: {str(eligible).lower()}",
                1,
            )
            (root / relative).write_text(source, encoding="utf-8")

        for relative in self.MARKER_PATHS:
            source = (root / relative).read_text(encoding="utf-8")
            source = source.replace(
                "stage=candidate publication=not_published",
                f"stage={stage} publication={publication}",
                1,
            )
            (root / relative).write_text(source, encoding="utf-8")

    def receipt(self, root: Path) -> dict[str, object]:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return {
            "assetSetSha256": "b" * 64,
            "manifestSha256": "a" * 64,
            "prerelease": True,
            "provenanceAttestation": "verified",
            "publishedAt": "2026-08-09T12:00:00Z",
            "releaseId": "123456789",
            "repository": "tttboy123/openusage-bar",
            "schemaVersion": "github-release-receipt/v1",
            "sourceSha": source_sha,
            "tag": "v0.8.7",
        }

    def test_only_the_three_release_state_rows_are_valid(self):
        valid_rows = (
            ("candidate", "not_published", False, None, False),
            ("prerelease_ready", "not_published", True, None, True),
            (
                "prerelease_published",
                "published_prerelease",
                False,
                "receipt",
                False,
            ),
        )
        for stage, publication, eligible, receipt_kind, gate_allowed in valid_rows:
            with self.subTest(stage=stage), _repository_fixture() as root:
                receipt = self.receipt(root) if receipt_kind == "receipt" else None
                if receipt is not None:
                    subprocess.run(["git", "tag", "v0.8.7"], cwd=root, check=True)
                self.transition(
                    root,
                    stage=stage,
                    publication=publication,
                    eligible=eligible,
                    receipt=receipt,
                )

                result = _run_verifier(root)
                self.assertEqual(result.returncode, 0, result.stderr)
                gated = _run_verifier(root, "--require-release-eligible")
                if gate_allowed:
                    self.assertEqual(gated.returncode, 0, gated.stderr)
                else:
                    self.assert_invalid(gated)

    def test_partial_or_crossed_release_state_rows_are_rejected(self):
        invalid_rows = (
            ("candidate", "not_published", True, None),
            ("candidate", "published_prerelease", False, None),
            ("prerelease_ready", "not_published", False, None),
            ("prerelease_ready", "published_prerelease", True, None),
            ("prerelease_ready", "not_published", True, "receipt"),
            ("prerelease_published", "published_prerelease", True, "receipt"),
            ("prerelease_published", "not_published", False, "receipt"),
            ("prerelease_published", "published_prerelease", False, None),
        )
        for stage, publication, eligible, receipt_kind in invalid_rows:
            with self.subTest(
                stage=stage, publication=publication, eligible=eligible,
                receipt=receipt_kind,
            ), _repository_fixture() as root:
                receipt = self.receipt(root) if receipt_kind == "receipt" else None
                if receipt is not None:
                    subprocess.run(["git", "tag", "v0.8.7"], cwd=root, check=True)
                self.transition(
                    root,
                    stage=stage,
                    publication=publication,
                    eligible=eligible,
                    receipt=receipt,
                )
                self.assert_invalid(_run_verifier(root))

    def test_published_receipt_is_closed_and_cryptographically_shaped(self):
        def add_private_actor(receipt: dict[str, object]) -> None:
            receipt["actor"] = "private-maintainer"

        mutations: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
            ("unknown-field", add_private_actor),
            ("schema", lambda value: value.__setitem__("schemaVersion", "receipt/v1")),
            ("repository", lambda value: value.__setitem__("repository", "https://example.test/repo")),
            ("release-id", lambda value: value.__setitem__("releaseId", "01")),
            ("tag", lambda value: value.__setitem__("tag", "v0.8.5")),
            ("source-sha", lambda value: value.__setitem__("sourceSha", "c" * 40)),
            ("published-at", lambda value: value.__setitem__("publishedAt", "2026-08-09T20:00:00+08:00")),
            ("prerelease", lambda value: value.__setitem__("prerelease", False)),
            ("manifest-digest", lambda value: value.__setitem__("manifestSha256", "A" * 64)),
            ("asset-set-digest", lambda value: value.__setitem__("assetSetSha256", "short")),
            ("attestation", lambda value: value.__setitem__("provenanceAttestation", "not_verified")),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label), _repository_fixture() as root:
                receipt = self.receipt(root)
                subprocess.run(["git", "tag", "v0.8.7"], cwd=root, check=True)
                mutate(receipt)
                self.transition(
                    root,
                    stage="prerelease_published",
                    publication="published_prerelease",
                    eligible=False,
                    receipt=receipt,
                )
                self.assert_invalid(_run_verifier(root), "private-maintainer")

    def test_tag_and_local_artifact_cannot_impersonate_publication(self):
        with _repository_fixture() as root:
            subprocess.run(["git", "tag", "v0.8.7"], cwd=root, check=True)
            artifact = root / "dist/OpenUsage-Bar-v0.8.7-macos-arm64.dmg"
            artifact.parent.mkdir()
            artifact.write_bytes(b"local candidate bytes, not a publication receipt\n")

            result = _run_verifier(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("published=v0.7.1", result.stdout)
        self.assertNotIn("published=v0.8.7", result.stdout)

    def test_publication_receipt_never_enters_renderer_safe_projections(self):
        for relative in self.PROJECTION_PATHS:
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn("publicationReceipt", source)
                self.assertNotIn("github-release-receipt/v1", source)
                self.assertNotIn("releaseId", source)
                self.assertNotIn("sourceSha", source)


class ProductVersionTruthSourceBindingTests(ProductVersionTruthTestCase):
    def test_release_state_version_build_channel_and_canary_are_bound(self):
        def stale_version(payload: dict[str, object]) -> None:
            payload["currentVersion"] = "0.8.5"

        def stale_build(payload: dict[str, object]) -> None:
            payload["buildVersion"] = "27"

        def wrong_channel(payload: dict[str, object]) -> None:
            payload["channel"] = "stable"

        def forged_canary(payload: dict[str, object]) -> None:
            payload["canary"] = {
                "clock": "passed",
                "qualifiedMachines": 5,
                "targetMachines": 5,
            }

        self.assert_json_mutations_invalid(
            RELEASE_STATE,
            (
                ("version", stale_version),
                ("build", stale_build),
                ("channel", wrong_channel),
                ("canary", forged_canary),
            ),
        )

    def test_setup_package_and_lock_identity_are_bound(self):
        self.assert_text_mutations_invalid(
            (
                (
                    "setup-name",
                    Path("setup.py"),
                    '"name": "openusage-bar"',
                    '"name": "usagehub"',
                ),
                (
                    "setup-version",
                    Path("setup.py"),
                    '"version": "0.8.7"',
                    '"version": "0.8.5"',
                ),
            )
        )

        def stale_version(payload: dict[str, object]) -> None:
            payload["version"] = "0.8.5"

        def stale_root_lock(payload: dict[str, object]) -> None:
            payload["packages"][""]["version"] = "0.8.5"

        def wrong_name(payload: dict[str, object]) -> None:
            payload["name"] = "openusage-bar"

        for application in ("desktop", "web"):
            with self.subTest(application=application, file="package.json"):
                self.assert_json_mutations_invalid(
                    Path(application) / "package.json",
                    (
                        ("version", stale_version),
                        ("name", wrong_name),
                    ),
                )
            with self.subTest(application=application, file="package-lock.json"):
                self.assert_json_mutations_invalid(
                    Path(application) / "package-lock.json",
                    (
                        ("top-level-version", stale_version),
                        ("root-package-version", stale_root_lock),
                    ),
                )

    def test_desktop_product_artifact_and_bundle_identifier_are_bound(self):
        def wrong_product(payload: dict[str, object]) -> None:
            payload["build"]["productName"] = "OpenUsage Bar"

        def wrong_artifact(payload: dict[str, object]) -> None:
            payload["build"]["artifactName"] = (
                "OpenUsage-Bar-v${version}-macos-${arch}.${ext}"
            )

        def wrong_app_id(payload: dict[str, object]) -> None:
            payload["build"]["appId"] = "com.lune.usagehub"

        self.assert_json_mutations_invalid(
            Path("desktop/package.json"),
            (
                ("product-name", wrong_product),
                ("artifact-template", wrong_artifact),
                ("app-id", wrong_app_id),
            ),
        )

    def test_client_build_identity_projections_are_bound(self):
        self.assert_text_mutations_invalid(
            (
                (
                    "desktop-projection",
                    Path("desktop/product_version_truth.js"),
                    'candidateVersion: "0.8.7"',
                    'candidateVersion: "0.8.5"',
                ),
                (
                    "web-projection",
                    Path("web/src/productVersionTruth.ts"),
                    'publicationStatus: "not_published"',
                    'publicationStatus: "published"',
                ),
                (
                    "swift-projection",
                    Path("swift_app/Sources/UsageCore/ProductVersionTruth.swift"),
                    'publishedBaselineTag: "v0.7.1"',
                    'publishedBaselineTag: "v0.8.7"',
                ),
            )
        )

    def test_every_swift_plist_binds_brand_version_and_build(self):
        plist_paths = (
            Path("swift_app/Resources/OpenUsageActivity-Info.plist"),
            Path("swift_app/Resources/OpenUsageBar-Info.plist"),
            Path("swift_app/Resources/OpenUsageProviderSettings-Info.plist"),
        )
        with _repository_fixture() as root:
            for relative in plist_paths:
                path = root / relative
                original = path.read_bytes()
                for field, value in (
                    ("CFBundleDisplayName", "OpenUsage Bar"),
                    ("CFBundleShortVersionString", "0.8.5"),
                    ("CFBundleVersion", "27"),
                ):
                    with self.subTest(plist=relative.name, field=field):
                        payload = plistlib.loads(original)
                        payload[field] = value
                        path.write_bytes(plistlib.dumps(payload, sort_keys=True))
                        try:
                            self.assert_invalid(_run_verifier(root))
                        finally:
                            path.write_bytes(original)

    def test_gateway_and_runtime_schema_and_code_constants_are_bound(self):
        def gateway_schema_drift(payload: dict[str, object]) -> None:
            payload["apiVersion"] = "runtime-capability.openusage/v1"

        def runtime_schema_drift(payload: dict[str, object]) -> None:
            payload["properties"]["apiVersion"]["const"] = "gateway.openusage/v1"

        self.assert_json_mutations_invalid(
            Path("openusage_bar/resources/gateway-api-v1.schema.json"),
            (("gateway-schema", gateway_schema_drift),),
        )
        self.assert_json_mutations_invalid(
            Path("openusage_bar/resources/runtime-capability-v1.schema.json"),
            (("runtime-schema", runtime_schema_drift),),
        )
        self.assert_text_mutations_invalid(
            (
                (
                    "runtime-python",
                    Path("openusage_bar/runtime_capabilities.py"),
                    'API_VERSION = "runtime-capability.openusage/v1"',
                    'API_VERSION = "gateway.openusage/v1"',
                ),
                (
                    "gateway-runtime",
                    Path("openusage_bar/gateway/runtime.py"),
                    '_API_VERSION = "gateway.openusage/v1"',
                    '_API_VERSION = "runtime-capability.openusage/v1"',
                ),
                (
                    "gateway-response",
                    Path("openusage_bar/gateway/response.py"),
                    'API_VERSION = "gateway.openusage/v1"',
                    'API_VERSION = "runtime-capability.openusage/v1"',
                ),
                (
                    "desktop-runtime",
                    Path("desktop/runtime_capability.js"),
                    'API_VERSION = "runtime-capability.openusage/v1"',
                    'API_VERSION = "gateway.openusage/v1"',
                ),
                (
                    "web-runtime",
                    Path("web/src/runtimeCapability.ts"),
                    '"runtime-capability.openusage/v1"',
                    '"gateway.openusage/v1"',
                ),
            )
        )

    def test_readme_and_roadmap_identity_markers_are_bound(self):
        self.assert_text_mutations_invalid(
            tuple(
                (
                    relative.name,
                    relative,
                    "candidate=0.8.7 build=29 channel=rc stage=candidate "
                    "publication=not_published published=v0.7.1",
                    "candidate=0.8.7 build=29 channel=rc stage=published "
                    "publication=published published=v0.8.7",
                )
                for relative in (
                    Path("README.md"),
                    Path("README.en.md"),
                    Path("ROADMAP.md"),
                )
            )
        )

    def test_key_ui_brand_surfaces_are_bound_to_usagehub(self):
        self.assert_text_mutations_invalid(
            (
                (
                    "web-app-heading",
                    Path("web/src/App.tsx"),
                    "<h1>UsageHub</h1>",
                    "<h1>OpenUsage Bar</h1>",
                ),
                (
                    "web-document-title",
                    Path("web/index.html"),
                    "<title>UsageHub</title>",
                    "<title>OpenUsage Bar</title>",
                ),
                (
                    "web-tray",
                    Path("web/src/components/TrayMenu.tsx"),
                    '<div className="tray-header">UsageHub</div>',
                    '<div className="tray-header">OpenUsage Bar</div>',
                ),
                (
                    "web-menubar",
                    Path("web/src/components/MenuBarPopover.tsx"),
                    '<div className="menubar-title">UsageHub</div>',
                    '<div className="menubar-title">OpenUsage Bar</div>',
                ),
                (
                    "desktop-window",
                    Path("desktop/main.js"),
                    'title: "UsageHub"',
                    'title: "OpenUsage Bar"',
                ),
                (
                    "swift-menu",
                    Path("swift_app/Sources/OpenUsageBar/MenuLogic.swift"),
                    'AppLocalization.text("UsageHub")',
                    'AppLocalization.text("OpenUsage Bar")',
                ),
                (
                    "swift-activity",
                    Path("swift_app/Sources/OpenUsageActivity/ActivityViews.swift"),
                    'AppLocalization.text("UsageHub")',
                    'AppLocalization.text("OpenUsage Bar")',
                ),
                (
                    "python-dashboard",
                    Path("openusage_bar/web_dashboard.py"),
                    "<title>UsageHub Dashboard</title>",
                    "<title>OpenUsage Bar Dashboard</title>",
                ),
                (
                    "python-repair-window",
                    Path("openusage_bar/ui.py"),
                    '"settings.window_title": "UsageHub Advanced and Repair"',
                    '"settings.window_title": "OpenUsage Bar Advanced and Repair"',
                ),
            )
        )

    def test_legacy_release_and_cross_platform_handoff_policies_do_not_merge(self):
        self.assert_text_mutations_invalid(
            (
                (
                    "legacy-release-audit",
                    Path("scripts/release_artifact_audit.py"),
                    "OpenUsage-Bar-v",
                    "UsageHub-",
                ),
                (
                    "legacy-canary",
                    Path("scripts/verify_canary_candidate.py"),
                    "OpenUsage-Bar-v",
                    "UsageHub-",
                ),
                (
                    "cross-platform-posture",
                    Path("scripts/distribution_trust_posture.py"),
                    "UsageHub-",
                    "OpenUsage-Bar-v",
                ),
                (
                    "local-handoff-product",
                    Path("scripts/release_handoff.py"),
                    '"UsageHub"',
                    '"OpenUsage Bar"',
                ),
            )
        )
        with _repository_fixture() as root:
            native_evidence = root / "scripts/native_ci_evidence.py"
            source = native_evidence.read_text(encoding="utf-8")
            self.assertEqual(source.count('"UsageHub"'), 2)
            native_evidence.write_text(
                source.replace('"UsageHub"', '"OpenUsage Bar"'),
                encoding="utf-8",
            )
            with self.subTest(mutation="native-evidence-product"):
                self.assert_invalid(_run_verifier(root))


class ProductVersionTruthPrivacyTests(ProductVersionTruthTestCase):
    def test_invalid_private_root_is_not_echoed(self):
        with tempfile.TemporaryDirectory(
            prefix="product-version-private-marker-"
        ) as directory:
            private_root = Path(directory) / "missing-private-repository"
            result = _run_verifier(private_root)

        self.assert_invalid(result, str(private_root), "private-marker")

    def test_invalid_private_cli_argument_is_not_echoed(self):
        private_value = "/private/customer/version-secret"
        result = _run_verifier(
            ROOT,
            "--private-release-token",
            private_value,
        )

        self.assert_invalid(result, private_value, "private-release-token")


if __name__ == "__main__":
    unittest.main()
