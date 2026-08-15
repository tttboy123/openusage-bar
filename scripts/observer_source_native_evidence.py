#!/usr/bin/env python3
"""Produce bounded, source-level Observer evidence on native hosted runners.

The probe deliberately publishes only capability facts. Synthetic credentials,
fixture payloads, account identifiers, and filesystem paths never enter the
evidence document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openusage_bar.codex_daily import CACHE_SCHEMA_VERSION, CodexLocalDailyImporter
from openusage_bar.codex_subscription import CodexSubscriptionAdapter
from openusage_bar.config import MoonshotConfig
from openusage_bar.keychain import default_keychain
from openusage_bar.moonshot import MoonshotBalanceAdapter, endpoint_for_site
from openusage_bar.providers.contracts import (
    BalanceFetchSuccess,
    QuotaFetchSuccess,
    UsageImportSuccess,
)


SCHEMA_VERSION = "observer-source-native-evidence/v1"
EVIDENCE_CLASS = "github_hosted_native_fixture"
RUNNER_ENVIRONMENT = "github-hosted"
MAX_EVIDENCE_BYTES = 16 * 1024
MOONSHOT_ACCOUNT = "observer-native-evidence-moonshot"


class EvidenceError(RuntimeError):
    """A sanitized evidence failure safe for logs and automation."""


def _source_record(
    source: str, *, verified: bool, failed_seam: str | None = None
) -> dict[str, object]:
    if verified and failed_seam is not None:
        raise EvidenceError("source_state_invalid")
    if source == "moonshot":
        if failed_seam not in {None, "credentialBackend", "factParser", "all"}:
            raise EvidenceError("source_state_invalid")
        credential_verified = verified or failed_seam == "factParser"
        parser_verified = verified or failed_seam in {None, "credentialBackend"}
        return {
            "familyId": "moonshot",
            "catalogSourceId": "moonshot_official_api",
            "runtimeSourceIds": ["moonshot.balance"],
            "status": "verified" if verified else "unverified",
            "reasonCode": (
                "all_required_seams_verified"
                if verified
                else "required_seam_unverified"
            ),
            "seams": {
                "executableDiscovery": "not_applicable",
                "localFileDiscovery": "not_applicable",
                "credentialBackend": (
                    "verified" if credential_verified else "not_verified"
                ),
                "factParser": "verified" if parser_verified else "not_verified",
            },
        }
    if source == "codex":
        if failed_seam not in {None, "localFileDiscovery", "factParser", "all"}:
            raise EvidenceError("source_state_invalid")
        local_file_verified = verified or failed_seam == "factParser"
        parser_verified = verified or failed_seam in {None, "localFileDiscovery"}
        return {
            "familyId": "codex",
            "catalogSourceId": "codex_local_log",
            "runtimeSourceIds": [
                "codex.local_rate_limits",
                "codex.local_sessions",
            ],
            "status": "verified" if verified else "unverified",
            "reasonCode": (
                "all_required_seams_verified"
                if verified
                else "required_seam_unverified"
            ),
            "seams": {
                "executableDiscovery": "not_applicable",
                "localFileDiscovery": (
                    "verified" if local_file_verified else "not_verified"
                ),
                "credentialBackend": "not_applicable",
                "factParser": "verified" if parser_verified else "not_verified",
            },
        }
    raise EvidenceError("source_invalid")


def _payload(
    platform: str, *, moonshot_verified: bool, codex_verified: bool
) -> dict[str, object]:
    sources = [
        _source_record("moonshot", verified=moonshot_verified),
        _source_record("codex", verified=codex_verified),
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "evidenceClass": EVIDENCE_CLASS,
        "platform": platform,
        "runnerEnvironment": RUNNER_ENVIRONMENT,
        "evaluatedSourceCount": 2,
        "verifiedSourceCount": sum(
            item["status"] == "verified" for item in sources
        ),
        "sources": sources,
    }


def _payload_from_sources(
    platform: str,
    moonshot: dict[str, object],
    codex: dict[str, object],
) -> dict[str, object]:
    sources = [moonshot, codex]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "evidenceClass": EVIDENCE_CLASS,
        "platform": platform,
        "runnerEnvironment": RUNNER_ENVIRONMENT,
        "evaluatedSourceCount": 2,
        "verifiedSourceCount": sum(
            item.get("status") == "verified" for item in sources
        ),
        "sources": sources,
    }


def _seam_schema(
    *, credential_backend: str, local_file_discovery: str, fact_parser: str
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "executableDiscovery",
            "localFileDiscovery",
            "credentialBackend",
            "factParser",
        ],
        "properties": {
            "executableDiscovery": {"const": "not_applicable"},
            "localFileDiscovery": {"const": local_file_discovery},
            "credentialBackend": {"const": credential_backend},
            "factParser": {"const": fact_parser},
        },
    }


def _source_schema(source: str, *, verified: bool) -> dict[str, object]:
    if verified:
        return _exact_source_schema(_source_record(source, verified=True))
    native_seam = (
        "credentialBackend" if source == "moonshot" else "localFileDiscovery"
    )
    return {
        "oneOf": [
            _exact_source_schema(
                _source_record(
                    source, verified=False, failed_seam=native_seam
                )
            ),
            _exact_source_schema(
                _source_record(
                    source, verified=False, failed_seam="factParser"
                )
            ),
            _exact_source_schema(
                _source_record(source, verified=False, failed_seam="all")
            ),
        ]
    }


def _exact_source_schema(record: dict[str, object]) -> dict[str, object]:
    seams = record["seams"]
    assert isinstance(seams, dict)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "familyId",
            "catalogSourceId",
            "runtimeSourceIds",
            "status",
            "reasonCode",
            "seams",
        ],
        "properties": {
            "familyId": {"const": record["familyId"]},
            "catalogSourceId": {"const": record["catalogSourceId"]},
            "runtimeSourceIds": {"const": record["runtimeSourceIds"]},
            "status": {"const": record["status"]},
            "reasonCode": {"const": record["reasonCode"]},
            "seams": _seam_schema(
                credential_backend=str(seams["credentialBackend"]),
                local_file_discovery=str(seams["localFileDiscovery"]),
                fact_parser=str(seams["factParser"]),
            ),
        },
    }


def _state_schema(
    *, moonshot_verified: bool, codex_verified: bool
) -> dict[str, object]:
    return {
        "type": "object",
        "required": ["verifiedSourceCount", "sources"],
        "properties": {
            "verifiedSourceCount": {
                "const": int(moonshot_verified) + int(codex_verified)
            },
            "sources": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "prefixItems": [
                    _source_schema("moonshot", verified=moonshot_verified),
                    _source_schema("codex", verified=codex_verified),
                ],
                "items": False,
            },
        },
    }


def render_schema() -> dict[str, object]:
    """Return the closed Draft 2020-12 evidence schema."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openusagebar.dev/schemas/observer-source-native-evidence-v1.schema.json",
        "title": "OpenUsage Bar Observer source native evidence v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schemaVersion",
            "evidenceClass",
            "platform",
            "runnerEnvironment",
            "evaluatedSourceCount",
            "verifiedSourceCount",
            "sources",
        ],
        "properties": {
            "schemaVersion": {"const": SCHEMA_VERSION},
            "evidenceClass": {"const": EVIDENCE_CLASS},
            "platform": {"enum": ["windows", "linux"]},
            "runnerEnvironment": {"const": RUNNER_ENVIRONMENT},
            "evaluatedSourceCount": {"const": 2},
            "verifiedSourceCount": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2,
            },
            "sources": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "oneOf": [
            _state_schema(moonshot_verified=True, codex_verified=True),
            _state_schema(moonshot_verified=True, codex_verified=False),
            _state_schema(moonshot_verified=False, codex_verified=True),
            _state_schema(moonshot_verified=False, codex_verified=False),
        ],
    }


