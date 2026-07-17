#!/usr/bin/env python3
"""Settings UI and strictly allowlisted headless collector entry point."""

from __future__ import annotations

import sys


COLLECTOR_COMMANDS = frozenset({
    "__refresh-once", "daemon", "status", "usage", "costs", "quotas", "sources",
    "providers", "changes", "doctor",
})


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from openusage_bar.ui import run_provider_settings

        run_provider_settings()
        return 0
    if arguments[0] not in COLLECTOR_COMMANDS:
        return 2
    from openusage_bar.collector_cli import main as collector_main

    return collector_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
