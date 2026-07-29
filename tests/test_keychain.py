import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from openusage_bar.keychain import (
    BoundedMacOSKeychain,
    KeychainError,
    MacOSKeychain,
    run_native_keychain_write,
)


class KeychainTests(unittest.TestCase):
    def test_uses_fixed_service_and_provider_account(self):
        api = Mock()
        api.update.return_value = True

        MacOSKeychain(api).set("minimax-main", "secret")

        query, value = api.update.call_args.args
        self.assertEqual(query["service"], "com.lune.openusage-menubar")
        self.assertEqual(query["account"], "minimax-main")
        self.assertEqual(value, b"secret")

    def test_adds_missing_item(self):
        api = Mock()
        api.update.return_value = False

        MacOSKeychain(api).set("demo", "secret")

        api.add.assert_called_once_with(
            {"service": "com.lune.openusage-menubar", "account": "demo"}, b"secret"
        )

    def test_decodes_loaded_value(self):
        api = Mock()
        api.get.return_value = "密钥".encode()

        self.assertEqual(MacOSKeychain(api).get("demo"), "密钥")

    def test_native_helper_rejects_reads_and_only_writes_step_plan_token(self):
        keychain = Mock()
        keychain.get.return_value = "密钥"
        output = io.BytesIO()

        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1, "action": "get", "account": "step-plan-main",
            }).encode()),
            output,
            keychain=keychain,
        )

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": False,
        })
        keychain.get.assert_not_called()

        output = io.BytesIO()
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "set",
                "account": "step-plan-main.oasis-token",
                "secret": "rotated-private-token",
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": True,
        })
        keychain.set.assert_called_once_with(
            "step-plan-main.oasis-token", "rotated-private-token"
        )

        output = io.BytesIO()
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "set",
                "account": "minimax-main",
                "secret": "must-not-be-written",
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": False,
        })
        self.assertEqual(keychain.set.call_count, 1)

    def test_bounded_keychain_reads_via_reader_and_privately_writes_session(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "keychain-helper.py"
            capture = Path(directory) / "capture.json"
            helper.write_text(
                "import json,sys\n"
                "request=json.loads(sys.stdin.buffer.read())\n"
                f"open({str(capture)!r},'w').write(json.dumps({{"
                "'argv':sys.argv[1:],'request':request}))\n"
                "response={'version':1,'ok':True}\n"
                "sys.stdout.write(json.dumps(response))\n",
                encoding="utf-8",
            )
            reader = Mock()
            reader.get.return_value = "stored-value"
            keychain = BoundedMacOSKeychain(
                helper_command=(sys.executable, str(helper)),
                timeout_seconds=2,
                reader=reader,
            )

            self.assertEqual(keychain.get("step-plan-main"), "stored-value")
            reader.get.assert_called_once_with("step-plan-main")
            self.assertFalse(capture.exists())
            secret = "rotated-private-token"
            keychain.set("step-plan-main.oasis-token", secret)
            captured = json.loads(capture.read_text(encoding="utf-8"))

        self.assertEqual(captured["argv"], ["__keychain-write"])
        self.assertEqual(captured["request"]["secret"], secret)
        self.assertNotIn(secret, " ".join(keychain.command))

    def test_bounded_keychain_timeout_is_sanitized_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "keychain-helper.py"
            helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            keychain = BoundedMacOSKeychain(
                helper_command=(sys.executable, str(helper)),
                timeout_seconds=1,
                reader=Mock(get=Mock(return_value=None)),
            )

            started = time.monotonic()
            self.assertIsNone(keychain.get("step-plan-main"))
            self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(KeychainError) as raised:
                keychain.set("step-plan-main.oasis-token", "never-in-error")
            self.assertNotIn("never-in-error", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
