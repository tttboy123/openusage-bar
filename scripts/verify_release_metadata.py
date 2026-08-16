#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path


PLISTS = (
    Path("swift_app/Resources/OpenUsageBar-Info.plist"),
    Path("swift_app/Resources/OpenUsageActivity-Info.plist"),
    Path("swift_app/Resources/OpenUsageProviderSettings-Info.plist"),
)
RELEASE_GUIDE = Path("docs/release-quick-start.md")
RELEASE_READMES = (Path("README.md"), Path("README.en.md"))
RELEASE_STATE = Path("openusage_bar/resources/release-state.v1.json")
ROADMAP = Path("ROADMAP.md")
SETUP_METADATA = Path("setup.py")
PACKAGE_METADATA_DIRECTORIES = (Path("web"), Path("desktop"))
CANARY_GUIDE = Path("docs/canary.md")
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DOCUMENTED_VERSION_PATTERN = re.compile(
    r"(?<![0-9.])v?((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?![0-9.])"
)
README_RELEASE_MARKER_PATTERN = re.compile(
    r"(?m)^<!-- openusage-release-version: "
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)) -->$"
)
README_DMG_PATTERN = re.compile(
    r"/releases/download/v"
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))/"
    r"OpenUsage-Bar-v"
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"-macos-arm64\.dmg"
)
CANARY_CANDIDATE_MARKER_PATTERN = re.compile(
    r"(?m)^[ \t]*The current candidate version is `"
    r"((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))`:[ \t]*$"
)
CANARY_CANDIDATE_OCCURRENCE_PATTERNS = (
    re.compile(r"OpenUsage-Bar-v([A-Za-z0-9.+_-]+?)-macos-arm64"),
    re.compile(r"--source-ref[ \t]+refs/tags/v([A-Za-z0-9.+_-]+)"),
    re.compile(
        r"verify_canary_candidate\.py[^\n]*?--version[ \t]+"
        r"([A-Za-z0-9.+_-]+)"
    ),
)
API_VERSION_PATTERN = re.compile(r"^[1-9]\d*\.\d+$")
ROADMAP_STATE_PATTERN = re.compile(
    r"(?m)^<!-- openusage-release-state: "
    r"version=([^ ]+) channel=([^ ]+) api=([^ ]+) "
    r"canary=(\d+)/(\d+) clock=([^ ]+) -->$"
)
RELEASE_STATE_FIELDS = {
    "schemaVersion", "currentVersion", "buildVersion", "channel",
    "apiVersion", "canary",
}
CANARY_FIELDS = {"qualifiedMachines", "targetMachines", "clock"}
RELEASE_CHANNELS = {"alpha", "beta", "rc", "stable"}
CANARY_CLOCKS = {"not_started", "running", "passed", "blocked"}


class MetadataError(ValueError):
    pass


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MetadataError("git_state")
    return result.stdout.strip()


def _bundle_config(root: Path) -> tuple[str, str]:
    tree = ast.parse((root / "openusage_bar/bundle_config.py").read_text("utf-8"))
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id in {"APP_VERSION", "BUILD_VERSION"}:
            value = ast.literal_eval(statement.value)
            if not isinstance(value, str):
                raise MetadataError("bundle_config")
            values[target.id] = value
    if set(values) != {"APP_VERSION", "BUILD_VERSION"}:
        raise MetadataError("bundle_config")
    return values["APP_VERSION"], values["BUILD_VERSION"]


def _plist_metadata(payload: bytes) -> tuple[str, str]:
    try:
        value = plistlib.loads(payload)
        version = value["CFBundleShortVersionString"]
        build = value["CFBundleVersion"]
    except (plistlib.InvalidFileException, KeyError, TypeError, ValueError) as error:
        raise MetadataError("plist") from error
    if not isinstance(version, str) or not isinstance(build, str):
        raise MetadataError("plist")
    return version, build


def _current_metadata(root: Path) -> tuple[str, str]:
    values = {_plist_metadata((root / path).read_bytes()) for path in PLISTS}
    values.add(_bundle_config(root))
    if len(values) != 1:
        raise MetadataError("version_mismatch")
    version, build = values.pop()
    if VERSION_PATTERN.fullmatch(version) is None:
        raise MetadataError("version_format")
    if not build.isascii() or not build.isdigit() or int(build) <= 0:
        raise MetadataError("build_format")
    return version, build


def _verify_changelog(root: Path, version: str) -> None:
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    headings = re.findall(
        r"(?m)^## ([0-9]+\.[0-9]+\.[0-9]+) - \d{4}-\d{2}-\d{2}$", changelog
    )
    if headings.count(version) != 1:
        raise MetadataError("changelog")


