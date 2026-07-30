from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "canary_report.yml"
PROTOCOL = ROOT / "docs" / "canary.md"


class CanaryIntakeContractTests(unittest.TestCase):
    def test_form_requests_every_release_gate_without_private_material(self):
        form = FORM.read_text(encoding="utf-8")

        for required in (
            "Previous version and build",
            "Scheduled collection advanced after restart without Refresh",
            "Menu-bar item was visibly present and opened its popover",
            "UI, CLI JSON, and Local API agreed at one dataRevision",
            "Balance state and freshness were checked",
            "Privacy scan result for attached diagnostics",
            "UTC event dates",
            "compatibility-v1.md",
        ):
            self.assertIn(required, form)
        for forbidden in (
            "Paste API key",
            "Paste cookie",
            "Paste raw log",
            "Provider account name:",
        ):
            self.assertNotIn(forbidden, form)

    def test_form_ids_are_unique_and_every_safety_checkbox_is_required(self):
        form = FORM.read_text(encoding="utf-8")
        identifiers = re.findall(r"^    id: ([a-z0-9-]+)$", form, re.MULTILINE)
        self.assertEqual(len(identifiers), len(set(identifiers)))

        safety = form.split("id: safety", 1)[1].split("  - type:", 1)[0]
        self.assertGreaterEqual(safety.count("required: true"), 3)

    def test_protocol_keeps_beta_activation_as_an_explicit_gate(self):
        protocol = PROTOCOL.read_text(encoding="utf-8")

        self.assertIn("Intake readiness and clock activation", protocol)
        self.assertIn("intake_ready", protocol)
        self.assertIn("does not start the 30-day clock", protocol)
        self.assertIn("compatibility-v1.md", protocol)
        self.assertIn("aggregate Balance state/quality/stale counts", protocol)
        self.assertIn("scripts/verify_canary_surfaces.py", protocol)
        self.assertIn("visualMenu", protocol)
        self.assertIn("pending_manual", protocol)
        self.assertIn("does not replace the visual menu-bar check", protocol)


if __name__ == "__main__":
    unittest.main()
