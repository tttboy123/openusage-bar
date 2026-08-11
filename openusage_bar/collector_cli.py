from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, TextIO

from .activity_store import ActivityStore, SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from .bounded_process import run_bounded
from .openusage_adapter import child_subprocess_environment
from .openusage_catalog import EXPECTED_PROVIDER_IDS
from .performance_timing import RefreshTimingRecorder, write_timing_report
from .query import QueryService, SCHEMA_VERSION, to_wire


DEFAULT_LEDGER_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "activity.sqlite3"
DEFAULT_API_SOCKET_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "openusage.sock"
DEFAULT_API_TOKEN_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "api.token"
DEFAULT_GATEWAY_CONFIG_PATH = Path.home() / ".config" / "openusage-bar" / "gateway.json"
DEFAULT_GATEWAY_TOKEN_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "gateway.token"
DEFAULT_GATEWAY_CACHE_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "gateway-cache.sqlite3"
DEFAULT_GATEWAY_TELEMETRY_PATH = (
    Path.home()
    / ".local"
    / "state"
    / "openusage-bar"
    / "gateway-telemetry.sqlite3"
)
DEFAULT_API_TCP_PORT = 17821
# An interactive attempt may legitimately use OpenUsage's bounded auto -> direct
# fallback (12s + 75s) followed by a bounded daily-history import (60s). One
# hundred sixty seconds avoids killing that slow path, but remains a hard limit;
# it is not a completion guarantee for an arbitrary number of configured sources.
DEFAULT_FRESH_TIMEOUT_SECONDS = 160
MIN_DAEMON_INTERVAL_SECONDS = 60
INTERNAL_REFRESH_COMMAND = "__refresh-once"
INTERNAL_GATEWAY_SELF_TEST_COMMAND = "__gateway-self-test"
INTERNAL_PLUGIN_SELF_TEST_COMMAND = "__plugin-self-test"


class CLIError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message)


class UnavailableRefresher:
    def refresh(self) -> None:
        raise RuntimeError("refresh unavailable")


class OfflineRefresher:
    def refresh(self) -> None:
        return None


def build_default_refresher(
    store: ActivityStore,
    *,
    timing_recorder: RefreshTimingRecorder | None = None,
) -> Any:
    """Build credential-owning adapters lazily, outside all import-time paths."""
    from .aggregator import build_headless_refresher

    return build_headless_refresher(
        store, timing_recorder=timing_recorder
    )


def _internal_refresh_once(
    argv: list[str],
    *,
    stderr: TextIO,
    refresher_factory: Callable[[ActivityStore], Any] | None,
) -> int:
    valid_shape = (
        len(argv) == 3
        or (
            len(argv) == 5
            and argv[3] == "--performance-output"
        )
    )
    if (
        not valid_shape
        or argv[0] != INTERNAL_REFRESH_COMMAND
        or argv[1] != "--ledger"
    ):
        stderr.write("invalid command input\n")
        return 2
    timing_path = Path(argv[4]) if len(argv) == 5 else None
    if timing_path is not None and (
        not timing_path.is_absolute()
        or not timing_path.parent.is_dir()
        or timing_path.exists()
    ):
        stderr.write("invalid command input\n")
        return 2
    store: ActivityStore | None = None
    try:
        store = ActivityStore(Path(argv[2]))
        factory = refresher_factory or build_default_refresher
        timing_recorder = (
            RefreshTimingRecorder() if timing_path is not None else None
        )
        if refresher_factory is None:
            refresher = factory(
                store, timing_recorder=timing_recorder
            )
        else:
            refresher = factory(store)
        refresher.refresh()
        if timing_path is not None:
            snapshot = (
                refresher.performance_timing_snapshot()
                if hasattr(refresher, "performance_timing_snapshot")
                else timing_recorder.snapshot()
            )
            write_timing_report(timing_path, snapshot)
        return 0
    except Exception:
        stderr.write("refresh unavailable\n")
        return 1
    finally:
        if store is not None:
            store.close()


def _gateway_self_test_unavailable_report() -> dict[str, object]:
    return {
        "schemaVersion": "gateway-self-test/v1",
        "object": "gateway.self_test",
        "ok": False,
        "checks": {
            "observe": {
                "ok": False,
                "observer": "unavailable",
                "gateway": "disabled",
                "credentialReads": 0,
                "providerCalls": 0,
            },
            "advise": {
                "ok": False,
                "decision": "defer",
                "reason": "self_test_failed",
                "credentialReads": 0,
                "providerCalls": 0,
            },
            "gateway": {
                "ok": False,
                "status": "failed",
                "credentialReads": 0,
                "providerCalls": 0,
            },
            "credentialFailure": {
                "ok": False,
                "errorCode": "self_test_failed",
                "retryable": False,
                "providerCalls": 0,
                "observer": "unavailable",
            },
        },
    }


