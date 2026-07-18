#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
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
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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
