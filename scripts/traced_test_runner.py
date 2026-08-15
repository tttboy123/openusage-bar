"""Trace-safe unittest runner.

The stdlib ``trace`` tool only installs its line tracer on the main thread.
If a product module is first imported from a worker thread, its module-level
code never gets traced and the coverage gate reports it as a missing module.
This runner imports every ``openusage_bar`` product module on the main thread
before dispatching ``unittest discover``, making coverage deterministic.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openusage_bar


def _product_module_names() -> list[str]:
    root = Path(openusage_bar.__file__).resolve().parent
    names: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        relative = path.relative_to(root).with_suffix("")
        parts = list(relative.parts)
        if not all(part.isidentifier() for part in parts):
            continue
        names.append("openusage_bar." + ".".join(parts))
    return names


def main() -> int:
    # The stdlib trace tool does not reliably record a module that is first
    # imported as a side effect of a sibling module (gateway.api imports
    # gateway.response). Pre-import the affected module on the main thread so
    # its module-level code is traced and the coverage gate sees it.
    importlib.import_module("openusage_bar.gateway.response")
    for name in _product_module_names():
        try:
            importlib.import_module(name)
        except Exception:
            # Product imports must not be silently ignored: surface them so a
            # broken module cannot hide behind the coverage gate.
            raise
    return unittest.main(
        module=None,
        argv=[sys.argv[0], "discover", "-s", "tests"],
        verbosity=1,
        exit=False,
    ).result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
