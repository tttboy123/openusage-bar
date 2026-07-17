import subprocess
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PrivacyScanTests(unittest.TestCase):
    def test_empty_or_null_forbidden_fields_fail_every_scan_target(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        for payload in ('{"apiKey":null}', '{"prompt":""}', '{"authorization":""}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                candidate = Path(temp) / "payload.json"
                candidate.write_text(payload, encoding="utf-8")
                result = subprocess.run([str(scanner), str(candidate)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(payload, result.stderr)

    def test_safe_runtime_facts_pass(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "health.json"
            candidate.write_text('{"schemaVersion":"1.0","todayTokens":0}', encoding="utf-8")
            result = subprocess.run([str(scanner), str(candidate)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "privacy_scan_matches=0 files=1\n")

    def test_catalog_credential_metadata_is_not_treated_as_a_secret_field(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        payload = (
            '{"credential_type":"api_key","credential_scope":"step_plan_api_key"}'
        )
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "catalog.json"
            candidate.write_text(payload, encoding="utf-8")
            result = subprocess.run([str(scanner), str(candidate)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_scans_nested_directories_and_structurally_scans_canonical_sqlite(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nested").mkdir()
            (root / "nested" / "safe.json").write_text(
                '{"providerId":"codex","familyId":"codex"}', encoding="utf-8"
            )
            from openusage_bar.activity_store import ActivityStore
            store = ActivityStore(root / "activity.sqlite3")
            store.close()
            result = subprocess.run([str(scanner), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "privacy_scan_matches=0 files=1 sqlite_files=1\n")

    def test_sqlite_unexpected_schema_and_secret_value_fail_without_echo(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        from openusage_bar.activity_store import ActivityStore
        for mode in ("table", "value"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/"activity.sqlite3"; store=ActivityStore(path); store.close()
                db=sqlite3.connect(path)
                if mode=="table": db.execute("CREATE TABLE secrets(value TEXT)")
                else: db.execute("INSERT INTO ledger_meta VALUES('note',?)",("sk-"+"X"*30,))
                db.commit(); db.close()
                result=subprocess.run([str(scanner),str(path)],capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(result.stderr,"privacy_scan_forbidden_material\n")
                self.assertNotIn("sk-",result.stderr)

    def test_sqlite_scan_does_not_mutate_database_or_wal(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        from openusage_bar.activity_store import ActivityStore
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "activity.sqlite3"
            store = ActivityStore(path)
            store.record_source_success(
                "codex", "openusage.daily", datetime.now(timezone.utc)
            )
            wal = Path(str(path) + "-wal")
            self.assertTrue(wal.exists())
            before = {candidate: candidate.read_bytes() for candidate in (path, wal)}
            result = subprocess.run(
                [str(scanner), str(path)], capture_output=True, text=True
            )
            after = {candidate: candidate.read_bytes() for candidate in (path, wal)}
            store.close()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, after)

    def test_nested_forbidden_material_fails_without_echoing_path_or_value(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = root / "private-name.json"
            secret.write_text('{"refreshToken":"not-printed"}', encoding="utf-8")
            result = subprocess.run([str(scanner), str(root)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "privacy_scan_forbidden_material\n")
            self.assertNotIn(str(secret), result.stderr)

    def test_missing_or_symlink_targets_fail_closed(self):
        scanner = ROOT / "scripts/privacy_scan.py"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            safe = root / "safe.json"
            safe.write_text('{"providerId":"codex"}', encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(safe)
            for target in (link, root / "missing.json"):
                with self.subTest(target=target.name):
                    result = subprocess.run(
                        [str(scanner), str(target)], capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stderr, "privacy_scan_forbidden_material\n")


if __name__ == "__main__":
    unittest.main()