def _internal_gateway_self_test(
    argv: list[str],
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if argv != [INTERNAL_GATEWAY_SELF_TEST_COMMAND, "--format", "json"]:
        stderr.write("invalid command input\n")
        return 2
    try:
        from .gateway import self_test as gateway_self_test

        report = gateway_self_test.run_gateway_self_test()
        if not gateway_self_test.is_gateway_self_test_report(report):
            report = _gateway_self_test_unavailable_report()
    except Exception:
        report = _gateway_self_test_unavailable_report()
    _write_json(stdout, report)
    return 0 if report.get("ok") is True else 1


def _internal_plugin_self_test(
    argv: list[str], *, stdout: TextIO, stderr: TextIO,
) -> int:
    if argv != [INTERNAL_PLUGIN_SELF_TEST_COMMAND, "--format", "json"]:
        stderr.write("invalid command input\n")
        return 2
    checks = {
        "principalIsolation": False,
        "samePayloadReplay": False,
        "differentPayloadConflict": False,
        "restartReplay": False,
    }
    try:
        from .plugin.config import PluginPrincipalRegistry
        from .plugin.store import IdempotencyConflict, PluginStore

        with tempfile.TemporaryDirectory(prefix="openusage-plugin-self-test-") as temporary:
            registry = PluginPrincipalRegistry.load_or_create(
                Path(temporary) / "tokens"
            )
            token_values = {
                principal: registry.token_path(principal)
                .read_text(encoding="ascii")
                .strip()
                for principal in ("loom", "codex", "claude_code", "desktop")
            }
            tokens_isolated = (
                len(set(token_values.values())) == 4
                and all(
                    registry.authenticate(token) == principal
                    for principal, token in token_values.items()
                )
            )
            path = Path(temporary) / "plugin.sqlite3"
            key = "idem_0123456789abcdef0123456789abcdef"
            projection = {"synthetic": True}
            calls = 0

            def operation() -> tuple[int, dict[str, object]]:
                nonlocal calls
                calls += 1
                return 200, {"syntheticReceipt": "loom"}

            first_store = PluginStore(path)
            first = first_store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=key,
                projection=projection, operation=operation,
            )
            second = first_store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=key,
                projection=projection, operation=operation,
            )
            checks["samePayloadReplay"] = first == second and calls == 1
            try:
                first_store.execute_idempotent(
                    principal="loom", route="/plugin/v1/route-advice", key=key,
                    projection={"synthetic": False}, operation=operation,
                )
            except IdempotencyConflict:
                checks["differentPayloadConflict"] = True
            codex = first_store.execute_idempotent(
                principal="codex", route="/plugin/v1/route-advice", key=key,
                projection=projection,
                operation=lambda: (200, {"syntheticReceipt": "codex"}),
            )
            checks["principalIsolation"] = tokens_isolated and codex != first
            first_store.close()
            reopened = PluginStore(path)
            replay = reopened.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=key,
                projection=projection, operation=operation,
            )
            reopened.close()
            checks["restartReplay"] = replay == first and calls == 1
    except Exception:
        pass
    ok = all(checks.values())
    _write_json(stdout, {
        "apiVersion": "plugin-self-test/v1",
        "object": "plugin.server_self_test",
        "ok": ok,
        "synthetic": True,
        "checks": checks,
    })
    return 0 if ok else 1


def _parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(prog="openusage-bar")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--strict", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(name: str, formats: tuple[str, ...]) -> argparse.ArgumentParser:
        child = commands.add_parser(name)
        child.add_argument("--format", choices=formats, required=True)
        child.add_argument("--offline", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--fresh", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--strict", action="store_true", default=argparse.SUPPRESS)
        return child

    common("status", ("json",))
    snapshot = common("snapshot", ("json",))
    snapshot.add_argument("--today")
    usage = common("usage", ("json", "jsonl"))
    usage.add_argument("--from", dest="from_day", required=True)
    usage.add_argument("--to", dest="to_day", required=True)
    costs = common("costs", ("json", "jsonl"))
    costs.add_argument("--from", dest="from_day", required=True)
    costs.add_argument("--to", dest="to_day", required=True)
    quotas = common("quotas", ("json",))
    quotas.add_argument("--limit", type=int)
    common("sources", ("json",))
    common("providers", ("json",))
    changes = common("changes", ("json", "jsonl"))
    changes.add_argument("--after", type=int, required=True)
    changes.add_argument("--limit", type=int, default=100)
    common("doctor", ("json",))
    daemon = commands.add_parser("daemon")
    daemon.add_argument("--interval", required=True)
    daemon.add_argument("--api-socket", default=str(DEFAULT_API_SOCKET_PATH))
    daemon.add_argument(
        "--api-transport",
        choices=("auto", "unix", "tcp"),
        default="auto",
    )
    daemon.add_argument("--api-port", type=int, default=0)
    daemon.add_argument("--api-token-path")
    service = commands.add_parser("service")
    service.add_argument(
        "action",
        choices=("install", "uninstall", "print"),
    )
    service.add_argument("--interval", default="300")
    service.add_argument("--command", dest="service_command")
    desktop_service = commands.add_parser("desktop-service")
    desktop_actions = desktop_service.add_subparsers(
        dest="desktop_service_action",
        required=True,
    )
    desktop_install = desktop_actions.add_parser("install")
    desktop_install.add_argument("--interval", choices=("300",), required=True)
    desktop_actions.add_parser("uninstall")
    state = commands.add_parser("state")
    state.add_argument("state_action", choices=("delete",))
    state.add_argument("--confirm", dest="state_confirmation", required=True)
    state.add_argument("--format", choices=("json",), required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--format", choices=("json",), required=True)
    reconcile.add_argument("--from", dest="from_day", required=True)
    reconcile.add_argument("--to", dest="to_day", required=True)
    executor = commands.add_parser("executor")
    executor.add_argument(
        "action",
        choices=("list", "state", "verify", "switch"),
    )
    executor.add_argument("--plugin", choices=("cc_switch", "omniroute"))
    executor.add_argument("--target")
    executor.add_argument("--auto-apply", action="store_true")
    executor.add_argument("--format", choices=("json",), required=True)
    dashboard = commands.add_parser("dashboard")
    dashboard.add_argument("--port", type=int, default=0)
    connect = commands.add_parser("connect")
    connect.add_argument("--family", required=True)
    connect.add_argument("--format", choices=("json",), required=True)
    gateway = commands.add_parser("gateway")
    gateway_commands = gateway.add_subparsers(
        dest="gateway_action",
        required=True,
    )
    gateway_commands.add_parser("print-config")
    gateway_status = gateway_commands.add_parser("status")
    gateway_status.add_argument(
        "--config",
        default=str(DEFAULT_GATEWAY_CONFIG_PATH),
    )
    gateway_start = gateway_commands.add_parser("start")
    gateway_start.add_argument(
        "--config",
        default=str(DEFAULT_GATEWAY_CONFIG_PATH),
    )
    gateway_start.add_argument(
        "--token-path",
    )
    plugin = commands.add_parser("plugin")
    plugin_commands = plugin.add_subparsers(
        dest="plugin_action", required=True,
    )
    plugin_commands.add_parser("status")
    plugin_start = plugin_commands.add_parser("start")
    plugin_start.add_argument("--state-dir")
    for action in ("install-service", "uninstall-service", "print-service"):
        plugin_commands.add_parser(action)
    return parser


def _day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise CLIError("invalid date") from error
    if parsed.isoformat() != value:
        raise CLIError("invalid date")
    return parsed


def _interval(value: str) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError) as error:
        raise CLIError("invalid interval") from error
    if str(interval) != value or interval < MIN_DAEMON_INTERVAL_SECONDS:
        raise CLIError("invalid interval")
    return interval


