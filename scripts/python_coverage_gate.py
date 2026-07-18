#!/usr/bin/env python3
"""Enforce trace coverage for every testable Python product module."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


MAX_REPORT_BYTES = 4 * 1024 * 1024
TRACE_ROW = re.compile(r"^\s*\d+\s+(\d+)%\s+([A-Za-z0-9_.]+)\s+\(")
EXCLUDED_RELATIVE_MODULES = frozenset({
    # Thin macOS boundaries are verified by behavioral contract tests; trace
    # cannot observe PyObjC callbacks or the Security framework implementation.
    "keychain.py",
    "ui.py",
})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--minimum", required=True, type=int)
    parser.add_argument("--package-root", required=True)
    return parser


def discover_modules(package_root: Path) -> tuple[str, ...]:
    if package_root.is_symlink() or not package_root.is_dir():
        raise ValueError
    modules: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            raise ValueError
        relative = path.relative_to(package_root)
        relative_name = relative.as_posix()
        if path.name == "__init__.py" or relative_name in EXCLUDED_RELATIVE_MODULES:
            continue
        parts = relative.with_suffix("").parts
        if not all(part.isidentifier() for part in parts):
            raise ValueError
        modules.append(".".join((package_root.name, *parts)))
    if not modules or len(modules) != len(set(modules)):
        raise ValueError
    return tuple(modules)


def main(arguments: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(arguments)
        if not 0 <= args.minimum <= 100:
            raise ValueError
        path = Path(args.report)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError
        text = path.read_text(encoding="utf-8")
        modules = discover_modules(Path(args.package_root))
    except (OSError, UnicodeError, ValueError):
        print("python_coverage_invalid_report", file=sys.stderr)
        return 2

    coverage: dict[str, int] = {}
    for line in text.splitlines():
        match = TRACE_ROW.match(line)
        if match:
            coverage[match.group(2)] = int(match.group(1))
    for module in modules:
        if module not in coverage:
            print(f"python_coverage_missing_module {module}", file=sys.stderr)
            return 1
        if coverage[module] < args.minimum:
            print(
                f"python_coverage_below_threshold {module}={coverage[module]}% "
                f"minimum={args.minimum}%",
                file=sys.stderr,
            )
            return 1
    values = " ".join(f"{module}={coverage[module]}%" for module in modules)
    print(f"python_product_coverage {values} minimum={args.minimum}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
