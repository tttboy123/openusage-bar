#!/usr/bin/env python3
"""Silent hosted portability gate for the trusted Linux absence reader.

This is an instantaneous diagnostic for an exclusive disposable Linux/x64
runner.  It is not native lifecycle or release evidence and does not prove
Local API, ledger, Gateway, or persistent absence.
"""

from __future__ import annotations

import sys


_STAGE_STATUS = {
    "authority": 10,
    "runtime-peer": 11,
    "manager-provenance": 12,
    "manager-binary-readlink": 13,
    "manager-binary-path-value": 14,
    "manager-binary-metadata": 15,
    "manager-binary-public-identity": 16,
    "systemctl-binding": 17,
    "unit-absence": 18,
    "manager-query": 19,
    "sandwich": 20,
    "cleanup": 21,
}


def main(arguments: tuple[str, ...] | list[str] | None = None) -> int:
    """Return only a closed status code; never render observation details."""

    try:
        selected = tuple(sys.argv[1:] if arguments is None else arguments)
    except Exception:
        return 2
    if selected:
        return 2
    try:
        from openusage_bar import platform_services

        before = (
            platform_services.read_current_user_collector_service_absence_state()
        )
        after = (
            platform_services.read_current_user_collector_service_absence_state()
        )
        expected_type = platform_services.LinuxCollectorServiceAbsenceState
        if (
            type(before) is not expected_type
            or type(after) is not expected_type
            or after != before
        ):
            return 1
    except Exception as error:
        try:
            from openusage_bar.platform_services import ServiceCommandError

            if type(error) is ServiceCommandError:
                status = _STAGE_STATUS.get(error.stage)
                if status is not None:
                    return status
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
