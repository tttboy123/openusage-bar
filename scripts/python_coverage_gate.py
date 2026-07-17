#!/usr/bin/env python3
"""Enforce standard-library trace coverage for an explicit module boundary."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


MAX_REPORT_BYTES = 4 * 1024 * 1024
TRACE_ROW = re.compile(r"^\s*\d+\s+(\d+)%\s+([A-Za-z0-9_.]+)\s+\(")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--minimum", required=True, type=int)
    parser.add_argument("modules", nargs="+")
    return parser


def main(arguments: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(arguments)
        if not 0 <= args.minimum <= 100 or len(set(args.modules)) != len(args.modules):
            raise ValueError
        path = Path(args.report)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        print("python_coverage_invalid_report", file=sys.stderr)
        return 2

    coverage: dict[str, int] = {}
    for line in text.splitlines():
        match = TRACE_ROW.match(line)
        if match:
            coverage[match.group(2)] = int(match.group(1))
    for module in args.modules:
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
    values = " ".join(f"{module}={coverage[module]}%" for module in args.modules)
    print(f"python_touched_coverage {values} minimum={args.minimum}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
