from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class LinuxServiceAbsenceCanaryTests(unittest.TestCase):
    def test_main_accepts_only_two_stable_closed_negative_facts(self):
        from openusage_bar.platform_services import (
            LinuxCollectorServiceAbsenceState,
        )
        from scripts.canary_linux_service_absence import main

        fact = LinuxCollectorServiceAbsenceState(
            unit_missing=True,
            unit_id="openusage-bar.service",
            load_state="not-found",
            active_state="inactive",
            sub_state="dead",
            unit_file_state=None,
            main_pid=0,
            control_pid=0,
            job=None,
            fragment_path=None,
            drop_in_paths=(),
            needs_reload=False,
        )

        for name, observations, expected in (
            ("stable", (fact, fact), 0),
            ("reader_failure", (fact, RuntimeError("PRIVATE")), 1),
        ):
            with self.subTest(name=name):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch(
                        "openusage_bar.platform_services."
                        "read_current_user_collector_service_absence_state",
                        side_effect=observations,
                    ) as reader,
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = main(())

                self.assertEqual(result, expected)
                self.assertEqual(reader.call_count, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "openusage_bar.platform_services."
                "read_current_user_collector_service_absence_state",
            ) as reader,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = main(("PRIVATE",))

        self.assertEqual(result, 2)
        reader.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_main_maps_only_closed_reader_stages_to_private_status_codes(self):
        from openusage_bar.platform_services import ServiceCommandError
        from scripts.canary_linux_service_absence import main

        stages = (
            (10, "authority"),
            (11, "runtime-peer"),
            (12, "manager-provenance"),
            (13, "manager-binary"),
            (14, "systemctl-binding"),
            (15, "unit-absence"),
            (16, "manager-query"),
            (17, "sandwich"),
            (18, "cleanup"),
        )
        for status, stage in stages:
            with self.subTest(stage=stage):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch(
                        "openusage_bar.platform_services."
                        "read_current_user_collector_service_absence_state",
                        side_effect=ServiceCommandError(stage=stage),
                    ),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = main(())

                self.assertEqual(result, status)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

        with self.assertRaises(ValueError):
            ServiceCommandError(stage="PRIVATE")
        self.assertEqual(
            str(ServiceCommandError(stage="manager-query")),
            "service activation command failed",
        )


if __name__ == "__main__":
    unittest.main()
