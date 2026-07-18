#!/usr/bin/env python3
"""Fail closed when tracked source or Git history contains credential material."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


MAX_FILE_BYTES = 8 * 1024 * 1024
HIGH_CONFIDENCE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9])Q4KG[A-Za-z0-9]{48,}"),
    re.compile(r"\bStep(?:Fun)?\s+Plan\s+[A-Za-z0-9]{48,}\b", re.IGNORECASE),
    re.compile(
        r"(?:Oasis[-_ ]?Token)\s*=\s*[A-Za-z0-9._~+/=-]{20,}",
        re.IGNORECASE,
    ),
)
CONTEXTUAL_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
    ),
    re.compile(
        r"(?:Authorization\s*:\s*Bearer|api[_ -]?key|"
        r"access[_ -]?token|refresh[_ -]?token|password|secret|cookie)"
        r"\s*[=:]\s*(?:"
        r"[\"'][A-Za-z0-9._~+/=-]{20,}[\"']|"
        r"(?!self\.|config\.|keychain\.|os\.|getattr\()"
        r"[A-Za-z0-9_~+/=-]{20,})",
        re.IGNORECASE,
    ),
)


def _git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _contains_secret(payload: str, *, contextual: bool = True) -> bool:
    patterns = HIGH_CONFIDENCE_PATTERNS + (CONTEXTUAL_PATTERNS if contextual else ())
    return any(pattern.search(payload) for pattern in patterns)


def _is_fixture_path(path: Path) -> bool:
    return path.parts[0] == "tests" or path.parts[:2] == ("docs", "testing")


def scan_tree(root: Path) -> bool:
    tracked = _git("ls-files", "-z", cwd=root)
    if tracked.returncode != 0:
        raise RuntimeError("tracked file enumeration unavailable")
    for raw_name in tracked.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = Path(raw_name.decode("utf-8", errors="strict"))
        path = root / relative
        try:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("tracked file unavailable")
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            payload = path.read_bytes()
        except (OSError, UnicodeError) as error:
            raise RuntimeError("tracked file scan unavailable") from error
        if b"\0" in payload:
            continue
        if _contains_secret(
            payload.decode("utf-8", errors="replace"),
            contextual=not _is_fixture_path(relative),
        ):
            return True
    return False


def scan_history(root: Path) -> bool:
    history = _git(
        "log",
        "--all",
        "-p",
        "--no-color",
        "--no-ext-diff",
        "--format=fuller",
        cwd=root,
    )
    if history.returncode != 0:
        raise RuntimeError("Git history scan unavailable")
    full_history = history.stdout.decode("utf-8", errors="replace")
    if _contains_secret(full_history, contextual=False):
        return True
    production_history = _git(
        "log",
        "--all",
        "-p",
        "--no-color",
        "--no-ext-diff",
        "--format=fuller",
        "--",
        ".",
        ":!tests",
        ":!docs/testing",
        cwd=root,
    )
    if production_history.returncode != 0:
        raise RuntimeError("Git production history scan unavailable")
    return _contains_secret(
        production_history.stdout.decode("utf-8", errors="replace")
    )


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    parsed = parser.parse_args(arguments)
    root_result = _git("rev-parse", "--show-toplevel", cwd=Path.cwd())
    if root_result.returncode != 0:
        print("release_secret_scan_unavailable", file=sys.stderr)
        return 1
    root = Path(root_result.stdout.decode().strip()).resolve()
    try:
        if scan_tree(root):
            print("release_secret_scan_forbidden_material scope=tree", file=sys.stderr)
            return 1
        if parsed.history and scan_history(root):
            print("release_secret_scan_forbidden_material scope=history", file=sys.stderr)
            return 1
    except (RuntimeError, UnicodeError, ValueError):
        print("release_secret_scan_unavailable", file=sys.stderr)
        return 1
    scopes = "tree,history" if parsed.history else "tree"
    print(f"release_secret_scan_matches=0 scopes={scopes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
