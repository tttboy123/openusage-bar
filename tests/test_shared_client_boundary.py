from __future__ import annotations

import io
import json
import os
import select
import signal
import unittest
from unittest.mock import Mock, patch


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class SharedClientBoundaryAttemptCounterTests(unittest.TestCase):
    def test_http_open_and_headless_keychain_get_increment_only_at_owned_boundaries(
        self,
    ) -> None:
        from openusage_bar.keychain import HeadlessKeychain
        from openusage_bar.network import BoundedHTTPClient, UnsafeEndpoint
        from openusage_bar.shared_client_boundary import (
            shared_client_boundary_attempt_counters,
        )

        resolver = lambda _host: ["93.184.216.34"]
        initial = shared_client_boundary_attempt_counters()
        successful_opener = Mock()
        successful_opener.open.return_value = _Response(b'{"ok":true}')
        self.assertEqual(
            BoundedHTTPClient(resolver, successful_opener).get_json(
                "https://api.example.com/usage", {}
            ),
            {"ok": True},
        )
        after_http_success = shared_client_boundary_attempt_counters()
        self.assertEqual(
            (
                after_http_success.bounded_http_open_attempts,
                after_http_success.headless_keychain_get_attempts,
            ),
            (
                initial.bounded_http_open_attempts + 1,
                initial.headless_keychain_get_attempts,
            ),
        )

        failing_opener = Mock()
        failing_opener.open.side_effect = OSError("PRIVATE_HTTP_FAILURE")
        with self.assertRaisesRegex(Exception, "Provider request failed"):
            BoundedHTTPClient(resolver, failing_opener).get_json(
                "https://api.example.com/usage", {}
            )
        after_http_failure = shared_client_boundary_attempt_counters()
        self.assertEqual(
            after_http_failure.bounded_http_open_attempts,
            after_http_success.bounded_http_open_attempts + 1,
        )

        with self.assertRaises(UnsafeEndpoint):
            BoundedHTTPClient(resolver, successful_opener).get_json(
                "http://api.example.com/usage", {}
            )
        self.assertEqual(shared_client_boundary_attempt_counters(), after_http_failure)

        successful_api = Mock()
        successful_api.get.return_value = b"secret"
        self.assertEqual(HeadlessKeychain(successful_api).get("provider-main"), "secret")
        after_keychain_success = shared_client_boundary_attempt_counters()
        self.assertEqual(
            after_keychain_success.headless_keychain_get_attempts,
            after_http_failure.headless_keychain_get_attempts + 1,
        )

        failing_api = Mock()
        failing_api.get.side_effect = RuntimeError("PRIVATE_CREDENTIAL_FAILURE")
        with self.assertRaisesRegex(RuntimeError, "PRIVATE_CREDENTIAL_FAILURE"):
            HeadlessKeychain(failing_api).get("provider-main")
        after_keychain_failure = shared_client_boundary_attempt_counters()
        self.assertEqual(
            after_keychain_failure.headless_keychain_get_attempts,
            after_keychain_success.headless_keychain_get_attempts + 1,
        )

        with self.assertRaises(ValueError):
            HeadlessKeychain(successful_api).get("")
        self.assertEqual(
            shared_client_boundary_attempt_counters(), after_keychain_failure
        )

    def test_saturated_counter_fails_before_the_external_boundary(self) -> None:
        from openusage_bar import shared_client_boundary as boundary
        from openusage_bar.keychain import HeadlessKeychain
        from openusage_bar.network import BoundedHTTPClient

        resolver = lambda _host: ["93.184.216.34"]
        maximum = (1 << 64) - 1
        cases = (
            ("_bounded_http_open_attempts", "http"),
            ("_headless_keychain_get_attempts", "keychain"),
        )
        for global_name, kind in cases:
            opener = Mock()
            api = Mock()
            before = boundary.shared_client_boundary_attempt_counters()
            with self.subTest(kind=kind), patch.object(boundary, global_name, maximum):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"^shared client boundary counter unavailable$",
                ):
                    if kind == "http":
                        BoundedHTTPClient(resolver, opener).get_json(
                            "https://api.example.com/usage", {}
                        )
                    else:
                        HeadlessKeychain(api).get("provider-main")
                saturated = boundary.shared_client_boundary_attempt_counters()
                self.assertEqual(getattr(saturated, global_name.removeprefix("_")), maximum)
                if kind == "http":
                    self.assertEqual(
                        saturated.headless_keychain_get_attempts,
                        before.headless_keychain_get_attempts,
                    )
                else:
                    self.assertEqual(
                        saturated.bounded_http_open_attempts,
                        before.bounded_http_open_attempts,
                    )
            opener.open.assert_not_called()
            api.get.assert_not_called()

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_forked_child_rekeys_resets_and_does_not_inherit_a_locked_mutex(self) -> None:
        from openusage_bar import shared_client_boundary as boundary

        boundary._record_bounded_http_open_attempt()
        parent = boundary.shared_client_boundary_attempt_counters()
        read_fd, write_fd = os.pipe()
        boundary._COUNTER_LOCK.acquire()
        try:
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    os.close(read_fd)
                    signal.alarm(3)
                    observed = boundary.shared_client_boundary_attempt_counters()
                    payload = json.dumps(
                        {
                            "epoch": observed.process_epoch_sha256,
                            "http": observed.bounded_http_open_attempts,
                            "keychain": observed.headless_keychain_get_attempts,
                        },
                        sort_keys=True,
                    ).encode("ascii")
                    os.write(write_fd, payload)
                    os._exit(0)
                except BaseException:
                    os._exit(2)
        finally:
            boundary._COUNTER_LOCK.release()
            os.close(write_fd)

        ready, _, _ = select.select([read_fd], [], [], 5.0)
        payload = os.read(read_fd, 4096) if ready else b""
        os.close(read_fd)
        _, status = os.waitpid(child_pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        child = json.loads(payload)
        self.assertNotEqual(child["epoch"], parent.process_epoch_sha256)
        self.assertEqual((child["http"], child["keychain"]), (0, 0))

    def test_counter_fact_is_closed_bounded_and_process_epoch_scoped(self) -> None:
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
            shared_client_boundary_attempt_counters,
        )

        observed = shared_client_boundary_attempt_counters()
        self.assertRegex(observed.process_epoch_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(repr(observed), "<SharedClientBoundaryAttemptCounters closed>")
        for values in (
            ("a" * 64, True, 0),
            ("a" * 64, -1, 0),
            ("a" * 64, 0, 1 << 64),
            ("A" * 64, 0, 0),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SharedClientBoundaryAttemptCounters(*values)


if __name__ == "__main__":
    unittest.main()
