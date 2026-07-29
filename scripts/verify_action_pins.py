#!/usr/bin/env python3
"""Verify immutable GitHub Action pins and their human-readable versions."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ACTION_REFERENCE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*"
    r"(?P<quote>['\"]?)"
    r"(?P<repository>actions/[A-Za-z0-9_./-]+)@"
    r"(?P<reference>[^\s#'\"]+)"
    r"(?P=quote)"
    r"(?:\s+#\s*(?P<comment>.*?))?\s*$",
    re.MULTILINE,
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
EXACT_RELEASE_TAG = re.compile(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?"
)


def action_references(source: str) -> list[tuple[str, str, str]]:
    return [
        (
            match.group("repository"),
            match.group("reference"),
            (match.group("comment") or "").strip(),
        )
        for match in ACTION_REFERENCE.finditer(source)
    ]


def action_pin_issues(
    source: str,
    approved: dict[tuple[str, str], str],
) -> list[str]:
    issues: list[str] = []
    for repository, commit, version_comment in action_references(source):
        expected_version = approved.get((repository, commit))
        if expected_version is not None and version_comment != expected_version:
            found = version_comment or "missing"
            issues.append(
                f"{repository}@{commit} must use version comment "
                f"{expected_version}, found {found}"
            )
    return issues


def _approved_pins(
    manifest_path: Path,
) -> tuple[dict[tuple[str, str], str], list[str]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, ["action-pins.json: manifest is unavailable or invalid JSON"]
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        return {}, ["action-pins.json: schemaVersion must be 1"]
    pins = manifest.get("pins")
    if not isinstance(pins, list):
        return {}, ["action-pins.json: pins must be an array"]

    approved: dict[tuple[str, str], str] = {}
    issues: list[str] = []
    for index, entry in enumerate(pins, start=1):
        if not isinstance(entry, dict):
            issues.append(f"action-pins.json: pin {index} must be an object")
            continue
        repository = entry.get("repository")
        commit = entry.get("commit")
        version = entry.get("version")
        if not isinstance(repository, str) or not repository.startswith("actions/"):
            issues.append(
                f"action-pins.json: pin {index} repository must start with actions/"
            )
            continue
        if not isinstance(commit, str) or FULL_COMMIT_SHA.fullmatch(commit) is None:
            issues.append(
                f"action-pins.json: pin {index} commit must be a full lowercase "
                "40-character SHA"
            )
        if (
            not isinstance(version, str)
            or EXACT_RELEASE_TAG.fullmatch(version) is None
        ):
            issues.append(
                f"action-pins.json: pin {index} version must be an exact vX.Y.Z "
                "release tag"
            )
        if not isinstance(commit, str) or not isinstance(version, str):
            continue
        key = (repository, commit)
        if key in approved:
            issues.append(
                f"action-pins.json: duplicate pin {repository}@{commit}"
            )
            continue
        approved[key] = version
    return approved, issues


def verify_action_pin_repository(root: Path) -> list[str]:
    manifest_path = root / ".github/action-pins.json"
    approved, issues = _approved_pins(manifest_path)
    used: set[tuple[str, str]] = set()
    workflow_directory = root / ".github/workflows"
    workflows = sorted(
        (*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml")),
        key=lambda path: path.name,
    )
    for workflow in workflows:
        source = workflow.read_text(encoding="utf-8")
        for repository, commit, _version_comment in action_references(source):
            used.add((repository, commit))
            if (repository, commit) not in approved:
                issues.append(
                    f"{workflow.name}: {repository}@{commit} is not present in "
                    ".github/action-pins.json"
                )
        issues.extend(
            f"{workflow.name}: {issue}"
            for issue in action_pin_issues(source, approved)
        )
    for repository, commit in sorted(set(approved) - used):
        issues.append(
            f"action-pins.json: {repository}@{commit} is not used by a "
            "committed workflow"
        )
    return issues


def main(arguments: list[str]) -> int:
    if len(arguments) > 1:
        print("usage: verify_action_pins.py [repository-root]", file=sys.stderr)
        return 2
    root = Path(arguments[0]).resolve() if arguments else Path.cwd().resolve()
    issues = verify_action_pin_repository(root)
    if issues:
        for issue in issues:
            print(f"action_pin_verification_failed {issue}", file=sys.stderr)
        return 1
    print("action_pin_verification_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