def _verify_release_guide(root: Path, version: str) -> None:
    guide = (root / RELEASE_GUIDE).read_text("utf-8")
    documented_versions = set(DOCUMENTED_VERSION_PATTERN.findall(guide))
    if documented_versions != {version}:
        raise MetadataError("release_guide")


def _verify_release_readmes(root: Path, version: str) -> None:
    for path in RELEASE_READMES:
        readme = (root / path).read_text("utf-8")
        if README_RELEASE_MARKER_PATTERN.findall(readme) != [version]:
            raise MetadataError("release_readme")
        downloads = README_DMG_PATTERN.findall(readme)
        if not downloads or any(
            directory_version != version or filename_version != version
            for directory_version, filename_version in downloads
        ):
            raise MetadataError("release_readme")


def _dict_version_node(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Dict):
        versions = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None or not isinstance(key, ast.Constant):
                raise MetadataError("setup_metadata")
            if key.value == "version":
                versions.append(value)
        if len(versions) > 1:
            raise MetadataError("setup_metadata")
        return versions[0] if versions else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and not node.args
    ):
        if any(keyword.arg is None for keyword in node.keywords):
            raise MetadataError("setup_metadata")
        versions = [
            keyword.value for keyword in node.keywords if keyword.arg == "version"
        ]
        if len(versions) > 1:
            raise MetadataError("setup_metadata")
        return versions[0] if versions else None
    raise MetadataError("setup_metadata")


def _darwin_condition(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_darwin_condition(value) for value in node.values)
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or not isinstance(node.ops[0], ast.Eq)
        or len(node.comparators) != 1
    ):
        return False
    operands = (node.left, node.comparators[0])
    platform = next(
        (
            operand
            for operand in operands
            if isinstance(operand, ast.Attribute)
            and isinstance(operand.value, ast.Name)
            and operand.value.id == "sys"
            and operand.attr == "platform"
        ),
        None,
    )
    darwin = next(
        (
            operand
            for operand in operands
            if isinstance(operand, ast.Constant) and operand.value == "darwin"
        ),
        None,
    )
    return platform is not None and darwin is not None


def _common_update_call(statement: ast.stmt) -> ast.Call | None:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "common"
        and call.func.attr == "update"
    ):
        return call
    return None


def _update_version_node(call: ast.Call) -> ast.AST | None:
    if len(call.args) > 1 or any(keyword.arg is None for keyword in call.keywords):
        raise MetadataError("setup_metadata")
    versions = [keyword.value for keyword in call.keywords if keyword.arg == "version"]
    if call.args:
        positional_version = _dict_version_node(call.args[0])
        if positional_version is not None:
            versions.append(positional_version)
    if len(versions) > 1:
        raise MetadataError("setup_metadata")
    return versions[0] if versions else None


def _setup_version_value(
    node: ast.AST, imported_app_versions: set[str], current_version: str
) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in imported_app_versions:
        return current_version
    raise MetadataError("setup_metadata")


def _verify_setup_metadata(root: Path, version: str) -> None:
    tree = ast.parse((root / SETUP_METADATA).read_text("utf-8"))
    setup_imports = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module == "setuptools"
        and any(
            alias.name == "setup" and (alias.asname is None or alias.asname == "setup")
            for alias in statement.names
        )
    ]
    assignments = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "common"
    ]
    darwin_branches = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.If) and _darwin_condition(statement.test)
    ]
    setup_calls = [
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "setup"
    ]
    if (
        len(setup_imports) != 1
        or len(assignments) != 1
        or len(darwin_branches) != 1
        or len(setup_calls) != 1
    ):
        raise MetadataError("setup_metadata")

    setup_call = setup_calls[0]
    setup_statement = next(
        statement
        for statement in tree.body
        if isinstance(statement, ast.Expr) and statement.value is setup_call
    )
    if not (
        tree.body.index(setup_imports[0])
        < tree.body.index(assignments[0])
        < tree.body.index(darwin_branches[0])
        < tree.body.index(setup_statement)
    ):
        raise MetadataError("setup_metadata")
    if (
        setup_call.args
        or len(setup_call.keywords) != 1
        or setup_call.keywords[0].arg is not None
        or not isinstance(setup_call.keywords[0].value, ast.Name)
        or setup_call.keywords[0].value.id != "common"
    ):
        raise MetadataError("setup_metadata")

    common_assignment = assignments[0]
    base_version_node = _dict_version_node(common_assignment.value)
    if base_version_node is None:
        raise MetadataError("setup_metadata")

    darwin_branch = darwin_branches[0]
    if darwin_branch.orelse:
        raise MetadataError("setup_metadata")
    update_calls = [
        call
        for statement in darwin_branch.body
        if (call := _common_update_call(statement)) is not None
    ]
    all_update_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "common"
        and node.func.attr == "update"
    ]
    if len(update_calls) > 1 or all_update_calls != update_calls:
        raise MetadataError("setup_metadata")

    allowed_common_names = {
        common_assignment.targets[0],
        setup_call.keywords[0].value,
        *(call.func.value for call in update_calls),
    }
    if any(
        node not in allowed_common_names
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "common"
    ):
        raise MetadataError("setup_metadata")
    if any(
        node is not setup_call.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "setup"
    ):
        raise MetadataError("setup_metadata")

    import_scope: list[ast.stmt] = []
    if update_calls:
        update_statement = next(
            statement
            for statement in darwin_branch.body
            if _common_update_call(statement) is update_calls[0]
        )
        import_scope = darwin_branch.body[:darwin_branch.body.index(update_statement)]
    imported_app_versions = {
        alias.asname or alias.name
        for statement in import_scope
        if isinstance(statement, ast.ImportFrom)
        and statement.module == "openusage_bar.bundle_config"
        for alias in statement.names
        if alias.name == "APP_VERSION"
    }
    if any(
        isinstance(node, ast.Name)
        and node.id in imported_app_versions
        and not isinstance(node.ctx, ast.Load)
        for node in ast.walk(darwin_branch)
    ):
        raise MetadataError("setup_metadata")
    base_version = _setup_version_value(base_version_node, set(), version)
    darwin_version = base_version
    if update_calls:
        override = _update_version_node(update_calls[0])
        if override is not None:
            darwin_version = _setup_version_value(
                override, imported_app_versions, version
            )
    if base_version != version or darwin_version != version:
        raise MetadataError("setup_metadata")


