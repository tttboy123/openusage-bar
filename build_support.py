from __future__ import annotations

import importlib.util
from pathlib import Path


_ZLIB_COPY = "            self.copy_file(zlib.__file__, os.path.dirname(arcdir))\n"
_ZLIB_GUARD = (
    '            if getattr(zlib, "__file__", None):\n'
    "                self.copy_file(zlib.__file__, os.path.dirname(arcdir))\n"
)


def patch_static_zlib_source(source: str) -> str:
    if _ZLIB_GUARD in source:
        return source
    if _ZLIB_COPY not in source:
        raise RuntimeError("Unsupported py2app build_app.py zlib block")
    return source.replace(_ZLIB_COPY, _ZLIB_GUARD, 1)


def apply_py2app_static_zlib_patch() -> bool:
    spec = importlib.util.find_spec("py2app")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("py2app must be installed before building")
    path = Path(next(iter(spec.submodule_search_locations))) / "build_app.py"
    source = path.read_text(encoding="utf-8")
    patched = patch_static_zlib_source(source)
    if patched == source:
        return False
    path.write_text(patched, encoding="utf-8")
    return True