def _api_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise CLIError("invalid api port")
    return value


def _resolve_api_transport(
    transport: str,
    api_port: int,
) -> tuple[str, int]:
    resolved = transport
    if resolved == "auto":
        resolved = "tcp" if sys.platform == "win32" else "unix"
    port = api_port
    if resolved == "tcp" and port == 0 and sys.platform == "win32":
        port = DEFAULT_API_TCP_PORT
    return resolved, port


def _default_api_token_path() -> str:
    """Resolve the manual TCP token path from the live runtime environment."""

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise CLIError("missing local application data directory")
        return str(
            PureWindowsPath(local_app_data) / "openusage-bar" / "api.token"
        )
    return str(
        Path.home() / ".local" / "state" / "openusage-bar" / "api.token"
    )


def _default_gateway_token_path() -> str:
    """Resolve the Gateway token path from the live runtime environment."""

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise CLIError("missing local application data directory")
        return str(
            PureWindowsPath(local_app_data) / "openusage-bar" / "gateway.token"
        )
    return str(DEFAULT_GATEWAY_TOKEN_PATH)


def _runtime_descriptor() -> Any:
    from .runtime_descriptor import RuntimeDescriptor

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise CLIError("missing local application data directory")
        state_dir: os.PathLike[str] = PureWindowsPath(local_app_data) / "openusage-bar"
    else:
        state_dir = Path.home() / ".local" / "state" / "openusage-bar"
    return RuntimeDescriptor.for_platform(sys.platform, state_dir=state_dir)


def _fresh_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
        raise CLIError("invalid refresh timeout")
    return value


def _compact(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(stdout: TextIO, payload: Any) -> None:
    stdout.write(_compact(payload) + "\n")


def _write_jsonl(stdout: TextIO, rows: list[dict[str, Any]], checkpoint: dict[str, Any]) -> None:
    for row in rows:
        _write_json(stdout, row)
    _write_json(stdout, checkpoint)


@dataclass(frozen=True)
class RefreshOutcome:
    succeeded: bool
    worker_alive: bool
    thread: threading.Thread
    finished: threading.Event


def _default_refresh_command(
    ledger_path: str,
    entrypoint: Path | None,
) -> list[str]:
    if entrypoint is None and getattr(sys, "frozen", False):
        # PyInstaller resource collectors are already the trusted executable
        # that must service the bounded internal refresh.  Unlike py2app they
        # do not have an outer EXECUTABLEPATH launcher; `_MEIPASS` is the
        # documented runtime marker and `sys.executable` is the bootloader
        # path on macOS, Windows, and Linux.
        raw_pyinstaller_root = getattr(sys, "_MEIPASS", None)
        if raw_pyinstaller_root is not None:
            raw_self = sys.executable
            if (
                not isinstance(raw_self, str)
                or not isinstance(raw_pyinstaller_root, str)
                or not raw_self
                or not raw_pyinstaller_root
                or "\x00" in raw_self
                or "\x00" in raw_pyinstaller_root
                or not os.path.isabs(raw_self)
                or not os.path.isabs(raw_pyinstaller_root)
            ):
                raise CLIError("invalid frozen runtime")
            executable = Path(os.path.realpath(raw_self))
            runtime_root = Path(os.path.realpath(raw_pyinstaller_root))
            if (
                not executable.is_absolute()
                or not runtime_root.is_absolute()
                or not executable.name
            ):
                raise CLIError("invalid frozen runtime")
            return [
                str(executable),
                INTERNAL_REFRESH_COMMAND,
                "--ledger",
                ledger_path,
            ]

        # The legacy native macOS helper remains a py2app bundle.  It has a
        # separate launcher and embedded interpreter, so retain its stricter
        # bundle relationship checks instead of treating every frozen runtime
        # as a self-executable.
        raw_executable = os.environ.get("EXECUTABLEPATH", "")
        raw_resources = os.environ.get("RESOURCEPATH", "")
        raw_interpreter = sys.executable
        if (
            not raw_executable
            or not raw_resources
            or not raw_interpreter
            or "\x00" in raw_executable
            or "\x00" in raw_resources
            or "\x00" in raw_interpreter
        ):
            raise CLIError("invalid frozen runtime")
        executable = Path(os.path.realpath(raw_executable))
        resources = Path(os.path.realpath(raw_resources))
        interpreter = Path(os.path.realpath(raw_interpreter))
        contents = interpreter.parent.parent
        expected_macos = contents / "MacOS"
        expected_resources = contents / "Resources"
        if (
            not executable.is_absolute()
            or not resources.is_absolute()
            or not interpreter.is_absolute()
            or contents.name != "Contents"
            or interpreter.parent.name != "MacOS"
            or resources != expected_resources
            or executable.parent != expected_macos
            or not executable.name
        ):
            raise CLIError("invalid frozen runtime")
        return [
            str(executable),
            INTERNAL_REFRESH_COMMAND,
            "--ledger",
            ledger_path,
        ]
    target = entrypoint or Path(__file__).resolve().parent.parent / "openusage_collector.py"
    return [
        sys.executable,
        str(target),
        INTERNAL_REFRESH_COMMAND,
        "--ledger",
        ledger_path,
    ]


def _refresh_in_subprocess(
    ledger_path: str,
    *,
    timeout: int,
    stderr: TextIO,
    runner: Callable[..., Any],
    entrypoint: Path | None,
    environment: dict[str, str] | None,
) -> bool:
    try:
        command = _default_refresh_command(ledger_path, entrypoint)
        completed = runner(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            env=child_subprocess_environment(environment),
        )
        succeeded = completed.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        succeeded = False
    except Exception:
        succeeded = False
    if not succeeded:
        stderr.write("refresh unavailable; using last-good ledger data\n")
    return succeeded


def _refresh_once(
    refresher: Any,
    *,
    timeout: int,
    stderr: TextIO,
    thread_joiner: Callable[[threading.Thread, int], bool] | None = None,
) -> RefreshOutcome:
    failure = threading.Event()
    finished = threading.Event()

    def worker() -> None:
        try:
            refresher.refresh()
        except Exception:
            failure.set()
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="openusage-refresh", daemon=True)
    thread.start()
    if thread_joiner is None:
        thread.join(timeout)
        completed = not thread.is_alive()
    else:
        completed = bool(thread_joiner(thread, timeout))
    worker_alive = thread.is_alive()
    succeeded = completed and not worker_alive and not failure.is_set()
    if not succeeded:
        stderr.write("refresh unavailable; using last-good ledger data\n")
    return RefreshOutcome(succeeded, worker_alive, thread, finished)


