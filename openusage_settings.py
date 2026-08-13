#!/usr/bin/env python3
"""Settings UI and strictly allowlisted headless collector entry point."""

from __future__ import annotations

import sys


COLLECTOR_COMMANDS = frozenset({
    "__refresh-once", "daemon", "status", "snapshot", "usage", "costs", "quotas",
    "sources", "providers", "changes", "doctor",
    "dashboard", "service", "executor", "connect", "reconcile",
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
    if arguments == ["gateway-account-mutate"]:
        from openusage_bar.gateway.commands import run_gateway_account_mutation

        if sys.platform == "darwin":
            from openusage_bar.keychain import MacOSKeychain

            # Keep every native account mutation under this packaged helper's
            # single Keychain ACL identity. The desktop host bounds this child.
            return run_gateway_account_mutation(
                sys.stdin,
                sys.stdout,
                keychain=MacOSKeychain(),
            )
        return run_gateway_account_mutation(sys.stdin, sys.stdout)
    if (
        len(arguments) == 2
        and arguments[0] == "__gateway-account-credential-roundtrip"
        and sys.platform == "darwin"
    ):
        from openusage_bar.gateway.commands import (
            run_gateway_account_credential_roundtrip,
        )
        from openusage_bar.keychain import MacOSKeychain

        # One process identity prevents a onefile diagnostic from crossing a
        # Keychain ACL boundary between create/edit/remove.
        return run_gateway_account_credential_roundtrip(
            sys.stdin,
            sys.stdout,
            keychain=MacOSKeychain(keychain_path=arguments[1]),
        )
    if arguments == ["gateway-account-editor"]:
        from openusage_bar.gateway.account_editor_tk import run_gateway_account_editor

        return run_gateway_account_editor(sys.stdin, sys.stdout)
    if arguments == ["gateway-account-editor", "--ui-self-test"]:
        from openusage_bar.gateway.account_editor_tk import (
            run_gateway_account_editor_self_test,
        )

        return run_gateway_account_editor_self_test(sys.stdout)
    if arguments == ["__keychain-write"]:
        from openusage_bar.keychain import run_native_keychain_write

        return run_native_keychain_write(sys.stdin.buffer, sys.stdout.buffer)
    if arguments[0] not in COLLECTOR_COMMANDS:
        return 2
    from openusage_bar.collector_cli import main as collector_main

    return collector_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
