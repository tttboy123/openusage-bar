import unittest
from pathlib import PurePosixPath, PureWindowsPath

from openusage_bar.runtime_descriptor import RuntimeDescriptor


class RuntimeDescriptorTests(unittest.TestCase):
    def test_windows_uses_loopback_tcp_with_separate_api_and_gateway_tokens(
        self,
    ) -> None:
        descriptor = RuntimeDescriptor.for_platform(
            "win32",
            state_dir=PureWindowsPath("C:/state/openusage-bar"),
        )

        self.assertEqual(descriptor.local_api_transport, "tcp")
        self.assertEqual(descriptor.local_api_host, "127.0.0.1")
        self.assertEqual(descriptor.local_api_port, 17821)
        self.assertIsNone(descriptor.local_api_socket_path)
        self.assertEqual(
            descriptor.local_api_token_path,
            PureWindowsPath("C:/state/openusage-bar/api.token"),
        )
        self.assertEqual(descriptor.gateway_host, "127.0.0.1")
        self.assertEqual(descriptor.gateway_port, 17823)
        self.assertEqual(descriptor.plugin_host, "127.0.0.1")
        self.assertEqual(descriptor.plugin_port, 17824)
        self.assertEqual(
            descriptor.plugin_state_dir,
            PureWindowsPath("C:/state/openusage-bar/plugin"),
        )
        self.assertEqual(
            descriptor.plugin_database_path,
            PureWindowsPath("C:/state/openusage-bar/plugin/plugin.sqlite3"),
        )
        self.assertEqual(
            descriptor.gateway_token_path,
            PureWindowsPath("C:/state/openusage-bar/gateway.token"),
        )
        self.assertNotEqual(
            descriptor.local_api_token_path,
            descriptor.gateway_token_path,
        )

    def test_macos_and_linux_use_a_private_unix_local_api_socket(
        self,
    ) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                descriptor = RuntimeDescriptor.for_platform(
                    platform,
                    state_dir=PurePosixPath("/state/openusage-bar"),
                )

                self.assertEqual(descriptor.local_api_transport, "unix")
                self.assertIsNone(descriptor.local_api_host)
                self.assertIsNone(descriptor.local_api_port)
                self.assertEqual(
                    descriptor.local_api_socket_path,
                    PurePosixPath("/state/openusage-bar/openusage.sock"),
                )
                self.assertIsNone(descriptor.local_api_token_path)
                self.assertEqual(descriptor.gateway_host, "127.0.0.1")
                self.assertEqual(descriptor.gateway_port, 17823)
                self.assertEqual(descriptor.plugin_host, "127.0.0.1")
                self.assertEqual(descriptor.plugin_port, 17824)
                self.assertEqual(
                    descriptor.plugin_state_dir,
                    PurePosixPath("/state/openusage-bar/plugin"),
                )
                self.assertEqual(
                    descriptor.gateway_token_path,
                    PurePosixPath("/state/openusage-bar/gateway.token"),
                )

    def test_linux_sys_platform_prefixes_use_the_unix_contract(self) -> None:
        for platform in ("linux2", "linux-musl"):
            with self.subTest(platform=platform):
                descriptor = RuntimeDescriptor.for_platform(
                    platform,
                    state_dir=PurePosixPath("/state/openusage-bar"),
                )

                self.assertEqual(descriptor.local_api_transport, "unix")
                self.assertIsNone(descriptor.local_api_host)
                self.assertIsNone(descriptor.local_api_port)
                self.assertEqual(
                    descriptor.local_api_socket_path,
                    PurePosixPath("/state/openusage-bar/openusage.sock"),
                )
                self.assertIsNone(descriptor.local_api_token_path)
                self.assertEqual(descriptor.gateway_host, "127.0.0.1")
                self.assertEqual(descriptor.gateway_port, 17823)
                self.assertEqual(
                    descriptor.gateway_token_path,
                    PurePosixPath("/state/openusage-bar/gateway.token"),
                )

    def test_unsupported_platform_failure_does_not_disclose_the_state_path(
        self,
    ) -> None:
        state_dir = PurePosixPath("/private/secret-user/runtime/openusage-bar")

        with self.assertRaises(RuntimeError) as raised:
            RuntimeDescriptor.for_platform("plan9", state_dir=state_dir)

        self.assertEqual(str(raised.exception), "unsupported platform")
        self.assertNotIn(str(state_dir), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
