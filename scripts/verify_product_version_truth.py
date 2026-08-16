#!/usr/bin/env python3
"""Verify product identity without inferring that a candidate was published."""

from __future__ import annotations

import argparse
import ast
import json
import plistlib
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


CONTRACT_PATH = Path("openusage_bar/resources/product-version-truth.v1.json")
RELEASE_STATE_PATH = Path("openusage_bar/resources/release-state.v1.json")
PLISTS = {
    Path("swift_app/Resources/OpenUsageBar-Info.plist"): (
        "UsageHub",
        "UsageHub",
        "OpenUsage Bar",
        "com.lune.openusagebar",
    ),
    Path("swift_app/Resources/OpenUsageActivity-Info.plist"): (
        "UsageHub Activity",
        "UsageHub Activity",
        "OpenUsage Activity",
        "com.lune.openusagebar.activity",
    ),
    Path("swift_app/Resources/OpenUsageProviderSettings-Info.plist"): (
        "UsageHub Provider Settings",
        "UsageHub Provider Settings",
        "OpenUsage Provider Settings",
        "com.lune.openusagebar.settings",
    ),
}
ROOT_FIELDS = {
    "candidate",
    "interfaces",
    "product",
    "publishedBaseline",
    "releaseTracks",
    "schemaVersion",
}
PRODUCT_FIELDS = {
    "bundleIdentifier",
    "cliName",
    "displayName",
    "legacyDisplayName",
    "pythonDistribution",
    "socketName",
    "technicalNamespace",
}
CANDIDATE_FIELDS = {
    "build",
    "canary",
    "channel",
    "publicationReceipt",
    "publicationStatus",
    "releaseEligible",
    "releaseStage",
    "version",
}
CANARY_FIELDS = {"clock", "qualifiedMachines", "targetMachines"}
PUBLISHED_FIELDS = {"publishedAt", "tag", "version"}
INTERFACE_FIELDS = {"gateway", "localApi", "runtimeCapability"}
TRACK_FIELDS = {"crossPlatformDesktop", "legacyMacNative"}
LEGACY_TRACK_FIELDS = {"appBundleName", "assetTemplate", "distributionRole"}
CROSS_TRACK_FIELDS = {
    "distributionRole",
    "electronArtifactTemplate",
    "evidenceArtifactTemplate",
    "macAppBundleName",
    "productName",
}
RELEASE_STATE_FIELDS = {
    "apiVersion",
    "buildVersion",
    "canary",
    "channel",
    "currentVersion",
    "schemaVersion",
}
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BUILD = re.compile(r"^[1-9]\d*$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID = re.compile(r"^[1-9]\d*$")
RECEIPT_FIELDS = {
    "assetSetSha256",
    "manifestSha256",
    "prerelease",
    "provenanceAttestation",
    "publishedAt",
    "releaseId",
    "repository",
    "schemaVersion",
    "sourceSha",
    "tag",
}
BUILD_IDENTITY_MARKER = re.compile(
    r"(?m)^<!-- openusage-build-identity: product=([^ ]+) "
    r"candidate=([^ ]+) build=([^ ]+) channel=([^ ]+) "
    r"stage=([^ ]+) publication=([^ ]+) published=([^ ]+) -->$"
)
EXPECTED_PRODUCT = {
    "bundleIdentifier": "com.lune.openusagebar",
    "cliName": "openusage-bar",
    "displayName": "UsageHub",
    "legacyDisplayName": "OpenUsage Bar",
    "pythonDistribution": "openusage-bar",
    "socketName": "openusage.sock",
    "technicalNamespace": "openusage-bar",
}
EXPECTED_LEGACY_TRACK = {
    "appBundleName": "OpenUsage Bar.app",
    "assetTemplate": "OpenUsage-Bar-v{version}-macos-arm64.{ext}",
    "distributionRole": "legacy_macos_release_track",
}
EXPECTED_CROSS_TRACK = {
    "distributionRole": "local_ci_handoff_only",
    "electronArtifactTemplate": "${productName}-${version}-${os}-${arch}.${ext}",
    "evidenceArtifactTemplate": "UsageHub-{version}-{platform}-{artifactArch}.{ext}",
    "macAppBundleName": "UsageHub.app",
    "productName": "UsageHub",
}


class ProductVersionTruthError(ValueError):
    """One intentionally opaque product/version contract failure."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ProductVersionTruthError()


def _fail() -> None:
    raise ProductVersionTruthError()


def _exact_object(value: Any, fields: Iterable[str]) -> dict[str, Any]:
    expected = set(fields)
    if type(value) is not dict or set(value) != expected:
        _fail()
    return value


def _read_json(root: Path, relative: Path) -> dict[str, Any]:
    try:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            _fail()
        value = json.loads(path.read_text(encoding="utf-8"))
    except ProductVersionTruthError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail()
    if type(value) is not dict:
        _fail()
    return value


def _read_text(root: Path, relative: Path, limit: int = 4 * 1024 * 1024) -> str:
    try:
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            _fail()
        return path.read_text(encoding="utf-8")
    except ProductVersionTruthError:
        raise
    except (OSError, UnicodeError):
        _fail()


def _version_tuple(value: Any) -> tuple[int, int, int]:
    if type(value) is not str or (match := VERSION.fullmatch(value)) is None:
        _fail()
    return tuple(int(part) for part in match.groups())


def _validate_publication_receipt(
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    receipt = candidate["publicationReceipt"]
    state = (
        candidate["releaseStage"],
        candidate["publicationStatus"],
        candidate["releaseEligible"],
        receipt is None,
    )
    if state == ("candidate", "not_published", False, True):
        return None
    if state == ("prerelease_ready", "not_published", True, True):
        return None
    if state != (
        "prerelease_published",
        "published_prerelease",
        False,
        False,
    ):
        _fail()

    value = _exact_object(receipt, RECEIPT_FIELDS)
    if (
        value["schemaVersion"] != "github-release-receipt/v1"
        or value["repository"] != "tttboy123/openusage-bar"
        or type(value["releaseId"]) is not str
        or RELEASE_ID.fullmatch(value["releaseId"]) is None
        or value["tag"] != f"v{candidate['version']}"
        or type(value["sourceSha"]) is not str
        or COMMIT_SHA.fullmatch(value["sourceSha"]) is None
        or type(value["manifestSha256"]) is not str
        or SHA256.fullmatch(value["manifestSha256"]) is None
        or type(value["assetSetSha256"]) is not str
        or SHA256.fullmatch(value["assetSetSha256"]) is None
        or type(value["prerelease"]) is not bool
        or value["prerelease"] is not True
        or value["provenanceAttestation"] != "verified"
        or type(value["publishedAt"]) is not str
    ):
        _fail()
    try:
        datetime.strptime(value["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail()
    return value


def _validate_contract(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_object(payload, ROOT_FIELDS)
    if payload["schemaVersion"] != "product-version-truth/v1":
        _fail()

    product = _exact_object(payload["product"], PRODUCT_FIELDS)
    if product != EXPECTED_PRODUCT:
        _fail()

    candidate = _exact_object(payload["candidate"], CANDIDATE_FIELDS)
    candidate_version = _version_tuple(candidate["version"])
    if (
        type(candidate["build"]) is not str
        or BUILD.fullmatch(candidate["build"]) is None
        or candidate["channel"] not in {"alpha", "beta", "rc"}
        or type(candidate["releaseEligible"]) is not bool
    ):
        _fail()
    _validate_publication_receipt(candidate)
    canary = _exact_object(candidate["canary"], CANARY_FIELDS)
    qualified = canary["qualifiedMachines"]
    target = canary["targetMachines"]
    if (
        type(qualified) is not int
        or type(target) is not int
        or qualified < 0
        or target <= 0
        or qualified > target
        or canary["clock"] not in {"not_started", "running", "passed", "blocked"}
        or (qualified < target and canary["clock"] in {"running", "passed"})
    ):
        _fail()

    published = _exact_object(payload["publishedBaseline"], PUBLISHED_FIELDS)
    published_version = _version_tuple(published["version"])
    if published["tag"] != f"v{published['version']}":
        _fail()
    try:
        date.fromisoformat(published["publishedAt"])
    except (TypeError, ValueError):
        _fail()
    if published_version >= candidate_version:
        _fail()

    interfaces = _exact_object(payload["interfaces"], INTERFACE_FIELDS)
    if (
        type(interfaces["localApi"]) is not str
        or re.fullmatch(r"^[1-9]\d*\.\d+$", interfaces["localApi"]) is None
        or interfaces["gateway"] != "gateway.openusage/v1"
        or interfaces["runtimeCapability"] != "runtime-capability.openusage/v1"
    ):
        _fail()

    tracks = _exact_object(payload["releaseTracks"], TRACK_FIELDS)
    legacy = _exact_object(tracks["legacyMacNative"], LEGACY_TRACK_FIELDS)
    cross = _exact_object(tracks["crossPlatformDesktop"], CROSS_TRACK_FIELDS)
    if legacy != EXPECTED_LEGACY_TRACK or cross != EXPECTED_CROSS_TRACK:
        _fail()
    return payload


def _literal_assignments(source: str, names: set[str]) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _fail()
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id in names:
            try:
                value = ast.literal_eval(statement.value)
            except (TypeError, ValueError):
                _fail()
            if type(value) is not str or target.id in values:
                _fail()
            values[target.id] = value
    if set(values) != names:
        _fail()
    return values


def _setup_metadata(root: Path) -> tuple[str, str]:
    source = _read_text(root, Path("setup.py"))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _fail()
    matches: list[dict[str, Any]] = []
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "common"
            and isinstance(statement.value, ast.Dict)
        ):
            try:
                value = ast.literal_eval(statement.value)
            except (TypeError, ValueError):
                _fail()
            if type(value) is dict:
                matches.append(value)
    if len(matches) != 1:
        _fail()
    name = matches[0].get("name")
    version = matches[0].get("version")
    if type(name) is not str or type(version) is not str:
        _fail()
    return name, version


def _verify_release_state(root: Path, truth: dict[str, Any]) -> None:
    state = _read_json(root, RELEASE_STATE_PATH)
    _exact_object(state, RELEASE_STATE_FIELDS)
    candidate = truth["candidate"]
    if (
        state["schemaVersion"] != 1
        or state["currentVersion"] != candidate["version"]
        or state["buildVersion"] != candidate["build"]
        or state["channel"] != candidate["channel"]
        or state["apiVersion"] != truth["interfaces"]["localApi"]
        or state["canary"] != candidate["canary"]
    ):
        _fail()


def _verify_package_metadata(root: Path, truth: dict[str, Any]) -> None:
    version = truth["candidate"]["version"]
    for application in ("web", "desktop"):
        package = _read_json(root, Path(application) / "package.json")
        lock = _read_json(root, Path(application) / "package-lock.json")
        expected_name = f"usagehub-{application}"
        packages = lock.get("packages")
        root_package = packages.get("") if type(packages) is dict else None
        if (
            package.get("name") != expected_name
            or package.get("version") != version
            or lock.get("name") != expected_name
            or lock.get("version") != version
            or type(root_package) is not dict
            or root_package.get("name") != expected_name
            or root_package.get("version") != version
        ):
            _fail()

    desktop = _read_json(root, Path("desktop/package.json"))
    build = desktop.get("build")
    cross = truth["releaseTracks"]["crossPlatformDesktop"]
    if (
        type(build) is not dict
        or build.get("appId") != truth["product"]["bundleIdentifier"]
        or build.get("productName") != cross["productName"]
        or build.get("artifactName") != cross["electronArtifactTemplate"]
    ):
        _fail()


def _verify_python_and_plists(root: Path, truth: dict[str, Any]) -> None:
    candidate = truth["candidate"]
    product = truth["product"]
    package_name, package_version = _setup_metadata(root)
    if package_name != product["pythonDistribution"] or package_version != candidate["version"]:
        _fail()
    bundle = _literal_assignments(
        _read_text(root, Path("openusage_bar/bundle_config.py")),
        {"APP_VERSION", "BUILD_VERSION"},
    )
    if bundle != {"APP_VERSION": candidate["version"], "BUILD_VERSION": candidate["build"]}:
        _fail()

    for relative, expected_identity in PLISTS.items():
        try:
            payload = plistlib.loads((root / relative).read_bytes())
        except (OSError, plistlib.InvalidFileException):
            _fail()
        display, name, executable, identifier = expected_identity
        if (
            type(payload) is not dict
            or payload.get("CFBundleDisplayName") != display
            or payload.get("CFBundleName") != name
            or payload.get("CFBundleExecutable") != executable
            or payload.get("CFBundleIdentifier") != identifier
            or payload.get("CFBundleShortVersionString") != candidate["version"]
            or payload.get("CFBundleVersion") != candidate["build"]
        ):
            _fail()


def _verify_interface_versions(root: Path, truth: dict[str, Any]) -> None:
    interfaces = truth["interfaces"]
    gateway_schema = _read_json(root, Path("openusage_bar/resources/gateway-api-v1.schema.json"))
    runtime_schema = _read_json(
        root, Path("openusage_bar/resources/runtime-capability-v1.schema.json")
    )
    runtime_properties = runtime_schema.get("properties")
    api_property = runtime_properties.get("apiVersion") if type(runtime_properties) is dict else None
    if (
        gateway_schema.get("apiVersion") != interfaces["gateway"]
        or type(api_property) is not dict
        or api_property.get("const") != interfaces["runtimeCapability"]
    ):
        _fail()

    runtime_python = _literal_assignments(
        _read_text(root, Path("openusage_bar/runtime_capabilities.py")), {"API_VERSION"}
    )
    gateway_runtime = _literal_assignments(
        _read_text(root, Path("openusage_bar/gateway/runtime.py")), {"_API_VERSION"}
    )
    gateway_response = _literal_assignments(
        _read_text(root, Path("openusage_bar/gateway/response.py")), {"API_VERSION"}
    )
    if (
        runtime_python["API_VERSION"] != interfaces["runtimeCapability"]
        or gateway_runtime["_API_VERSION"] != interfaces["gateway"]
        or gateway_response["API_VERSION"] != interfaces["gateway"]
    ):
        _fail()
    desktop_source = _read_text(root, Path("desktop/runtime_capability.js"))
    web_source = _read_text(root, Path("web/src/runtimeCapability.ts"))
    gateway_api_source = _read_text(root, Path("openusage_bar/gateway/api.py"))
    expected = re.escape(interfaces["runtimeCapability"])
    if (
        re.search(rf'const API_VERSION = "{expected}";', desktop_source) is None
        or re.search(rf'RUNTIME_CAPABILITY_API_VERSION\s*=\s*\n?\s*"{expected}"', web_source) is None
        or f'"apiVersion": "{interfaces["gateway"]}"' not in gateway_api_source
    ):
        _fail()


def _verify_document_markers(root: Path, truth: dict[str, Any]) -> None:
    candidate = truth["candidate"]
    expected = (
        truth["product"]["displayName"],
        candidate["version"],
        candidate["build"],
        candidate["channel"],
        candidate["releaseStage"],
        candidate["publicationStatus"],
        f"v{truth['publishedBaseline']['version']}",
    )
    for relative in (Path("README.md"), Path("README.en.md"), Path("ROADMAP.md")):
        matches = BUILD_IDENTITY_MARKER.findall(_read_text(root, relative))
        if matches != [expected]:
            _fail()


def _verify_client_projections(root: Path, truth: dict[str, Any]) -> None:
    candidate = truth["candidate"]
    published = truth["publishedBaseline"]
    canary = candidate["canary"]
    projection = {
        "displayName": truth["product"]["displayName"],
        "legacyDisplayName": truth["product"]["legacyDisplayName"],
        "candidateVersion": candidate["version"],
        "candidateBuild": candidate["build"],
        "channel": candidate["channel"],
        "releaseStage": candidate["releaseStage"],
        "publicationStatus": candidate["publicationStatus"],
        "releaseEligible": candidate["releaseEligible"],
        "publishedBaselineVersion": published["version"],
        "publishedBaselineTag": published["tag"],
        "canaryQualifiedMachines": canary["qualifiedMachines"],
        "canaryTargetMachines": canary["targetMachines"],
        "canaryClock": canary["clock"],
    }
    for relative in (
        Path("desktop/product_version_truth.js"),
        Path("web/src/productVersionTruth.ts"),
        Path("swift_app/Sources/UsageCore/ProductVersionTruth.swift"),
    ):
        source = _read_text(root, relative)
        expected_lines = {
            f"{field}: {json.dumps(value, ensure_ascii=True)}"
            for field, value in projection.items()
        }
        if any(line not in source for line in expected_lines):
            _fail()

    desktop = _read_json(root, Path("desktop/package.json"))
    build = desktop.get("build")
    packaged_files = build.get("files") if type(build) is dict else None
    if type(packaged_files) is not list or "product_version_truth.js" not in packaged_files:
        _fail()


def _verify_ui_and_track_bindings(root: Path, truth: dict[str, Any]) -> None:
    product = truth["product"]["displayName"]
    ui_requirements = {
        Path("web/src/App.tsx"): (f"<h1>{product}</h1>",),
        Path("web/index.html"): (f"<title>{product}</title>",),
        Path("web/src/components/TrayMenu.tsx"): (
            f'<div className="tray-header">{product}</div>',
        ),
        Path("web/src/components/MenuBarPopover.tsx"): (
            f'<div className="menubar-title">{product}</div>',
        ),
        Path("desktop/main.js"): (
            f'title: "{product}"',
            f'tray.setToolTip("{product}")',
            f'label: "About {product}"',
        ),
        Path("swift_app/Sources/OpenUsageBar/MenuLogic.swift"): (
            f'values: values, accessibilityTitle: AppLocalization.text("{product}")',
        ),
        Path("swift_app/Sources/OpenUsageActivity/ActivityViews.swift"): (
            f'AppLocalization.text("{product}")',
        ),
        Path("openusage_bar/web_dashboard.py"): (
            f"<title>{product} Dashboard</title>",
        ),
        Path("openusage_bar/ui.py"): (
            f'"settings.window_title": "{product} Advanced and Repair"',
            f'"settings.window_title": "{product} 高级与修复"',
        ),
    }
    for relative, needles in ui_requirements.items():
        source = _read_text(root, relative)
        if any(needle not in source for needle in needles):
            _fail()

    policy_requirements = {
        Path("scripts/release_artifact_audit.py"): (
            're.compile(r"^OpenUsage-Bar-v(\\d+\\.\\d+\\.\\d+)-macos-arm64\\.zip$")',
        ),
        Path("scripts/verify_canary_candidate.py"): (
            'r"^OpenUsage-Bar-v((?:0|[1-9]\\d*)',
        ),
        Path("scripts/distribution_trust_posture.py"): (
            'r"UsageHub-[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-mac-(?:arm64|x64)\\.dmg"',
            'r"UsageHub-[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-win-(?:arm64|x64)\\.exe"',
            'r"UsageHub-[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-linux-(?:arm64|x64|x86_64)\\.AppImage"',
        ),
        Path("scripts/native_ci_evidence.py"): ('"UsageHub"', '"releaseEligible": False'),
        Path("scripts/release_handoff.py"): ('"UsageHub"', "releaseEligible"),
    }
    for relative, needles in policy_requirements.items():
        source = _read_text(root, relative)
        if any(needle not in source for needle in needles):
            _fail()


def _tag_commit(root: Path, tag: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail()
    commit = result.stdout.strip()
    if result.returncode != 0 or COMMIT_SHA.fullmatch(commit) is None:
        _fail()
    return commit


def _verify_release_tag_bindings(root: Path, truth: dict[str, Any]) -> None:
    _tag_commit(root, truth["publishedBaseline"]["tag"])
    receipt = truth["candidate"]["publicationReceipt"]
    if receipt is not None:
        if receipt["sourceSha"] != _tag_commit(root, receipt["tag"]):
            _fail()


def _verify_release_gate_binding(
    root: Path,
    truth: dict[str, Any],
    expected_tag: str | None,
    expected_source_sha: str | None,
) -> None:
    if (expected_tag is None) != (expected_source_sha is None):
        _fail()
    if expected_tag is None or expected_source_sha is None:
        return
    candidate_tag = f"v{truth['candidate']['version']}"
    if (
        expected_tag != candidate_tag
        or COMMIT_SHA.fullmatch(expected_source_sha) is None
        or _tag_commit(root, expected_tag) != expected_source_sha
    ):
        _fail()


def verify_product_version_truth(root: Path) -> dict[str, Any]:
    """Return the validated closed contract, or raise one opaque error."""
    try:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            _fail()
        truth = _validate_contract(_read_json(resolved, CONTRACT_PATH))
        _verify_release_state(resolved, truth)
        _verify_python_and_plists(resolved, truth)
        _verify_package_metadata(resolved, truth)
        _verify_interface_versions(resolved, truth)
        _verify_document_markers(resolved, truth)
        _verify_client_projections(resolved, truth)
        _verify_ui_and_track_bindings(resolved, truth)
        _verify_release_tag_bindings(resolved, truth)
        return truth
    except ProductVersionTruthError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError):
        _fail()


def main(arguments: list[str]) -> int:
    parser = SafeArgumentParser(add_help=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--require-release-eligible", action="store_true")
    parser.add_argument("--expected-tag")
    parser.add_argument("--expected-source-sha")
    try:
        parsed = parser.parse_args(arguments)
        truth = verify_product_version_truth(parsed.root)
        binding_requested = (
            parsed.expected_tag is not None
            or parsed.expected_source_sha is not None
        )
        if binding_requested and not parsed.require_release_eligible:
            _fail()
        if parsed.require_release_eligible:
            candidate = truth["candidate"]
            if (
                candidate["releaseStage"] != "prerelease_ready"
                or candidate["publicationStatus"] != "not_published"
                or candidate["releaseEligible"] is not True
                or candidate["publicationReceipt"] is not None
            ):
                _fail()
            _verify_release_gate_binding(
                parsed.root,
                truth,
                parsed.expected_tag,
                parsed.expected_source_sha,
            )
    except ProductVersionTruthError:
        print("product_version_truth_invalid", file=sys.stderr)
        return 1
    candidate = truth["candidate"]
    baseline = truth["publishedBaseline"]
    print(
        "product_version_truth_ok "
        f"product={truth['product']['displayName']} "
        f"candidate={candidate['version']} build={candidate['build']} "
        f"channel={candidate['channel']} published={baseline['tag']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
