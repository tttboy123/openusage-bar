#!/usr/bin/env python3
"""Export an explicit, aggregate-only OpenUsage Bar canary diagnostic."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import socket
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
API_SCHEMA = "1.0"
DIAGNOSTIC_SCHEMA = "openusage-diagnostics-1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
CAPABILITY_STATES = frozenset({"supported", "unsupported", "unknown"})
CAPABILITY_FIELDS = (
    "tokenHistory", "modelBreakdown", "resetTimestamps", "billing", "credits",
    "balance", "cost", "rateLimits", "serviceStatus",
)
FORBIDDEN_KEYS = frozenset({
    "accountref", "displayname", "credentialsource", "sourceid", "payloadjson",
    "apikey", "secret", "cookie", "prompt", "response", "rawpayload",
})
FORBIDDEN_INPUT_KEYS = frozenset({
    "apikey", "api_key", "secret", "token", "password", "cookie", "prompt",
    "response", "payloadjson", "rawpayload", "changejson",
})
SECRET_TEXT = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _items(value: Any, name: str, *, limit: int = 10_000) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{name} must be a bounded array")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return value


def _identifier_list(value: Any, name: str) -> list[str]:
    result = [_identifier(item, name) for item in _items(value, name, limit=100)]
    return sorted(set(result))


def _counted(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _capability_declarations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _items(payload.get("providers"), "capability providers", limit=1_000):
        provider = _mapping(raw, "capability provider")
        family_id = _identifier(provider.get("familyId"), "familyId")
        if family_id in seen:
            raise ValueError("duplicate capability family")
        seen.add(family_id)
        raw_capabilities = _mapping(provider.get("capabilities"), "capabilities")
        quota = _mapping(raw_capabilities.get("quotaWindows"), "quotaWindows")
        quota_state = quota.get("state")
        if quota_state not in CAPABILITY_STATES:
            raise ValueError("invalid quota capability state")
        states: dict[str, str] = {}
        for field in CAPABILITY_FIELDS:
            state = raw_capabilities.get(field)
            if state not in CAPABILITY_STATES:
                raise ValueError("invalid capability state")
            states[field] = state
        sources: list[dict[str, str]] = []
        for raw_source in _items(provider.get("sources"), "capability sources", limit=100):
            source = _mapping(raw_source, "capability source")
            sources.append({
                "kind": _identifier(source.get("kind"), "source kind"),
                "provenance": _identifier(source.get("provenance"), "source provenance"),
                "stability": _identifier(source.get("stability"), "source stability"),
            })
        supports_accounts = provider.get("supportsAccounts")
        if not isinstance(supports_accounts, bool):
            raise ValueError("supportsAccounts must be boolean")
        declarations.append({
            "capabilities": {
                "quotaWindows": {
                    "state": quota_state,
                    "values": _identifier_list(quota.get("values"), "quota windows"),
                },
                **states,
            },
            "familyId": family_id,
            "metricFamilies": _identifier_list(
                provider.get("metricFamilies"), "metric families"
            ),
            "regions": _identifier_list(provider.get("regions"), "regions"),
            "sources": sorted(
                sources, key=lambda item: (item["kind"], item["stability"], item["provenance"])
            ),
            "supportsAccounts": supports_accounts,
        })
    return sorted(declarations, key=lambda item: item["familyId"])


def _validate_export(value: Any, *, home: Path | None = None) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.casefold() in FORBIDDEN_KEYS:
                raise ValueError("diagnostic contains a forbidden field")
            _validate_export(nested, home=home)
    elif isinstance(value, list):
        for nested in value:
            _validate_export(nested, home=home)
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in SECRET_TEXT):
            raise ValueError("diagnostic contains secret-like text")
        if value.startswith(("/Users/", "/home/")) or (
            home is not None and str(home) in value
        ):
            raise ValueError("diagnostic contains an absolute home path")


def _reject_sensitive_input(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_INPUT_KEYS:
                raise ValueError("Local API response contains a forbidden field")
            _reject_sensitive_input(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_input(nested)


def build_diagnostics(
    snapshot: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    product: dict[str, str],
    runtime: dict[str, str],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    snapshot = _mapping(snapshot, "snapshot")
    capabilities = _mapping(capabilities, "capabilities")
    _reject_sensitive_input(snapshot)
    _reject_sensitive_input(capabilities)
    if snapshot.get("schemaVersion") != API_SCHEMA or capabilities.get("schemaVersion") != API_SCHEMA:
        raise ValueError("unsupported Local API schema")
    revision = _integer(snapshot.get("dataRevision"), "snapshot revision")
    if _integer(capabilities.get("dataRevision"), "capability revision") != revision:
        raise ValueError("Local API revision changed during export")
    version = product.get("version")
    build = product.get("build")
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ValueError("invalid product version")
    if not isinstance(build, str) or not build.isascii() or not build.isdecimal():
        raise ValueError("invalid product build")
    macos = runtime.get("macOS")
    architecture = runtime.get("architecture")
    if not isinstance(macos, str) or len(macos) > 32 or not macos:
        raise ValueError("invalid macOS version")
    if architecture not in {"arm64", "x86_64", "unknown"}:
        raise ValueError("invalid architecture")

    summary = _mapping(snapshot.get("summary"), "summary")
    providers = _items(snapshot.get("providers"), "providers")
    quotas = [_mapping(item, "quota window") for item in _items(snapshot.get("quotaWindows"), "quota windows")]
    sources = [_mapping(item, "source") for item in _items(snapshot.get("sources"), "sources")]
    source_states = [_identifier(item.get("state"), "source state") for item in sources]
    errors = []
    for source in sources:
        error = source.get("errorCode")
        if error is None:
            continue
        errors.append(error if isinstance(error, str) and ERROR_CODE.fullmatch(error) else "UNCLASSIFIED")
    quota_states = [_identifier(item.get("state"), "quota state") for item in quotas]
    quota_quality = [_identifier(item.get("quality"), "quota quality") for item in quotas]
    stale_count = sum(item.get("stale") is True for item in quotas)
    if any(not isinstance(item.get("stale"), bool) for item in quotas):
        raise ValueError("quota stale state must be boolean")
    current = (clock or (lambda: datetime.now(timezone.utc)))()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    result = {
        "aggregates": {
            "coveredDayCount": _integer(summary.get("coveredDayCount"), "covered days"),
            "modelCount": _integer(summary.get("modelCount"), "model count"),
            "providerInstanceCount": len(providers),
            "quotaQuality": _counted(quota_quality),
            "quotaStates": _counted(quota_states),
            "quotaWindowCount": len(quotas),
            "staleQuotaWindowCount": stale_count,
            "sourceCount": len(sources),
            "sourceErrorCodes": _counted(errors),
            "sourceStates": _counted(source_states),
            "todayTokens": _integer(summary.get("todayTokens"), "today tokens"),
        },
        "capabilityDeclarations": _capability_declarations(capabilities),
        "exportedAt": current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "localAPI": {
            "catalogRevision": _identifier(snapshot.get("catalogRevision"), "catalog revision"),
            "dataRevision": revision,
            "schemaVersion": API_SCHEMA,
        },
        "product": {"build": build, "version": version},
        "runtime": {"architecture": architecture, "macOS": macos},
        "schemaVersion": DIAGNOSTIC_SCHEMA,
    }
    _validate_export(result, home=Path.home())
    return result


def _get(socket_path: Path, route: str) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(str(socket_path))
        client.sendall(
            f"GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        chunks: list[bytes] = []
        size = 0
        while chunk := client.recv(65_536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Local API response is too large")
            chunks.append(chunk)
    finally:
        client.close()
    response = b"".join(chunks)
    head, separator, body = response.partition(b"\r\n\r\n")
    if not separator or not head.startswith(b"HTTP/1.1 200 "):
        raise ValueError("Local API route is unavailable")
    payload = json.loads(body)
    return _mapping(payload, "Local API response")


def _product(app: Path) -> dict[str, str]:
    info = app / "Contents/Info.plist"
    try:
        with info.open("rb") as handle:
            payload = plistlib.load(handle)
        return {
            "version": str(payload["CFBundleShortVersionString"]),
            "build": str(payload["CFBundleVersion"]),
        }
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException) as error:
        raise ValueError("installed app metadata is unavailable") from error


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("output must be an absolute path in an existing directory")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Export redacted OpenUsage Bar diagnostics.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--socket", type=Path,
        default=Path.home() / ".local/state/openusage-bar/openusage.sock",
    )
    parser.add_argument("--app", type=Path, default=Path("/Applications/OpenUsage Bar.app"))
    arguments = parser.parse_args()
    if not arguments.socket.is_absolute() or "\x00" in str(arguments.socket):
        parser.error("socket path must be absolute")
    try:
        diagnostic = None
        for _ in range(2):
            app = arguments.app
            default_app = Path("/Applications/OpenUsage Bar.app")
            home_app = Path.home() / "Applications/OpenUsage Bar.app"
            if app == default_app and not app.is_dir() and home_app.is_dir():
                app = home_app
            snapshot = _get(arguments.socket, "/v1/snapshot")
            capabilities = _get(arguments.socket, "/v1/capabilities")
            try:
                diagnostic = build_diagnostics(
                    snapshot,
                    capabilities,
                    product=_product(app),
                    runtime={
                        "macOS": platform.mac_ver()[0] or "unknown",
                        "architecture": platform.machine() if platform.machine() in {"arm64", "x86_64"} else "unknown",
                    },
                )
                break
            except ValueError as error:
                if "revision changed" not in str(error):
                    raise
        if diagnostic is None:
            raise ValueError("Local API revision did not stabilize")
        _write_private(arguments.output, diagnostic)
    except (OSError, ValueError, json.JSONDecodeError):
        print("diagnostics export unavailable", file=os.sys.stderr)
        return 1
    print("diagnostics_exported=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