def _source_is_unhealthy(source: Any, generated_at: datetime) -> bool:
    return source.state != "ok" or (
        source.stale_at is not None
        and datetime.fromisoformat(source.stale_at.replace("Z", "+00:00"))
        <= generated_at
    )


def _is_catalog_diagnostic_source(source: Any) -> bool:
    return (
        source.provider_id == "openusage_catalog"
        and source.source_id == "openusage.detect"
    )


@dataclass(frozen=True)
class HealthSnapshot:
    partial: bool
    sources_ok: bool
    source_count: int


def _health_snapshot(query: QueryService, today: date) -> HealthSnapshot:
    source_result = query.source_status()
    sources = source_result.sources
    quotas = query.capacity().providers
    activity = query.activity(today, today)
    generated_at = datetime.fromisoformat(
        source_result.generated_at.replace("Z", "+00:00")
    )
    sources_unhealthy = any(
        _source_is_unhealthy(row, generated_at)
        for row in sources
        if not _is_catalog_diagnostic_source(row)
    )
    quotas_unhealthy = any(
        row.stale or row.state != "ok" for row in quotas
    )
    has_evidence = (
        any(row.covered for row in activity.coverage)
        or bool(quotas)
        or any(not _is_catalog_diagnostic_source(row) for row in sources)
    )
    return HealthSnapshot(
        partial=not has_evidence or sources_unhealthy or quotas_unhealthy,
        sources_ok=not sources_unhealthy,
        source_count=len(sources),
    )


def _doctor(
    query: QueryService,
    store: ActivityStore,
    health: HealthSnapshot,
) -> dict[str, Any]:
    sources = query.source_status()
    catalog_health = _openusage_catalog_health(sources.sources)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "dataRevision": sources.data_revision,
        "generatedAt": sources.generated_at,
        "health": {
            "schema": {
                "ok": store.schema_version == LEDGER_SCHEMA_VERSION,
                "version": store.schema_version,
            },
            "ledger": {"ok": True, "dataRevision": sources.data_revision},
            "query": {"ok": True},
            "sources": {"ok": health.sources_ok, "count": health.source_count},
            "openusageCatalog": catalog_health,
        },
    }


_CATALOG_ERROR = re.compile(
    r"^(openusage_unavailable|unsupported_openusage_version|provider_catalog_drift|"
    r"invalid_detect_output|timeout)_e(\d+)_a(\d+)_m(\d+)_x(\d+)$"
)


def _openusage_catalog_health(sources: Any) -> dict[str, Any]:
    expected = len(EXPECTED_PROVIDER_IDS)
    source = next(
        (
            row
            for row in sources
            if row.provider_id == "openusage_catalog"
            and row.source_id == "openusage.detect"
        ),
        None,
    )
    if source is None:
        return {
            "status": "not_checked", "expectedCount": expected,
            "actualCount": 0, "missingCount": 0, "extraCount": 0,
        }
    if source.state == "ok" and source.error_code is None:
        return {
            "status": "ok", "expectedCount": expected,
            "actualCount": expected, "missingCount": 0, "extraCount": 0,
        }
    match = _CATALOG_ERROR.fullmatch(source.error_code or "")
    if match is None:
        return {
            "status": "invalid_detect_output", "expectedCount": expected,
            "actualCount": 0, "missingCount": 0, "extraCount": 0,
        }
    return {
        "status": match.group(1),
        "expectedCount": int(match.group(2)),
        "actualCount": int(match.group(3)),
        "missingCount": int(match.group(4)),
        "extraCount": int(match.group(5)),
    }


@dataclass(frozen=True)
class CommandEvaluation:
    payload: dict[str, Any] | None
    jsonl_rows: tuple[dict[str, Any], ...]
    checkpoint: dict[str, Any] | None
    partial: bool