def validate_evidence(payload: object) -> dict[str, object]:
    """Validate the exact source/state matrix without an optional dependency."""
    if not isinstance(payload, dict):
        raise EvidenceError("evidence_invalid")
    if set(payload) != {
        "schemaVersion",
        "evidenceClass",
        "platform",
        "runnerEnvironment",
        "evaluatedSourceCount",
        "verifiedSourceCount",
        "sources",
    }:
        raise EvidenceError("evidence_invalid")
    if (
        payload.get("schemaVersion") != SCHEMA_VERSION
        or payload.get("evidenceClass") != EVIDENCE_CLASS
        or payload.get("runnerEnvironment") != RUNNER_ENVIRONMENT
        or payload.get("platform") not in {"windows", "linux"}
        or type(payload.get("evaluatedSourceCount")) is not int
        or payload.get("evaluatedSourceCount") != 2
        or type(payload.get("verifiedSourceCount")) is not int
    ):
        raise EvidenceError("evidence_invalid")
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        raise EvidenceError("evidence_invalid")
    verified_count = 0
    for source, expected_name in zip(sources, ("moonshot", "codex")):
        if not isinstance(source, dict):
            raise EvidenceError("evidence_invalid")
        native_seam = (
            "credentialBackend"
            if expected_name == "moonshot"
            else "localFileDiscovery"
        )
        allowed = [
            _source_record(expected_name, verified=True),
            _source_record(
                expected_name, verified=False, failed_seam=native_seam
            ),
            _source_record(
                expected_name, verified=False, failed_seam="factParser"
            ),
            _source_record(expected_name, verified=False, failed_seam="all"),
        ]
        if source not in allowed:
            raise EvidenceError("evidence_invalid")
        verified_count += source.get("status") == "verified"
    if payload["verifiedSourceCount"] != verified_count:
        raise EvidenceError("evidence_invalid")
    return payload


