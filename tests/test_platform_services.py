import sys
import unittest

from openusage_bar import platform_services


class PlatformServicesRenderTests(unittest.TestCase):
    def test_launchd_plist_is_xml_with_label_and_interval(self):
        rendered = platform_services.launchd_plist(interval=300)

        self.assertIn("<key>Label</key>", rendered)
        self.assertIn("com.lune.openusagebar.collector", rendered)
        self.assertIn("--interval", rendered)
        self.assertIn("300", rendered)

    def test_systemd_unit_contains_exec_and_wanted_by(self):
        unit = platform_services.systemd_unit(interval=300)

        self.assertIn("[Unit]", unit)
        self.assertIn("ExecStart=openusage-bar daemon --interval 300", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_windows_task_xml_contains_command_and_interval(self):
        xml = platform_services.windows_task_xml(interval_minutes=5)

        self.assertIn("openusage-bar", xml)
        self.assertIn("daemon --interval 300", xml)
        self.assertIn("<Task version=", xml)

    def test_render_current_platform_matches_active_platform(self):
        rendered = platform_services.render_current_platform(interval=300)

        self.assertTrue(rendered)
        if sys.platform == "darwin":
            self.assertIn("<plist", rendered)
        elif sys.platform.startswith("linux"):
            self.assertIn("[Unit]", rendered)
        elif sys.platform == "win32":
            self.assertIn("<Task", rendered)