def _evaluate_command(
    args: argparse.Namespace,
    query: QueryService,
    store: ActivityStore,
    current: datetime,
) -> CommandEvaluation:
    today = current.astimezone().date()
    health = _health_snapshot(query, today) if args.strict or args.command == "doctor" else None
    if args.command == "status":
        result = query.summary(today)
        return CommandEvaluation(to_wire(result), (), None, bool(health and health.partial))
    if args.command == "snapshot":
        selected = _day(args.today) if args.today is not None else today
        result = query.resource_snapshot(selected)
        return CommandEvaluation(
            to_wire(result), (), None, bool(health and health.partial)
        )
    if args.command == "usage":
        result = query.activity(_day(args.from_day), _day(args.to_day))
        payload = to_wire(result)
        if args.format == "json":
            return CommandEvaluation(payload, (), None, bool(health and health.partial))
        rows = tuple(
            (row | {"type": "usage"}) for row in payload["rows"]
        ) + tuple(
            (row | {"type": "coverage"}) for row in payload["coverage"]
        )
        checkpoint = {
            "type": "checkpoint", "schemaVersion": result.schema_version,
            "dataRevision": result.data_revision, "generatedAt": result.generated_at,
            "nextCursor": result.data_revision,
        }
        return CommandEvaluation(None, rows, checkpoint, bool(health and health.partial))
    if args.command == "costs":
        result = query.costs(_day(args.from_day), _day(args.to_day))
        payload = to_wire(result)
        if args.format == "json":
            return CommandEvaluation(payload, (), None, bool(health and health.partial))
        rows = tuple(
            (row | {"type": "cost"}) for row in payload["rows"]
        ) + tuple(
            (row | {"type": "costCoverage"}) for row in payload["coverage"]
        )
        checkpoint = {
            "type": "checkpoint", "schemaVersion": result.schema_version,
            "dataRevision": result.data_revision, "generatedAt": result.generated_at,
            "nextCursor": result.data_revision,
        }
        return CommandEvaluation(None, rows, checkpoint, bool(health and health.partial))
    if args.command == "quotas":
        return CommandEvaluation(
            to_wire(query.capacity(args.limit)), (), None, bool(health and health.partial)
        )
    if args.command == "sources":
        return CommandEvaluation(
            to_wire(query.source_status()), (), None, bool(health and health.partial)
        )
    if args.command == "providers":
        return CommandEvaluation(
            to_wire(query.provider_instances()), (), None,
            bool(health and health.partial),
        )
    if args.command == "changes":
        result = query.changes(args.after, args.limit)
        payload = to_wire(result)
        if args.format == "json":
            return CommandEvaluation(payload, (), None, bool(health and health.partial))
        checkpoint = {
            "type": "checkpoint", "schemaVersion": result.schema_version,
            "dataRevision": result.data_revision, "generatedAt": result.generated_at,
            "nextCursor": result.next_cursor,
            "hasMore": result.has_more,
        }
        return CommandEvaluation(
            None, tuple(payload["records"]), checkpoint, bool(health and health.partial)
        )
    if args.command == "doctor":
        assert health is not None
        return CommandEvaluation(_doctor(query, store, health), (), None, health.partial)
    raise CLIError("invalid command")


def _render(evaluation: CommandEvaluation, stdout: TextIO) -> None:
    if evaluation.payload is not None:
        _write_json(stdout, evaluation.payload)
    else:
        _write_jsonl(stdout, list(evaluation.jsonl_rows), evaluation.checkpoint or {})


def _run_daemon(
    interval: int,
    refresher: Any,
    *,
    stop_event: threading.Event,
    waiter: Callable[[int], bool],
    stderr: TextIO,
    catalog_monitor: Any | None = None,
) -> int:
    while not stop_event.is_set():
        if catalog_monitor is not None:
            try:
                catalog_monitor.maybe_run()
            except Exception:
                pass
        try:
            refresher.refresh()
        except Exception:
            stderr.write("refresh unavailable; retained last-good ledger data\n")
        if waiter(interval):
            break
    return 0


