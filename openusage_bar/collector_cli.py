from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from .activity_store import ActivityStore, SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from .bounded_process import run_bounded
from .openusage_adapter import child_subprocess_environment
from .openusage_catalog import EXPECTED_PROVIDER_IDS
from .performance_timing import RefreshTimingRecorder, write_timing_report
from .query import QueryService, SCHEMA_VERSION, to_wire


DEFAULT_LEDGER_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "activity.sqlite3"
DEFAULT_API_SOCKET_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "openusage.sock"
DEFAULT_RUNTIME_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "runtime.sqlite3"
# An interactive attempt may legitimately use OpenUsage's bounded auto -> direct
# fallback (12s + 75s) followed by a bounded daily-history import (60s). One
# hundred sixty seconds avoids killing that slow path, but remains a hard limit;
# it is not a completion guarantee for an arbitrary number of configured sources.
DEFAULT_FRESH_TIMEOUT_SECONDS = 160
MIN_DAEMON_INTERVAL_SECONDS = 60
INTERNAL_REFRESH_COMMAND = "__refresh-once"


class CLIError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(message)


class UnavailableRefresher:
    def refresh(self) -> None:
        raise RuntimeError("refresh unavailable")


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
    runtime_ingest = commands.add_parser("runtime-ingest")
    runtime_ingest.add_argument("--database", default=str(DEFAULT_RUNTIME_PATH))
    runtime_summary = commands.add_parser("runtime-summary")
    runtime_summary.add_argument("--database", default=str(DEFAULT_RUNTIME_PATH))
    runtime_summary.add_argument("--window-seconds", required=True)
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


def _runtime_database_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CLIError("invalid runtime database")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise CLIError("invalid runtime database")
    if path == DEFAULT_RUNTIME_PATH:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.parent.is_dir():
        raise CLIError("invalid runtime database")
    return path


def _runtime_window_seconds(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise CLIError("invalid runtime window")
    seconds = int(value)
    if not 60 <= seconds <= 86_400:
        raise CLIError("invalid runtime window")
    return seconds


def _runtime_latency_wire(value: Any) -> dict[str, Any]:
    return {
        "durationSampleCount": value.duration_sample_count,
        "durationAvgMs": value.duration_avg_ms,
        "durationP95Ms": value.duration_p95_ms,
        "ttftSampleCount": value.ttft_sample_count,
        "ttftAvgMs": value.ttft_avg_ms,
        "ttftP95Ms": value.ttft_p95_ms,
    }


def _runtime_costs_wire(values: Any) -> list[dict[str, Any]]:
    return [
        {"currency": value.currency, "micros": value.cost_micros}
        for value in values
    ]


def _runtime_tokens_wire(value: Any) -> dict[str, Any]:
    return {
        "input": value.input_tokens,
        "output": value.output_tokens,
        "cacheRead": value.cache_read_tokens,
        "cacheCreation": value.cache_creation_tokens,
        "reasoning": value.reasoning_tokens,
        "total": value.total_tokens,
        "countingConventions": list(value.token_counting_conventions),
    }


def _runtime_summary_wire(value: Any) -> dict[str, Any]:
    groups = []
    for group in value.groups:
        groups.append({
            "providerId": group.provider_id,
            "modelId": group.model_id,
            "scopeRef": group.scope_ref,
            "observationCount": group.observation_count,
            "tokens": _runtime_tokens_wire(group),
            "statusCounts": dict(group.status_counts),
            "costCoverage": {"state": group.cost_coverage_state},
            "costs": _runtime_costs_wire(group.costs),
            "latency": _runtime_latency_wire(group.latency),
        })
    return {
        "schemaVersion": value.schema_version,
        "runtimeRevision": value.runtime_revision,
        "generatedAt": value.generated_at,
        "window": {"start": value.window_start, "end": value.window_end},
        "coverage": {
            "state": value.coverage_state,
            "omittedGroupCount": value.omitted_group_count,
        },
        "observationCount": value.observation_count,
        "tokens": _runtime_tokens_wire(value),
        "statusCounts": dict(value.status_counts),
        "costCoverage": {"state": value.cost_coverage_state},
        "costs": _runtime_costs_wire(value.costs),
        "latency": _runtime_latency_wire(value.latency),
        "groups": groups,
    }


def _run_runtime_command(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    clock: Callable[[], datetime] | None,
) -> int:
    from .runtime_observation import MAX_DOCUMENT_BYTES, decode_runtime_document
    from .runtime_store import RuntimeStore

    if args.offline or args.fresh or args.strict:
        raise CLIError("invalid runtime option")
    path = _runtime_database_path(args.database)
    current = (clock or (lambda: datetime.now(timezone.utc)))()
    if args.command == "runtime-ingest":
        payload = stdin.read(MAX_DOCUMENT_BYTES + 1)
        document = decode_runtime_document(payload)
        store = RuntimeStore(path, clock=lambda: current)
        try:
            result = store.ingest(document.observations)
        finally:
            store.close()
        _write_json(stdout, {
            "schemaVersion": document.schema_version,
            "runtimeRevision": result.revision,
            "acceptedCount": result.accepted_count,
            "duplicateCount": result.duplicate_count,
            "expiredCount": result.expired_count,
            "prunedCount": result.pruned_count,
        })
        return 0
    if args.command == "runtime-summary":
        seconds = _runtime_window_seconds(args.window_seconds)
        store = RuntimeStore(path, clock=lambda: current)
        try:
            summary = store.summary(current - timedelta(seconds=seconds), current)
        finally:
            store.close()
        _write_json(stdout, _runtime_summary_wire(summary))
        return 0
    raise CLIError("invalid runtime command")


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
    stop_event: threading.Event,
    waiter: Callable[[int], bool],
    stderr: TextIO,
    catalog_monitor: Any | None = None,
) -> int:
    from .local_api import create_unix_server

    try:
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


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
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
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    arguments = list(sys.argv[1:] if argv is None else argv)
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
        if args.command == "daemon":
            interval = _interval(args.interval)
        else:
            interval = None
    except CLIError:
        stderr.write("invalid command input\n")
        return 2

    if args.command in {"runtime-ingest", "runtime-summary"}:
        try:
            return _run_runtime_command(
                args, stdin=stdin, stdout=stdout, clock=clock
            )
        except CLIError:
            stderr.write("invalid command input\n")
            return 2
        except ValueError as error:
            from .runtime_observation import RuntimeObservationDecodeError

            if isinstance(error, RuntimeObservationDecodeError):
                stderr.write("invalid runtime input\n")
            else:
                stderr.write("invalid command input\n")
            return 2
        except Exception:
            stderr.write("runtime observation unavailable\n")
            return 1

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

        if args.command == "daemon":
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
            return _run_daemon_with_api(
                interval or MIN_DAEMON_INTERVAL_SECONDS,
                refresher,
                active_query,
                args.api_socket,
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
