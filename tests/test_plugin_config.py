from __future__ import annotations

import os
import stat
import tempfile
import unittest
import threading
from pathlib import Path
from unittest import mock

from openusage_bar.plugin import config as plugin_config
from openusage_bar.plugin.config import PluginPrincipalRegistry
from openusage_bar.plugin.contracts import PRINCIPALS


class PluginConfigTests(unittest.TestCase):
    def test_registry_creates_four_distinct_private_principal_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "plugin"
            registry = PluginPrincipalRegistry.load_or_create(state)
            tokens = [
                registry.token_path(principal).read_text(encoding="ascii").strip()
                for principal in PRINCIPALS
            ]
            self.assertEqual(len(set(tokens)), 4)
            self.assertTrue(all("\n" not in token and "\r" not in token for token in tokens))
            for principal, token in zip(PRINCIPALS, tokens, strict=True):
                self.assertEqual(registry.authenticate(token), principal)
                path = registry.token_path(principal)
                self.assertEqual(path.name, f"{principal}.token")
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_bytes(), token.encode("ascii"))

    def test_registry_never_accepts_principal_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registry = PluginPrincipalRegistry.load_or_create(Path(temporary) / "plugin")
            self.assertIsNone(registry.authenticate("loom"))
            self.assertIsNone(registry.authenticate("Bearer loom"))

    def test_concurrent_first_create_never_overwrites_a_principal_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "plugin"
            barrier = threading.Barrier(8)
            observed: list[tuple[bytes, ...]] = []

            def load() -> None:
                barrier.wait()
                registry = PluginPrincipalRegistry.load_or_create(state)
                observed.append(tuple(registry.token_path(p).read_bytes() for p in PRINCIPALS))

            threads = [threading.Thread(target=load) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(len(observed), 8)
            self.assertTrue(all(item == observed[0] for item in observed))

    def test_concurrent_windows_first_create_has_one_acl_writer_per_token(self) -> None:
        class CoordinatedWindowsSecurity:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self._states: dict[tuple[int, int], dict[str, object]] = {}
                self._blocked_identity: tuple[int, int] | None = None
                self.first_harden_started = threading.Event()
                self.release_first_harden = threading.Event()
                self.competing_harden = threading.Event()

            def harden_directory(self, _directory: Path) -> None:
                return None

            def _state(self, descriptor: int) -> dict[str, object]:
                metadata = os.fstat(descriptor)
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                with self._lock:
                    return self._states.setdefault(
                        identity,
                        {
                            "hardened": False,
                            "harden_calls": 0,
                            "verify_calls": 0,
                        },
                    )

            def harden_file(self, descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                state = self._state(descriptor)
                with self._lock:
                    state["harden_calls"] = int(state["harden_calls"]) + 1
                    call = int(state["harden_calls"])
                    if call > 1:
                        self.competing_harden.set()
                    should_block = self._blocked_identity is None
                    if should_block:
                        self._blocked_identity = identity
                        self.first_harden_started.set()
                if call > 1:
                    raise OSError("concurrent ACL writer")
                if should_block and not self.release_first_harden.wait(2):
                    raise OSError("creator ACL publication timed out")
                with self._lock:
                    state["hardened"] = True

            def verify_file(self, descriptor: int) -> None:
                state = self._state(descriptor)
                with self._lock:
                    state["verify_calls"] = int(state["verify_calls"]) + 1
                    hardened = bool(state["hardened"])
                if not hardened:
                    raise OSError("ACL publication is not complete")

            @property
            def states(self) -> tuple[dict[str, object], ...]:
                with self._lock:
                    return tuple(self._states.values())

        security = CoordinatedWindowsSecurity()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "plugin"
            barrier = threading.Barrier(8)
            observed: list[tuple[bytes, ...]] = []
            errors: list[Exception] = []

            def load() -> None:
                try:
                    barrier.wait()
                    registry = PluginPrincipalRegistry.load_or_create(state)
                    observed.append(tuple(registry.token_path(p).read_bytes() for p in PRINCIPALS))
                except Exception as error:
                    errors.append(error)

            with (
                mock.patch.object(plugin_config.os, "name", "nt"),
                mock.patch.object(plugin_config, "_WINDOWS_FILE_SECURITY", security),
            ):
                threads = [threading.Thread(target=load) for _ in range(8)]
                for thread in threads:
                    thread.start()
                self.assertTrue(security.first_harden_started.wait(2))
                security.competing_harden.wait(0.2)
                security.release_first_harden.set()
                for thread in threads:
                    thread.join(5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(observed), 8)
            self.assertTrue(all(item == observed[0] for item in observed))
            self.assertEqual(len(security.states), len(PRINCIPALS))
            self.assertTrue(all(item["harden_calls"] == 1 for item in security.states))
            self.assertTrue(all(int(item["verify_calls"]) >= 1 for item in security.states))


if __name__ == "__main__":
    unittest.main()
