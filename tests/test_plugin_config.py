from __future__ import annotations

import os
import stat
import tempfile
import unittest
import threading
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
