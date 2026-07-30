from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


SOURCE_CLASSES = ("network", "local_file", "child_process")
OUTCOMES = ("success", "backoff", "timeout", "unavailable", "failed")
_BACKOFF_ERRORS = frozenset({"rate_limited", "import_in_progress", "backoff"})
_TIMEOUT_ERRORS = frozenset({"timeout", "timed_out"})


def source_class_for(source: object, default: str) -> str:
    candidate = getattr(source, "performance_source_class", default)
    if candidate not in SOURCE_CLASSES:
        return default
    return candidate


def _outcome(result: Any) -> str:
    error_code = getattr(result, "error_code", None)
    if error_code in _BACKOFF_ERRORS:
        return "backoff"
    if error_code in _TIMEOUT_ERRORS:
        return "timeout"
    ok = getattr(result, "ok", None)
    if ok is True:
        return "success"
    if ok is False:
        return "unavailable"

    cards = getattr(result, "cards", None)
    if cards is None:
        cards = [result] if hasattr(result, "status") else []
    for card in cards:
        status = getattr(card, "status", None)
        if getattr(status, "value", status) == "ok":
            return "success"
    return "unavailable"


class RefreshTimingRecorder:
    """Aggregate monotonic refresh timings without retaining source identity."""

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        self.monotonic = monotonic or time.monotonic
        self._samples: list[tuple[str, str, float]] = []
        self._lock = threading.Lock()

    def record(self, source_class: str, outcome: str, duration_seconds: float) -> None:
        if source_class not in SOURCE_CLASSES:
            raise ValueError("invalid performance source class")
        if outcome not in OUTCOMES:
            raise ValueError("invalid performance outcome")
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            self._samples.append((source_class, outcome, duration))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = tuple(self._samples)
        classes: list[dict[str, Any]] = []
        for source_class in SOURCE_CLASSES:
            selected = [
                (outcome, duration)
                for candidate, outcome, duration in samples
                if candidate == source_class
            ]
            if not selected:
                continue
            classes.append(
                {
                    "sourceClass": source_class,
                    "sampleCount": len(selected),
                    "durationSecondsTotal": round(
                        sum(duration for _, duration in selected), 3
                    ),
                    "durationSecondsMax": round(
                        max(duration for _, duration in selected), 3
                    ),
                    "outcomes": {
                        outcome: sum(
                            candidate == outcome for candidate, _ in selected
                        )
                        for outcome in OUTCOMES
                    },
                }
            )
        return {
            "schemaVersion": 1,
            "scope": "source-class",
            "classes": classes,
        }


def measure_source_call(
    recorder: RefreshTimingRecorder | None,
    source_class: str,
    operation: Callable[[], Any],
) -> Any:
    if source_class not in SOURCE_CLASSES:
        raise ValueError("invalid performance source class")
    if recorder is None:
        return operation()
    started = recorder.monotonic()
    try:
        result = operation()
    except Exception:
        recorder.record(source_class, "failed", recorder.monotonic() - started)
        raise
    recorder.record(source_class, _outcome(result), recorder.monotonic() - started)
    return result


def write_timing_report(path: Path, payload: dict[str, Any]) -> None:
    if (
        set(payload) != {"schemaVersion", "scope", "classes"}
        or payload.get("schemaVersion") != 1
        or payload.get("scope") != "source-class"
        or not isinstance(payload.get("classes"), list)
    ):
        raise ValueError("invalid performance timing report")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload["classes"]:
        if not isinstance(item, dict) or set(item) != {
            "sourceClass",
            "sampleCount",
            "durationSecondsTotal",
            "durationSecondsMax",
            "outcomes",
        }:
            raise ValueError("invalid performance timing report")
        source_class = item["sourceClass"]
        count = item["sampleCount"]
        total = item["durationSecondsTotal"]
        maximum = item["durationSecondsMax"]
        outcomes = item["outcomes"]
        if (
            source_class not in SOURCE_CLASSES
            or source_class in seen
            or type(count) is not int
            or count < 0
            or isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not math.isfinite(total)
            or total < 0
            or isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or not math.isfinite(maximum)
            or maximum < 0
            or not isinstance(outcomes, dict)
            or set(outcomes) != set(OUTCOMES)
            or any(
                type(outcomes[outcome]) is not int
                or outcomes[outcome] < 0
                for outcome in OUTCOMES
            )
            or sum(outcomes.values()) != count
        ):
            raise ValueError("invalid performance timing report")
        seen.add(source_class)
        normalized.append(
            {
                "sourceClass": source_class,
                "sampleCount": count,
                "durationSecondsTotal": round(float(total), 3),
                "durationSecondsMax": round(float(maximum), 3),
                "outcomes": {
                    outcome: outcomes[outcome] for outcome in OUTCOMES
                },
            }
        )
    normalized.sort(
        key=lambda item: SOURCE_CLASSES.index(item["sourceClass"])
    )
    safe_payload = {
        "schemaVersion": 1,
        "scope": "source-class",
        "classes": normalized,
    }
    rendered = json.dumps(
        safe_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
