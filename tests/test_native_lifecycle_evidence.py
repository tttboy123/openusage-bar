from __future__ import annotations

import unittest
import subprocess
import sys
from pathlib import Path


SOURCE_COMMIT = "a" * 40
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/native_lifecycle_evidence.py"


def windows_x64_record() -> dict[str, object]:
    return {
        "schemaVersion": "native-lifecycle-evidence/v1",
        "object": "native.lifecycle",
        "synthetic": False,
        "releaseEligible": False,
        "observedAt": "2026-08-11T01:02:03.000000Z",
        "sourceCommit": SOURCE_COMMIT,
        "target": {
            "platform": "win",
            "arch": "x64",
            "serviceManager": "task_scheduler",
        },
        "artifact": {
            "name": "UsageHub-0.8.6-win-x64.exe",
            "sha256": "b" * 64,
            "sizeBytes": 128,
        },
        "checks": {
            "install": "passed",
            "firstRun": "passed",
            "observeDefault": "passed",
            "serviceRegistered": "passed",
            "uninstallPreserve": "passed",
            "serviceRemoved": "passed",
            "reinstall": "passed",
            "uninstallDelete": "passed",
            "finalStateRemoved": "passed",
        },
        "gateway": {
            "defaultMode": "observe",
            "listenerActive": False,
            "cacheCreated": False,
            "telemetryCreated": False,
        },
        "privacy": {
            "providerCredentialReads": 0,
            "providerNetworkCalls": 0,
        },
        "persistence": {
            "ledgerOnPreserve": "preserved",
            "credentialsOnPreserve": "not_created",
            "gatewayCacheOnPreserve": "not_created",
            "gatewayTelemetryOnPreserve": "not_created",
            "stateAfterDelete": "removed",
        },
    }


class NativeLifecycleEvidenceTests(unittest.TestCase):
    def test_public_validator_accepts_the_closed_real_windows_x64_record(self) -> None:
        from scripts.native_lifecycle_evidence import validate_lifecycle_record

        record = windows_x64_record()

        self.assertEqual(validate_lifecycle_record(record), record)

    def test_cli_rejects_invalid_arguments_without_echoing_private_input(self) -> None:
        private_canary = "PRIVATE_LIFECYCLE_MACHINE_CANARY"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "generate",
                "--platform",
                private_canary,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "native_lifecycle_invalid reason=arguments_invalid\n",
        )
        self.assertNotIn(private_canary, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