def _run_daemon_with_api(
    interval: int,
    refresher: Any,
    query: QueryService,
    api_socket: str,
    *,
    transport: str = "unix",
    api_port: int = 0,
    api_token_path: str | None = None,
    stop_event: threading.Event,
    waiter: Callable[[int], bool],
    stderr: TextIO,
    catalog_monitor: Any | None = None,
) -> int:
    from .local_api import create_tcp_server, create_unix_server

    try:
        if transport == "tcp":
            server = create_tcp_server(
                query,
                port=api_port,
                token_path=api_token_path,
            )
        else:
            server = create_unix_server(api_socket, query)
    except Exception:
        stderr.write("local API unavailable; daemon stopped\n")
        return 1
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="openusage-local-api",
        daemon=True,
    )
    server_thread.start()
    try:
        return _run_daemon(
            interval, refresher, stop_event=stop_event, waiter=waiter,
            stderr=stderr, catalog_monitor=catalog_monitor,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(5)


def _gateway_config_payload(config: Any) -> dict[str, Any]:
    return {
        "cache_enabled": config.cache_enabled,
        "enabled": config.enabled,
        "host": config.host,
        "mode": config.mode.value,
        "port": config.port,
        "proxy_enabled": config.proxy_enabled,
    }


def _gateway_can_start(config: Any) -> bool:
    return config.enabled and config.mode.value in ("advise", "gateway")


def _build_default_gateway_server(
    *,
    config: Any,
    token_path: Path,
    query: QueryService,
) -> Any:
    """Build the optional Gateway without importing it for read-only commands."""
    from .gateway.api import GatewayRouter
    from .gateway.cache import SQLiteGatewayCache
    from .gateway.decision_trace import DecisionTraceRecorder
    from .gateway.policy import SnapshottingShouldSendEvaluator
    from .gateway.pools import account_pools_public_payload
    from .gateway.runtime import GatewayRuntime
    from .gateway.server import create_gateway_server
    from .gateway.telemetry import GatewayTelemetryStore

    telemetry = None
    if _gateway_can_start(config):
        try:
            telemetry = GatewayTelemetryStore(DEFAULT_GATEWAY_TELEMETRY_PATH)
        except Exception:
            telemetry = None

    burn_rate = None
    if telemetry is not None:
        def burn_rate(provider_id: str, model_id: str) -> float | None:
            return telemetry.recent_burn_rate(
                provider_id,
                model_id,
                window_minutes=5,
                max_rows=1_000,
            )

    should_send = SnapshottingShouldSendEvaluator(
        capacity=query.capacity,
        burn_rate=burn_rate,
        ttl_seconds=10.0,
    )
    decision_traces = DecisionTraceRecorder()

    proxy = None
    if config.mode.value == "gateway" and config.proxy_enabled:
        cache = None
        if config.cache_enabled:
            try:
                cache = SQLiteGatewayCache(DEFAULT_GATEWAY_CACHE_PATH)
            except Exception:
                cache = None
        proxy = GatewayRuntime(
            cache=cache,
            telemetry=telemetry,
        )

    router = GatewayRouter(
        mode=config.mode,
        policy=should_send,
        proxy=proxy,
        account_pools=lambda: account_pools_public_payload(
            accounts=config.accounts,
            pools=config.account_pools,
        ),
        decision_traces=decision_traces,
    )
    return create_gateway_server(
        router,
        host=config.host,
        port=config.port,
        token_path=token_path,
    )


def _run_gateway_server(
    config: Any,
    token_path: Path,
    query: QueryService,
    *,
    stop_event: threading.Event | None,
    server_factory: Callable[..., Any] | None,
    stderr: TextIO,
) -> int:
    active_stop = stop_event or threading.Event()
    if stop_event is None and threading.current_thread() is threading.main_thread():
        def stop(*_: object) -> None:
            active_stop.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    factory = server_factory or _build_default_gateway_server
    try:
        server = factory(
            config=config,
            token_path=token_path,
            query=query,
        )
    except Exception:
        stderr.write("gateway unavailable\n")
        return 1

    server_failed = threading.Event()

    def serve() -> None:
        try:
            server.serve_forever()
        except Exception:
            server_failed.set()
        finally:
            if not active_stop.is_set():
                server_failed.set()
                active_stop.set()

    server_thread = threading.Thread(
        target=serve,
        name="openusage-gateway",
        daemon=True,
    )
    started = False
    cleanup_failed = False
    try:
        server_thread.start()
        started = True
        try:
            active_stop.wait()
        except KeyboardInterrupt:
            active_stop.set()
    except Exception:
        server_failed.set()
    finally:
        if started:
            try:
                server.shutdown()
            except Exception:
                cleanup_failed = True
        try:
            server.server_close()
        except Exception:
            cleanup_failed = True
        if started:
            server_thread.join(5)
            if server_thread.is_alive():
                cleanup_failed = True

    if server_failed.is_set() or cleanup_failed:
        stderr.write("gateway unavailable\n")
        return 1
    return 0


class _UnavailablePluginDependency:
    def request(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("dependency unavailable")

    def query_usage(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("dependency unavailable")

    def query_quotas(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("dependency unavailable")

    def should_send(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("dependency unavailable")

    def health(self) -> str:
        raise RuntimeError("dependency unavailable")


class _LazyPluginFacts:
    def __init__(self, descriptor: Any) -> None:
        self._descriptor = descriptor

    def _client(self) -> Any:
        from .plugin.clients import BoundedJSONTransport, LocalFactsClient
        from .plugin.config import read_private_token

        if self._descriptor.local_api_transport == "unix":
            transport = BoundedJSONTransport(
                unix_socket_path=Path(str(self._descriptor.local_api_socket_path)),
            )
        else:
            transport = BoundedJSONTransport(
                host="127.0.0.1", port=self._descriptor.local_api_port,
                bearer_token=read_private_token(
                    Path(str(self._descriptor.local_api_token_path))
                ),
            )
        return LocalFactsClient(transport)

    def request(self, route: str, query: dict[str, object]) -> dict[str, object]:
        return self._client().request(route, query)

    def query_usage(self, request: dict[str, object]) -> dict[str, object]:
        return self._client().query_usage(request)

    def query_quotas(self, request: dict[str, object]) -> dict[str, object]:
        return self._client().query_quotas(request)


class _LazyPluginAdvice:
    def __init__(self, descriptor: Any) -> None:
        self._descriptor = descriptor

    def _client(self) -> Any:
        from .plugin.clients import BoundedJSONTransport, GatewayAdviceClient
        from .plugin.config import read_private_token

        transport = BoundedJSONTransport(
            host=self._descriptor.gateway_host, port=self._descriptor.gateway_port,
            bearer_token=read_private_token(
                Path(str(self._descriptor.gateway_token_path))
            ),
        )
        return GatewayAdviceClient(transport)

    def should_send(self, request: dict[str, object]) -> dict[str, object]:
        return self._client().should_send(request)

    def health(self) -> str:
        return self._client().health()


def _build_default_plugin_server(*, descriptor: Any, state_dir: Path) -> Any:
    from .plugin.api import PluginRouter
    from .plugin.config import PluginPrincipalRegistry
    from .plugin.server import create_plugin_server
    from .plugin.store import PluginStore

    registry = PluginPrincipalRegistry.load_or_create(state_dir)
    plugin_store = PluginStore(state_dir / "plugin.sqlite3")
    facts: object = _LazyPluginFacts(descriptor)
    advice: object = _LazyPluginAdvice(descriptor)
    router = PluginRouter(
        store=plugin_store, facts_client=facts, advice_client=advice,
        configured_principals=(),
    )
    try:
        return create_plugin_server(
            router, registry=registry, host="127.0.0.1", port=17824,
        )
    except Exception:
        router.close()
        raise


def _run_plugin_server(
    *, descriptor: Any, state_dir: Path,
    stop_event: threading.Event | None,
    server_factory: Callable[..., Any] | None,
    stderr: TextIO,
) -> int:
    active_stop = stop_event or threading.Event()
    if stop_event is None and threading.current_thread() is threading.main_thread():
        def stop(*_: object) -> None:
            active_stop.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    try:
        server = (server_factory or _build_default_plugin_server)(
            descriptor=descriptor, state_dir=state_dir,
        )
    except Exception:
        stderr.write("plugin API unavailable\n")
        return 1
    failed = threading.Event()

    def serve() -> None:
        try:
            server.serve_forever()
        except Exception:
            failed.set()
            active_stop.set()

    thread = threading.Thread(target=serve, name="openusage-plugin", daemon=True)
    cleanup_failed = False
    thread.start()
    try:
        active_stop.wait()
    except KeyboardInterrupt:
        active_stop.set()
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            cleanup_failed = True
        thread.join(5)
    if failed.is_set() or cleanup_failed or thread.is_alive():
        stderr.write("plugin API unavailable\n")
        return 1
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    store: ActivityStore | None = None,
    query: QueryService | None = None,
    store_factory: Callable[[], ActivityStore] | None = None,
    refresher: Any | None = None,
    refresher_factory: Callable[[ActivityStore], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    offline: bool = False,
    fresh_timeout: int = DEFAULT_FRESH_TIMEOUT_SECONDS,
    thread_joiner: Callable[[threading.Thread, int], bool] | None = None,
    stop_event: threading.Event | None = None,
    waiter: Callable[[int], bool] | None = None,
    subprocess_runner: Callable[..., Any] | None = None,
    refresh_entrypoint: Path | None = None,
    child_environment: dict[str, str] | None = None,
    catalog_monitor: Any | None = None,
    gateway_server_factory: Callable[..., Any] | None = None,
    plugin_server_factory: Callable[..., Any] | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == INTERNAL_PLUGIN_SELF_TEST_COMMAND:
        return _internal_plugin_self_test(
            arguments, stdout=stdout, stderr=stderr,
        )
    if arguments and arguments[0] == INTERNAL_GATEWAY_SELF_TEST_COMMAND:
        return _internal_gateway_self_test(
            arguments,
            stdout=stdout,
            stderr=stderr,
        )
    if arguments and arguments[0] == INTERNAL_REFRESH_COMMAND:
        return _internal_refresh_once(
            arguments,
            stderr=stderr,
            refresher_factory=refresher_factory,
        )
    owned = store is None
    parser = _parser()
    try:
        args = parser.parse_args(arguments)
        if args.command in ("daemon", "service") or (
            args.command == "desktop-service"
            and args.desktop_service_action == "install"
        ):
            interval = _interval(args.interval)
        else:
            interval = None
        api_port = _api_port(args.api_port) if args.command == "daemon" else 0
    except CLIError:
        stderr.write("invalid command input\n")
        return 2

    if args.command == "plugin":
        try:
            descriptor = _runtime_descriptor()
            state_dir = (
                Path(args.state_dir)
                if args.plugin_action == "start" and args.state_dir is not None
                else Path(str(descriptor.plugin_state_dir))
            )
            if args.plugin_action == "status":
                _write_json(stdout, {
                    "apiVersion": "plugin-runtime/v1", "object": "plugin.runtime",
                    "host": descriptor.plugin_host, "port": descriptor.plugin_port,
                    "configured": state_dir.is_dir(),
                })
                return 0
            if args.plugin_action in {"install-service", "uninstall-service", "print-service"}:
                from . import platform_services
                if args.plugin_action == "print-service":
                    stdout.write(platform_services.render_plugin_current_platform() + "\n")
                elif args.plugin_action == "install-service":
                    platform_services.install_plugin_service()
                else:
                    platform_services.uninstall_plugin_service()
                return 0
            if not state_dir.is_absolute() or ".." in state_dir.parts:
                raise CLIError("invalid Plugin state directory")
            return _run_plugin_server(
                descriptor=descriptor, state_dir=state_dir,
                stop_event=stop_event, server_factory=plugin_server_factory,
                stderr=stderr,
            )
        except CLIError:
            stderr.write("invalid command input\n")
            return 2
        except Exception:
            stderr.write("plugin API unavailable\n")
            return 1

    if args.command == "service":
        from . import platform_services

        if args.action == "print":
            stdout.write(
                platform_services.render_current_platform(
                    interval=interval,
                    command=args.service_command,
                )
            )
            stdout.write("\n")
            return 0
        try:
            if args.action == "install":
                platform_services.install_service(
                    interval=interval,
                    command=args.service_command,
                )
            else:
                platform_services.uninstall_service()
        except Exception:
            stderr.write("service action failed\n")
            return 1
        return 0

    if args.command == "desktop-service":
        from .managed_collector import (
            install_managed_collector,
            uninstall_managed_collector,
        )

        try:
            if args.desktop_service_action == "install":
                install_managed_collector(interval=interval)
            else:
                uninstall_managed_collector()
        except Exception:
            stderr.write("desktop service action failed\n")
            return 1
        return 0

    if args.command == "state":
        from .lifecycle_state import (
            LifecycleStatePaths,
            current_user_runtime_is_active,
            delete_local_state,
        )

        try:
            paths = LifecycleStatePaths.for_current_user()
            result = delete_local_state(
                paths,
                confirmation=args.state_confirmation,
                runtime_is_active=lambda: current_user_runtime_is_active(paths),
            )
        except Exception:
            stderr.write("local state delete failed\n")
            return 1
        _write_json(
            stdout,
            {
                "apiVersion": "local-state-lifecycle/v1",
                "object": "local.state_delete",
                "deleted": result.deleted,
            },
        )
        return 0

    gateway_config: Any | None = None
    gateway_token_path: str | None = None
    if args.command == "gateway":
        from .gateway.config import GatewayConfig, load_gateway_config

        if args.gateway_action == "print-config":
            _write_json(stdout, _gateway_config_payload(GatewayConfig()))
            return 0

        try:
            gateway_config = load_gateway_config(Path(args.config))
        except ValueError:
            stderr.write("invalid gateway configuration\n")
            return 2
        payload = _gateway_config_payload(gateway_config)
        if args.gateway_action == "status":
            _write_json(
                stdout,
                payload | {"can_start": _gateway_can_start(gateway_config)},
            )
            return 0
        if not _gateway_can_start(gateway_config):
            stderr.write("gateway_disabled\n")
            return 1
        try:
            gateway_token_path = (
                args.token_path
                if args.token_path is not None
                else _default_gateway_token_path()
            )
        except CLIError:
            stderr.write("invalid command input\n")
            return 2

    active_store: ActivityStore | None = store
    refresh_outcome: RefreshOutcome | None = None
    deferred_close = False
    try:
        if active_store is None:
            if store_factory is not None:
                active_store = store_factory()
            else:
                DEFAULT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
                active_store = ActivityStore(DEFAULT_LEDGER_PATH)
        active_query = query or QueryService(active_store, clock=clock)

        if args.command == "gateway":
            assert gateway_config is not None
            assert gateway_token_path is not None
            return _run_gateway_server(
                gateway_config,
                Path(gateway_token_path),
                active_query,
                stop_event=stop_event,
                server_factory=gateway_server_factory,
                stderr=stderr,
            )

        if args.command == "reconcile":
            from .reconciliation import reconciliation_from_local_sources

            since_day = _day(args.from_day)
            until_day = _day(args.to_day)
            report = reconciliation_from_local_sources(
                since=since_day,
                until=until_day,
            )
            payload = {
                "schemaVersion": "1.0",
                "since": report.since.isoformat(),
                "until": report.until.isoformat(),
                "sourceStatuses": [
                    {
                        "providerId": provider_id,
                        "sourceId": source_id,
                        "state": state,
                    }
                    for provider_id, source_id, state in report.source_statuses
                ],
                "rows": [
                    {
                        "day": row.day,
                        "codexTokenTotal": row.codex_token_total,
                        "ccSwitchCostUsd": row.cc_switch_cost_usd,
                        "omnirouteCostUsd": row.omniroute_cost_usd,
                        "ccSwitchCovered": row.cc_switch_covered,
                        "omnirouteCovered": row.omniroute_covered,
                        "notes": list(row.notes),
                    }
                    for row in report.rows
                ],
            }
            stdout.write(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            )
            stdout.write("\n")
            return 0

        if args.command == "executor":
            from .executors import apply_executor_switch, default_executors

            plugins = {plugin.plugin_id: plugin for plugin in default_executors()}
            if args.action == "list":
                payload = {
                    "schemaVersion": "1.0",
                    "executors": [
                        {
                            "pluginId": plugin.plugin_id,
                            "available": plugin.state().available,
                            "activeTarget": plugin.state().active_target,
                        }
                        for plugin in plugins.values()
                    ],
                }
            else:
                if args.plugin is None or args.plugin not in plugins:
                    stderr.write("invalid executor plugin\n")
                    return 2
                plugin = plugins[args.plugin]
                if args.action == "state":
                    state = plugin.state()
                    payload = {
                        "schemaVersion": "1.0",
                        "pluginId": state.plugin_id,
                        "available": state.available,
                        "activeTarget": state.active_target,
                        "detail": state.detail,
                    }
                elif args.action == "verify":
                    payload = {
                        "schemaVersion": "1.0",
                        "pluginId": plugin.plugin_id,
                        "verified": plugin.verify(),
                    }
                else:
                    if not args.target:
                        stderr.write("invalid executor target\n")
                        return 2
                    result = apply_executor_switch(
                        plugin,
                        args.target,
                        enabled=args.auto_apply,
                    )
                    payload = {
                        "schemaVersion": "1.0",
                        "pluginId": result.plugin_id,
                        "target": result.target,
                        "ok": result.ok,
                        "detail": result.detail,
                    }
            stdout.write(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            )
            stdout.write("\n")
            return 0

        if args.command == "connect":
            from .quick_connect import quick_connect

            try:
                item = quick_connect(args.family)
            except ValueError:
                stderr.write("invalid provider family\n")
                return 2
            payload = {
                "schemaVersion": "1.0",
                "familyId": item.family_id,
                "consoleUrl": item.console_url,
                "authModes": list(item.auth_modes),
            }
            stdout.write(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            )
            stdout.write("\n")
            return 0

        if args.command == "dashboard":
            from .web_dashboard import make_dashboard_server

            port = args.port
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 0 <= port <= 65535
            ):
                stderr.write("invalid dashboard port\n")
                return 2
            today = (clock or (lambda: datetime.now(timezone.utc)))().astimezone().date()
            server = make_dashboard_server(active_query, port=port, today=today)
            stdout.write(
                f"UsageHub dashboard on http://127.0.0.1:{server.server_address[1]}\n"
            )
            stdout.flush()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0

        if args.command == "daemon":
            if offline or args.offline:
                refresher = OfflineRefresher()
                catalog_monitor = None
            else:
                if catalog_monitor is None:
                    from .daily_history import OpenUsageCatalogMonitor

                    catalog_monitor = OpenUsageCatalogMonitor(active_store, clock=clock)
                if refresher is None:
                    factory = refresher_factory or build_default_refresher
                    try:
                        refresher = factory(active_store)
                    except Exception:
                        refresher = UnavailableRefresher()
            active_stop = stop_event or threading.Event()
            if stop_event is None and threading.current_thread() is threading.main_thread():
                def stop(*_: object) -> None:
                    active_stop.set()
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
            active_waiter = waiter or active_stop.wait
            resolved_transport, resolved_port = _resolve_api_transport(
                args.api_transport,
                api_port,
            )
            api_token_path = args.api_token_path
            if resolved_transport == "tcp" and api_token_path is None:
                api_token_path = _default_api_token_path()
            return _run_daemon_with_api(
                interval or MIN_DAEMON_INTERVAL_SECONDS,
                refresher,
                active_query,
                args.api_socket,
                transport=resolved_transport,
                api_port=resolved_port,
                api_token_path=api_token_path,
                stop_event=active_stop,
                waiter=active_waiter,
                stderr=stderr,
                catalog_monitor=catalog_monitor,
            )

        is_offline = offline or args.offline
        current = (clock or (lambda: datetime.now(timezone.utc)))()
        evaluation = _evaluate_command(args, active_query, active_store, current)
        if args.fresh and not is_offline:
            selected_timeout = _fresh_timeout(fresh_timeout)
            if refresher is None and refresher_factory is not None:
                try:
                    refresher = refresher_factory(active_store)
                except Exception:
                    refresher = UnavailableRefresher()
            if refresher is not None:
                refresh_outcome = _refresh_once(
                    refresher,
                    timeout=selected_timeout,
                    stderr=stderr,
                    thread_joiner=thread_joiner,
                )
                refreshed = refresh_outcome.succeeded
            elif (
                active_store.path == ":memory:"
                and subprocess_runner is None
                and refresh_entrypoint is None
            ):
                stderr.write("refresh unavailable; using last-good ledger data\n")
                refreshed = False
            else:
                refreshed = _refresh_in_subprocess(
                    active_store.path,
                    timeout=selected_timeout,
                    stderr=stderr,
                    runner=subprocess_runner or run_bounded,
                    entrypoint=refresh_entrypoint,
                    environment=child_environment,
                )
            if refreshed:
                evaluation = _evaluate_command(args, active_query, active_store, current)
        _render(evaluation, stdout)
        if args.strict and evaluation.partial:
            return 3
        return 0
    except (CLIError, ValueError):
        stderr.write("invalid query input\n")
        return 2
    except Exception:
        stderr.write("operation unavailable\n")
        return 1
    finally:
        if owned and active_store is not None:
            if refresh_outcome is not None and refresh_outcome.worker_alive:
                store_to_close = active_store

                def close_after_refresh() -> None:
                    refresh_outcome.finished.wait()
                    store_to_close.close()

                threading.Thread(
                    target=close_after_refresh,
                    name="openusage-store-cleanup",
                    daemon=True,
                ).start()
                deferred_close = True
            if not deferred_close:
                active_store.close()
