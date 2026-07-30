#!/usr/bin/env python3
"""Validate one declarative Provider Adapter Kit bundle."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.provider_conformance import load_provider_adapter_bundle


def validate_bundle(path: Path) -> dict[str, object]:
    bundle = load_provider_adapter_bundle(path)
    return {
        "bundle": bundle.fixture.fixture_id,
        "caseCount": len(bundle.fixture.cases),
        "configKinds": [getattr(config, "type") for config in bundle.configs],
        "ok": True,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(
            '{"error":"invalid_arguments","ok":false}',
            file=sys.stderr,
        )
        return 2
    try:
        result = validate_bundle(Path(arguments[0]))
    except Exception:
        print(
            '{"error":"invalid_bundle","ok":false}',
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
