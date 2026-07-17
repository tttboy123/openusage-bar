#!/usr/bin/env python3
"""Fail closed when selected runtime artifacts contain credential-shaped data."""

from __future__ import annotations

import re
import sqlite3
import sys
from enum import Enum
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openusage_bar.activity_store import _EXPECTED_SCHEMA


FORBIDDEN_NAME = (
    r"api[_ -]?key|authorization|bearer|cookie|password|prompt|response|secret|"
    r"access[_ -]?token|refresh[_ -]?token|oasis[_ -]?token"
)
FORBIDDEN_FIELDS = re.compile(
    rf"(?:"
    rf"(?<![A-Za-z0-9_])(?:{FORBIDDEN_NAME})(?![A-Za-z0-9_])[\"']?\s*[:=]|"
    rf"<key>\s*(?:{FORBIDDEN_NAME})\s*</key>|"
    rf"(?<![A-Za-z0-9_])(?:{FORBIDDEN_NAME})(?![A-Za-z0-9_])\s+"
    rf"(?:TEXT|BLOB|VARCHAR|CHAR)\b"
    rf")",
    re.IGNORECASE,
)
CREDENTIAL_OR_EMAIL = re.compile(
    r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"(?:Oasis|access|refresh)[_-]?[Tt]oken[=:]\s*[A-Za-z0-9._~+/=-]{16,}|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)
MAX_BYTES = 16 * 1024 * 1024
MAX_SQLITE_ROWS_PER_TABLE = 250_000
MAX_SQLITE_TEXT_BYTES = 16 * 1024 * 1024
MAX_SQLITE_CELL_BYTES = 64 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"


class ScanResult(Enum):
    SAFE = "safe"
    SQLITE_SAFE = "sqlite_safe"
    INVALID = "invalid"


def _contains_forbidden(text: str) -> bool:
    return bool(FORBIDDEN_FIELDS.search(text) or CREDENTIAL_OR_EMAIL.search(text))


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def scan_sqlite(path: Path) -> ScanResult:
    connection: sqlite3.Connection | None = None
    total_text_bytes = 0
    try:
        uri = f"file:{quote(str(path.absolute()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            return ScanResult.INVALID

        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        actual_tables = {
            name
            for object_type, name, _table_name, _sql in schema_rows
            if object_type == "table" and not name.startswith("sqlite_")
        }
        if actual_tables != set(_EXPECTED_SCHEMA):
            return ScanResult.INVALID

        for schema_row in schema_rows:
            for value in schema_row:
                if isinstance(value, str) and _contains_forbidden(value):
                    return ScanResult.INVALID

        for table_name, expected_columns in _EXPECTED_SCHEMA.items():
            actual_columns = tuple(
                (name, column_type, not_null, default, primary_key)
                for (
                    _column_id,
                    name,
                    column_type,
                    not_null,
                    default,
                    primary_key,
                ) in connection.execute(
                    f"PRAGMA table_info({_identifier(table_name)})"
                )
            )
            if actual_columns != expected_columns:
                return ScanResult.INVALID

            text_columns = [
                name for name, column_type, *_rest in expected_columns
                if column_type.upper() == "TEXT"
            ]
            if not text_columns:
                continue
            selected = ",".join(_identifier(name) for name in text_columns)
            rows = connection.execute(
                f"SELECT {selected} FROM {_identifier(table_name)} "
                f"LIMIT {MAX_SQLITE_ROWS_PER_TABLE + 1}"
            )
            row_count = 0
            for row in rows:
                row_count += 1
                if row_count > MAX_SQLITE_ROWS_PER_TABLE:
                    return ScanResult.INVALID
                for value in row:
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        return ScanResult.INVALID
                    value_bytes = len(value.encode("utf-8"))
                    if value_bytes > MAX_SQLITE_CELL_BYTES:
                        return ScanResult.INVALID
                    total_text_bytes += value_bytes
                    if total_text_bytes > MAX_SQLITE_TEXT_BYTES:
                        return ScanResult.INVALID
                    if _contains_forbidden(value):
                        return ScanResult.INVALID
        return ScanResult.SQLITE_SAFE
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        return ScanResult.INVALID
    finally:
        if connection is not None:
            connection.close()


def scan_file(path: Path) -> ScanResult:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return ScanResult.INVALID
        with path.open("rb") as stream:
            header = stream.read(len(SQLITE_HEADER))
        if header == SQLITE_HEADER:
            return scan_sqlite(path)
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeError):
        return ScanResult.INVALID
    if _contains_forbidden(text):
        return ScanResult.INVALID
    return ScanResult.SAFE


def expand(arguments: list[str]) -> list[Path] | None:
    expanded: list[Path] = []
    for value in arguments:
        path = Path(value)
        if path.is_symlink() or not path.exists():
            return None
        if path.is_dir():
            children = sorted(path.rglob("*"))
            if any(child.is_symlink() for child in children):
                return None
            expanded.extend(child for child in children if child.is_file())
        else:
            expanded.append(path)
    return expanded


def main(arguments: list[str]) -> int:
    paths = expand(arguments)
    if not paths:
        print("privacy_scan_forbidden_material", file=sys.stderr)
        return 1
    results = [scan_file(path) for path in paths]
    if ScanResult.INVALID in results:
        print("privacy_scan_forbidden_material", file=sys.stderr)
        return 1
    safe_files = results.count(ScanResult.SAFE)
    sqlite_files = results.count(ScanResult.SQLITE_SAFE)
    suffix = f" sqlite_files={sqlite_files}" if sqlite_files else ""
    print(f"privacy_scan_matches=0 files={safe_files}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