def canonical_evidence_json(payload: object) -> str:
    validated = validate_evidence(payload)
    rendered = json.dumps(
        validated,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    if len(rendered.encode("ascii")) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("evidence_too_large")
    return rendered


class _MoonshotFixtureClient:
    def get_json(self, _endpoint: str, _headers: dict[str, str]):
        return {
            "code": 0,
            "data": {
                "available_balance": "12.34",
                "voucher_balance": "2",
                "cash_balance": "10.34",
            },
        }


class _MoonshotRequestVerifier:
    def __init__(self, delegate: object, secret: str) -> None:
        self.delegate = delegate
        self.secret = secret
        self.called = False

    def get_json(self, endpoint: str, headers: dict[str, str]):
        if (
            endpoint != endpoint_for_site("china")
            or headers != {"Authorization": f"Bearer {self.secret}"}
        ):
            raise ValueError("native fixture request invalid")
        self.called = True
        return self.delegate.get_json(endpoint, headers)


def _record_from_checks(
    source: str, *, native_verified: bool, parser_verified: bool
) -> dict[str, object]:
    if native_verified and parser_verified:
        return _source_record(source, verified=True)
    if native_verified:
        failed_seam = "factParser"
    elif parser_verified:
        failed_seam = (
            "credentialBackend"
            if source == "moonshot"
            else "localFileDiscovery"
        )
    else:
        failed_seam = "all"
    return _source_record(source, verified=False, failed_seam=failed_seam)


def _probe_moonshot_native(
    *, keychain: object, client: object, clock: Callable[[], datetime]
) -> dict[str, object]:
    """Exercise the native credential facade and the production fact parser."""
    try:
        existing = keychain.get(MOONSHOT_ACCOUNT)
    except Exception:
        return _source_record(
            "moonshot", verified=False, failed_seam="all"
        )
    if existing is not None:
        raise EvidenceError("credential_state_not_clean")

    canary_value = "oub-native-evidence-" + secrets.token_hex(16)
    attempted_write = False
    set_returned = False
    credential_verified = False
    parser_verified = False
    cleanup_reason: str | None = None
    try:
        attempted_write = True
        keychain.set(MOONSHOT_ACCOUNT, canary_value)
        set_returned = True
        credential_verified = keychain.get(MOONSHOT_ACCOUNT) == canary_value
        if credential_verified:
            verifying_client = _MoonshotRequestVerifier(client, canary_value)
            adapter = MoonshotBalanceAdapter(
                MoonshotConfig(
                    provider_id=MOONSHOT_ACCOUNT,
                    name="Moonshot native evidence",
                    site="china",
                ),
                keychain,
                verifying_client,
                clock,
            )
            adapter.fetch()
            parser_verified = (
                verifying_client.called
                and isinstance(
                    adapter.last_balance_result, BalanceFetchSuccess
                )
                and len(adapter.last_balance_result.observations) == 1
                and adapter.last_balance_result.observations[0].source_id
                == "moonshot.balance"
            )
    except Exception:
        parser_verified = False
    finally:
        if attempted_write:
            try:
                current = keychain.get(MOONSHOT_ACCOUNT)
            except Exception:
                cleanup_reason = "credential_cleanup_failed"
            else:
                if current == canary_value:
                    try:
                        keychain.delete(MOONSHOT_ACCOUNT)
                        if keychain.get(MOONSHOT_ACCOUNT) is not None:
                            cleanup_reason = "credential_cleanup_failed"
                    except Exception:
                        cleanup_reason = "credential_cleanup_failed"
                elif current is not None:
                    cleanup_reason = "credential_state_changed"
                elif set_returned:
                    cleanup_reason = "credential_state_changed"
    if cleanup_reason is not None:
        raise EvidenceError(cleanup_reason)
    return _record_from_checks(
        "moonshot",
        native_verified=credential_verified,
        parser_verified=parser_verified,
    )


def _assert_safe_path(path: Path) -> None:
    if path.is_symlink():
        raise EvidenceError("codex_state_not_clean")
    if path.exists() and not path.is_dir():
        raise EvidenceError("codex_state_not_clean")


def _mkdir_owned(path: Path, created: list[Path]) -> None:
    _assert_safe_path(path)
    if path.exists():
        return
    path.mkdir()
    created.append(path)


def _codex_fixture(now: datetime) -> str:
    observed = now.astimezone(timezone.utc)
    reset = observed + timedelta(hours=2)
    stamp = observed.isoformat().replace("+00:00", "Z")
    events = [
        {
            "timestamp": stamp,
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                },
                "rate_limits": {
                    "limit_id": "codex",
                    "limit_name": None,
                    "primary": {
                        "used_percent": 25,
                        "window_minutes": 300,
                        "resets_at": int(reset.timestamp()),
                    },
                    "secondary": None,
                    "credits": None,
                    "individual_limit": None,
                    "plan_type": "pro",
                    "rate_limit_reached_type": None,
                },
            },
        },
    ]
    return "".join(
        json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        for event in events
    )


