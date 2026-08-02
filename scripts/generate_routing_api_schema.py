#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openusage_bar.routing_api import (
    ROUTING_API_SCHEMA,
    ROUTING_REPLAY_SCHEMA,
    ROUTING_SHADOW_SCHEMA,
)


DEFAULT_OUTPUT = ROOT / "openusage_bar" / "resources" / "routing-api-v1.schema.json"
SCHEMAS = {
    "decision": (ROUTING_API_SCHEMA, "routing-api-v1.schema.json"),
    "shadow": (ROUTING_SHADOW_SCHEMA, "routing-shadow-v1.schema.json"),
    "replay": (ROUTING_REPLAY_SCHEMA, "routing-replay-v1.schema.json"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=tuple(SCHEMAS), default="decision")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    schema, resource_name = SCHEMAS[args.kind]
    output = args.output or (
        DEFAULT_OUTPUT
        if args.kind == "decision"
        else ROOT / "openusage_bar" / "resources" / resource_name
    )
    output.write_text(
        json.dumps(
            schema,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