def _json_object(root: Path, path: Path) -> dict[str, object]:
    try:
        value = json.loads((root / path).read_text("utf-8"))
    except json.JSONDecodeError as error:
        raise MetadataError("package_metadata") from error
    if not isinstance(value, dict):
        raise MetadataError("package_metadata")
    return value


def _verify_package_metadata(root: Path, version: str) -> None:
    for directory in PACKAGE_METADATA_DIRECTORIES:
        package = _json_object(root, directory / "package.json")
        package_lock = _json_object(root, directory / "package-lock.json")
        packages = package_lock.get("packages")
        root_package = packages.get("") if isinstance(packages, dict) else None
        if not isinstance(root_package, dict):
            raise MetadataError("package_metadata")
        versions = (
            package.get("version"),
            package_lock.get("version"),
            root_package.get("version"),
        )
        if any(
            not isinstance(candidate, str) or candidate != version
            for candidate in versions
        ):
            raise MetadataError("package_metadata")


def _candidate_stanza(source: str, marker: re.Match[str]) -> str:
    lines = source[marker.end():].splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise MetadataError("canary_candidate")
    if lines[0].strip().startswith("```"):
        closing = next(
            (
                index
                for index, line in enumerate(lines[1:], start=1)
                if line.strip().startswith("```")
            ),
            None,
        )
        if closing is None:
            raise MetadataError("canary_candidate")
        return "\n".join(lines[1:closing])
    closing = next(
        (index for index, line in enumerate(lines) if not line.strip()), len(lines)
    )
    return "\n".join(lines[:closing])


def _verify_canary_candidate(root: Path, version: str) -> None:
    source = (root / CANARY_GUIDE).read_text("utf-8")
    markers = list(CANARY_CANDIDATE_MARKER_PATTERN.finditer(source))
    if len(markers) != 1 or markers[0].group(1) != version:
        raise MetadataError("canary_candidate")
    stanza = _candidate_stanza(source, markers[0])
    documented_versions = DOCUMENTED_VERSION_PATTERN.findall(stanza)
    if not documented_versions or any(
        candidate != version for candidate in documented_versions
    ):
        raise MetadataError("canary_candidate")
    for pattern in CANARY_CANDIDATE_OCCURRENCE_PATTERNS:
        candidates = pattern.findall(stanza)
        if not candidates or any(candidate != version for candidate in candidates):
            raise MetadataError("canary_candidate")