def _file_identity(path: Path) -> tuple[int, int]:
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode):
        raise EvidenceError("codex_file_identity_invalid")
    return (details.st_dev, details.st_ino)


def _write_owned_text(path: Path, content: str) -> tuple[int, int]:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return _file_identity(path)


def _cache_identity_for_fixture(
    cache_path: Path, session_file: Path
) -> tuple[int, int]:
    identity = _file_identity(cache_path)
    try:
        encoded = cache_path.read_bytes()
        payload = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("codex_cache_invalid") from error
    expected_key = hashlib.sha256(session_file.name.encode("utf-8")).hexdigest()
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "sessions"}
        or payload.get("schemaVersion") != CACHE_SCHEMA_VERSION
        or not isinstance(sessions, list)
        or len(sessions) != 1
        or not isinstance(sessions[0], dict)
        or sessions[0].get("key") != expected_key
    ):
        raise EvidenceError("codex_cache_invalid")
    return identity


def _cleanup_codex_paths(
    *, owned_files: dict[Path, tuple[int, int]], created: list[Path]
) -> None:
    cleanup_failed = False
    for path, expected_identity in owned_files.items():
        try:
            if _file_identity(path) != expected_identity:
                cleanup_failed = True
            else:
                path.unlink()
        except (EvidenceError, OSError):
            cleanup_failed = True
    for path in reversed(created):
        try:
            path.rmdir()
        except OSError:
            cleanup_failed = True
    if cleanup_failed:
        raise EvidenceError("codex_cleanup_failed")


