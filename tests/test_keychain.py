import unittest
from unittest.mock import Mock

from openusage_bar.keychain import MacOSKeychain


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


if __name__ == "__main__":
    unittest.main()
