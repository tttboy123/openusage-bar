import sqlite3
import tempfile
import unittest
from pathlib import Path

from openusage_bar.executors import (
    CcSwitchExecutor,
    OmniRouteExecutor,
    apply_executor_switch,
)


def _cc_switch_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE providers ("
        "id TEXT, app_type TEXT, name TEXT, category TEXT, "
        "is_current INTEGER, sort_index INTEGER)"
    )
    connection.execute(
        "INSERT INTO providers VALUES "
        "('codex-official','codex','OpenAI Official','official',0,0)"
    )
    connection.execute(
        "INSERT INTO providers VALUES (?, 'codex', ?, 'cn_official', 1, 1)",
        ("provider-deepseek", "DeepSeek"),
    )
    connection.commit()
    connection.close()


class CcSwitchExecutorTests(unittest.TestCase):
    def test_state_reports_active_codex_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cc-switch.db"
            _cc_switch_db(db)

            state = CcSwitchExecutor(db_path=db).state()

            self.assertTrue(state.available)
            self.assertEqual(state.active_target, "DeepSeek")
            self.assertTrue(CcSwitchExecutor(db_path=db).verify())

    def test_missing_database_is_unavailable(self):
        executor = CcSwitchExecutor(db_path=Path("/nonexistent/cc-switch.db"))

        self.assertFalse(executor.state().available)
        self.assertIsNone(executor.state().active_target)
        self.assertFalse(executor.verify())

    def test_switch_fails_closed_without_programmatic_interface(self):
        result = CcSwitchExecutor().switch("deepseek")

        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "cc_switch_has_no_programmatic_interface")

    def test_switch_rejects_invalid_target(self):
        with self.assertRaises(ValueError):
            CcSwitchExecutor().switch("bad/target")


class OmniRouteExecutorTests(unittest.TestCase):
    def test_state_and_verify_use_local_usage_file(self):
        with tempfile.TemporaryDirectory() as directory:
            usage = Path(directory) / "usage.json"
            usage.write_text("{}", encoding="utf-8")

            state = OmniRouteExecutor(usage_path=usage).state()

            self.assertTrue(state.available)
            self.assertTrue(OmniRouteExecutor(usage_path=usage).verify())

    def test_missing_usage_file_is_unavailable(self):
        executor = OmniRouteExecutor(usage_path=Path("/nonexistent/usage.json"))

        self.assertFalse(executor.state().available)
        self.assertFalse(executor.verify())

    def test_switch_with_injected_backend(self):
        backend_calls = []

        def backend(target: str) -> bool:
            backend_calls.append(target)
            return target == "deepseek-v4-flash"

        executor = OmniRouteExecutor(switch_backend=backend)
        ok = executor.switch("deepseek-v4-flash")
        failed = executor.switch("other-model")

        self.assertTrue(ok.ok)
        self.assertEqual(ok.detail, "omniroute_switch_completed")
        self.assertFalse(failed.ok)
        self.assertEqual(failed.detail, "omniroute_switch_failed")
        self.assertEqual(backend_calls, ["deepseek-v4-flash", "other-model"])

    def test_switch_without_backend_fails_closed(self):
        result = OmniRouteExecutor().switch("deepseek-v4-flash")

        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "omniroute_switch_backend_unavailable")


class ApplyExecutorSwitchTests(unittest.TestCase):
    def test_auto_apply_disabled_by_default(self):
        result = apply_executor_switch(
            CcSwitchExecutor(), "deepseek", enabled=False
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "auto_apply_disabled")

    def test_auto_apply_enabled_delegates_to_executor(self):
        result = apply_executor_switch(
            CcSwitchExecutor(), "deepseek", enabled=True
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "cc_switch_has_no_programmatic_interface")