def _release_state(root: Path, version: str, build: str) -> dict[str, object]:
    try:
        state = json.loads((root / RELEASE_STATE).read_text("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        raise MetadataError("release_state") from error
    if not isinstance(state, dict) or set(state) != RELEASE_STATE_FIELDS:
        raise MetadataError("release_state")
    if type(state["schemaVersion"]) is not int or state["schemaVersion"] != 1:
        raise MetadataError("release_state")
    if state["currentVersion"] != version or state["buildVersion"] != build:
        raise MetadataError("release_state")
    channel = state["channel"]
    api_version = state["apiVersion"]
    if not isinstance(channel, str) or channel not in RELEASE_CHANNELS:
        raise MetadataError("release_state")
    if (
        not isinstance(api_version, str)
        or API_VERSION_PATTERN.fullmatch(api_version) is None
    ):
        raise MetadataError("release_state")
    canary = state["canary"]
    if not isinstance(canary, dict) or set(canary) != CANARY_FIELDS:
        raise MetadataError("release_state")
    qualified = canary["qualifiedMachines"]
    target = canary["targetMachines"]
    clock = canary["clock"]
    if (
        type(qualified) is not int
        or type(target) is not int
        or qualified < 0
        or target <= 0
        or qualified > target
        or not isinstance(clock, str)
        or clock not in CANARY_CLOCKS
    ):
        raise MetadataError("release_state")
    if qualified < target and clock in {"running", "passed"}:
        raise MetadataError("release_state")
    return state


def _verify_roadmap_state(root: Path, state: dict[str, object]) -> None:
    roadmap = (root / ROADMAP).read_text("utf-8")
    matches = ROADMAP_STATE_PATTERN.findall(roadmap)
    canary = state["canary"]
    if not isinstance(canary, dict):
        raise MetadataError("roadmap_state")
    expected = (
        str(state["currentVersion"]),
        str(state["channel"]),
        str(state["apiVersion"]),
        str(canary["qualifiedMachines"]),
        str(canary["targetMachines"]),
        str(canary["clock"]),
    )
    if matches != [expected]:
        raise MetadataError("roadmap_state")


def _verify_build_history(root: Path, version: str, build: str) -> None:
    current_build = int(build)
    for tag in _git(root, "tag", "--list", "v*").splitlines():
        if TAG_PATTERN.fullmatch(tag) is None or tag == f"v{version}":
            continue
        result = subprocess.run(
            ["git", "show", f"{tag}:{PLISTS[0].as_posix()}"],
            cwd=root, capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise MetadataError("tag_metadata")
        old_version, old_build = _plist_metadata(result.stdout)
        if old_version != tag[1:]:
            raise MetadataError("tag_metadata")
        if not old_build.isdigit() or int(old_build) >= current_build:
            raise MetadataError("build_reused")


def _main_ref(root: Path) -> str:
    for ref in ("refs/remotes/origin/main", "refs/heads/main"):
        if subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref], cwd=root, check=False
        ).returncode == 0:
            return ref
    raise MetadataError("main_ref")


def _verify_tag(root: Path, version: str, tag: str, expected_commit: str | None) -> None:
    if TAG_PATTERN.fullmatch(tag) is None or tag != f"v{version}":
        raise MetadataError("tag_version")
    commit = _git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if SHA_PATTERN.fullmatch(commit) is None:
        raise MetadataError("tag_commit")
    if expected_commit is not None and (
        SHA_PATTERN.fullmatch(expected_commit) is None or expected_commit != commit
    ):
        raise MetadataError("tag_moved")
    main_ref = _main_ref(root)
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, main_ref],
        cwd=root, check=False,
    ).returncode != 0:
        raise MetadataError("tag_not_on_main")
    head = _git(root, "rev-parse", "HEAD")
    if head == commit:
        return
    changed = _git(root, "diff", "--name-only", f"{commit}..{head}").splitlines()
    if not changed or any(
        not (path.startswith("docs/") or path.endswith(".md")) for path in changed
    ):
        raise MetadataError("post_tag_code_change")


def verify(root: Path, tag: str | None, expected_commit: str | None) -> tuple[str, str]:
    root = root.resolve()
    version, build = _current_metadata(root)
    _verify_changelog(root, version)
    _verify_release_guide(root, version)
    _verify_release_readmes(root, version)
    state = _release_state(root, version, build)
    _verify_setup_metadata(root, version)
    _verify_package_metadata(root, version)
    _verify_canary_candidate(root, version)
    _verify_roadmap_state(root, state)
    _verify_build_history(root, version, build)
    if tag:
        _verify_tag(root, version, tag, expected_commit)
    return version, build


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag")
    parser.add_argument("--expected-commit")
    parsed = parser.parse_args(arguments)
    environment_tag = os.environ.get("GITHUB_REF_NAME", "")
    tag = parsed.tag or (environment_tag if environment_tag.startswith("v") else None)
    expected = parsed.expected_commit or (os.environ.get("GITHUB_SHA") if tag else None)
    try:
        version, build = verify(parsed.root, tag, expected)
    except (MetadataError, OSError, UnicodeError, SyntaxError):
        print("release_metadata_invalid", file=sys.stderr)
        return 1
    print(f"release_metadata_ok version={version} build={build}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
