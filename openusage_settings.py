#!/usr/bin/env python3
"""Settings UI and strictly allowlisted headless collector entry point."""

from __future__ import annotations

import sys


COLLECTOR_COMMANDS = frozenset({
    "__refresh-once", "daemon", "status", "snapshot", "usage", "costs", "quotas",
    "sources", "providers", "changes", "doctor", "runtime-ingest", "runtime-summary",
    "route", "proxy",
})


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from openusage_bar.ui import run_provider_settings

        run_provider_settings()
        return 0
    if arguments == ["provider-mutate"]:
        from openusage_bar.provider_commands import run_provider_mutation

        return run_provider_mutation(sys.stdin, sys.stdout)
    if arguments == ["routing-mutate"]:
        from openusage_bar.routing_commands import run_routing_mutation

        return run_routing_mutation(sys.stdin, sys.stdout)
    if arguments == ["__keychain-write"]:
        from openusage_bar.keychain import run_native_keychain_write

        return run_native_keychain_write(sys.stdin.buffer, sys.stdout.buffer)
    if arguments[0] not in COLLECTOR_COMMANDS:
        return 2
    from openusage_bar.collector_cli import main as collector_main

    return collector_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