def _probe_codex_native(
    *, home: Path, clock: Callable[[], datetime]
) -> dict[str, object]:
    """Exercise both Codex local parsers against the default home shape."""
    home = Path(home)
    sessions = home / ".codex" / "sessions"
    archived_sessions = home / ".codex" / "archived_sessions"
    cache_path = (
        home
        / ".local"
        / "state"
        / "openusage-bar"
        / "codex-session-cache.json"
    )
    session_file = sessions / "observer-native-evidence.jsonl"

    if not home.is_dir() or home.is_symlink():
        raise EvidenceError("codex_state_not_clean")
    for path in (
        home / ".codex",
        home / ".local",
        home / ".local" / "state",
        home / ".local" / "state" / "openusage-bar",
    ):
        _assert_safe_path(path)
    if (
        sessions.exists()
        or sessions.is_symlink()
        or archived_sessions.exists()
        or archived_sessions.is_symlink()
        or cache_path.exists()
        or cache_path.is_symlink()
    ):
        raise EvidenceError("codex_state_not_clean")

    created: list[Path] = []
    owned_files: dict[Path, tuple[int, int]] = {}
    local_file_verified = False
    parser_verified = False
    try:
        _mkdir_owned(home / ".codex", created)
        _mkdir_owned(sessions, created)
        _mkdir_owned(home / ".local", created)
        _mkdir_owned(home / ".local" / "state", created)
        _mkdir_owned(home / ".local" / "state" / "openusage-bar", created)
        owned_files[session_file] = _write_owned_text(
            session_file, _codex_fixture(clock())
        )
        empty_cache = json.dumps(
            {"schemaVersion": CACHE_SCHEMA_VERSION, "sessions": []},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        owned_files[cache_path] = _write_owned_text(cache_path, empty_cache)
        local_file_verified = True

        quota_adapter = CodexSubscriptionAdapter(sessions, clock=clock)
        quota_adapter.fetch()
        quota_verified = (
            isinstance(quota_adapter.last_quota_result, QuotaFetchSuccess)
            and {
                observation.source_id
                for observation in quota_adapter.last_quota_result.observations
            }
            == {"codex.local_rate_limits"}
        )

        importer = CodexLocalDailyImporter(
            session_roots=(sessions,),
            cache_path=cache_path,
            clock=clock,
        )
        local_day: date = clock().astimezone(importer.local_timezone).date()
        usage_result = importer.fetch_usage(local_day, local_day)
        usage_verified = (
            isinstance(usage_result, UsageImportSuccess)
            and bool(usage_result.rows)
            and sum(row.total_tokens for row in usage_result.rows) == 120
            and importer.usage_source_id == "codex.local_sessions"
        )
        owned_files[cache_path] = _cache_identity_for_fixture(
            cache_path, session_file
        )
        parser_verified = quota_verified and usage_verified
    except EvidenceError:
        raise
    except Exception:
        parser_verified = False
    finally:
        _cleanup_codex_paths(
            owned_files=owned_files,
            created=created,
        )
    return _record_from_checks(
        "codex",
        native_verified=local_file_verified,
        parser_verified=parser_verified,
    )


def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        return "linux"
    raise EvidenceError("probe_environment_invalid")


def _probe_environment() -> str:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != RUNNER_ENVIRONMENT
    ):
        raise EvidenceError("probe_environment_invalid")
    return _platform()


def collect_native_evidence() -> dict[str, object]:
    """Collect evidence using only runner identity and platform-native seams."""
    platform = _probe_environment()
    clock = lambda: datetime.now(timezone.utc)
    try:
        keychain = default_keychain()
    except Exception:
        moonshot = _source_record(
            "moonshot", verified=False, failed_seam="all"
        )
    else:
        moonshot = _probe_moonshot_native(
            keychain=keychain,
            client=_MoonshotFixtureClient(),
            clock=clock,
        )
    codex = _probe_codex_native(home=Path.home(), clock=clock)
    payload = _payload_from_sources(platform, moonshot, codex)
    return validate_evidence(payload)


def _write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise EvidenceError("arguments_invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError("evidence_invalid")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise EvidenceError("evidence_invalid")


def verify_evidence_file(path: Path) -> dict[str, object]:
    descriptor: int | None = None
    try:
        path = Path(path)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_EVIDENCE_BYTES
        ):
            raise EvidenceError("evidence_invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size <= 0
            or opened.st_size > MAX_EVIDENCE_BYTES
        ):
            raise EvidenceError("evidence_invalid")
        chunks: list[bytes] = []
        remaining = MAX_EVIDENCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    except EvidenceError:
        raise
    except OSError as error:
        raise EvidenceError("evidence_invalid") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("evidence_invalid")
    try:
        rendered = encoded.decode("ascii")
        payload = json.loads(
            rendered,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except EvidenceError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise EvidenceError("evidence_invalid") from error
    if rendered != canonical_evidence_json(payload):
        raise EvidenceError("evidence_invalid")
    assert isinstance(payload, dict)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Generate closed Observer source evidence artifacts."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Run native source probes")
    probe.add_argument("--output", type=Path, required=True)
    schema = commands.add_parser("schema", help="Render the committed schema")
    schema.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="Verify canonical evidence")
    verify.add_argument("--evidence", type=Path, required=True)
    return parser


def _report_error(error: EvidenceError) -> int:
    reason = str(error)
    if not reason.isidentifier() or not reason.isascii():
        reason = "evidence_invalid"
    print(
        "observer_source_native_evidence_invalid " f"reason={reason}",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "schema":
            rendered = json.dumps(
                render_schema(), ensure_ascii=True, indent=2, sort_keys=True
            ) + "\n"
            _write_text(args.output, rendered)
            return 0
        if args.command == "verify":
            verify_evidence_file(args.evidence)
            return 0
        rendered = canonical_evidence_json(collect_native_evidence())
        _write_text(args.output, rendered)
    except EvidenceError as error:
        return _report_error(error)
    except Exception:
        return _report_error(EvidenceError("evidence_invalid"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
