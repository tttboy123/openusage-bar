from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .keychain import KeychainError
from .models import Category, Overview, ProviderCard, ProviderStatus, canonical_category


DEFAULT_CACHE_PATH = Path.home() / ".local" / "state" / "openusage-bar" / "cards.json"
KEYCHAIN_SERVICE = "com.lune.openusage-menubar"
MAX_KEYCHAIN_VALUE_BYTES = 64 * 1024


class BoundedReadOnlyKeychain:
    """Read headless credentials without an unbounded Security-framework prompt."""

    def __init__(
        self,
        timeout_seconds: int = 5,
        security_executable: str = "/usr/bin/security",
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("Keychain timeout must be between 1 and 30 seconds")
        if not security_executable.startswith("/") or "\x00" in security_executable:
            raise ValueError("Security executable must be an absolute path")
        self.timeout_seconds = timeout_seconds
        self.security_executable = security_executable
        self.last_read_bytes = 0
        self.last_process_alive = False

    @staticmethod
    def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()

    def get(self, account: str) -> str | None:
        if not account or any(character in account for character in "\x00\r\n"):
            return None
        environment = {"PATH": "/usr/bin:/bin"}
        for name in ("HOME", "USER", "LOGNAME", "TMPDIR"):
            value = os.environ.get(name)
            if value and "\x00" not in value:
                environment[name] = value
        process: subprocess.Popen[bytes] | None = None
        output = bytearray()
        try:
            process = subprocess.Popen(
                [
                    self.security_executable, "find-generic-password",
                    "-s", KEYCHAIN_SERVICE, "-a", account, "-w",
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
            assert process.stdout is not None
            deadline = time.monotonic() + self.timeout_seconds
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        self._kill_and_reap(process)
                        return None
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(8192, MAX_KEYCHAIN_VALUE_BYTES + 1 - len(output)),
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    self.last_read_bytes = len(output)
                    if len(output) > MAX_KEYCHAIN_VALUE_BYTES:
                        self._kill_and_reap(process)
                        return None
            remaining = max(0.0, deadline - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._kill_and_reap(process)
                return None
        except (OSError, ValueError):
            if process is not None:
                self._kill_and_reap(process)
            return None
        finally:
            self.last_process_alive = bool(process is not None and process.poll() is None)
            if process is not None and process.stdout is not None:
                process.stdout.close()
        if returncode != 0:
            return None
        try:
            value = bytes(output).decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            return None
        return value or None

    def set(self, _account: str, _secret: str) -> None:
        raise KeychainError("Headless Keychain access is read-only")


class Adapter(Protocol):
    def fetch(self) -> Overview | ProviderCard: ...


def merge_cards(base: list[ProviderCard], overrides: list[ProviderCard]) -> list[ProviderCard]:
    merged = {card.provider_id: card for card in base}
    for card in overrides:
        previous = merged.get(card.provider_id)
        if previous is not None:
            if (
                previous.family_id is not None
                and card.family_id is not None
                and previous.family_id != card.family_id
            ):
                raise ValueError(
                    f"provider instance {card.provider_id!r} has conflicting family IDs"
                )
            card = replace(
                card,
                family_id=card.family_id or previous.family_id,
                credential_source=(
                    card.credential_source or previous.credential_source
                ),
                source_kind=card.source_kind or previous.source_kind,
            )
        merged[card.provider_id] = card
    return list(merged.values())


class CardCache:
    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        self.path = path

    def save(self, cards: list[ProviderCard]) -> None:
        serialized = []
        for card in cards:
            raw = asdict(card)
            raw["category"] = canonical_category(card.provider_id, card.category).value
            raw["status"] = card.status.value
            raw["refreshed_at"] = card.refreshed_at.isoformat()
            raw["resets_at"] = card.resets_at.isoformat() if card.resets_at else None
            serialized.append(raw)
        payload = {"version": 1, "cards": serialized}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="cards.", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> list[ProviderCard]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("cards"), list):
                return []
            cards = []
            for raw in payload["cards"]:
                cards.append(
                    ProviderCard(
                        provider_id=raw["provider_id"],
                        name=raw["name"],
                        category=canonical_category(raw["provider_id"], Category(raw["category"])),
                        status=ProviderStatus(raw["status"]),
                        primary=raw.get("primary"),
                        detail=raw.get("detail"),
                        remaining_percent=raw.get("remaining_percent"),
                        resets_at=datetime.fromisoformat(raw["resets_at"]) if raw.get("resets_at") else None,
                        source=raw["source"],
                        refreshed_at=datetime.fromisoformat(raw["refreshed_at"]),
                        stale=bool(raw.get("stale", False)),
                        last_error=raw.get("last_error"),
                        family_id=raw.get("family_id"),
                        credential_source=raw.get("credential_source"),
                        source_kind=raw.get("source_kind"),
                    )
                )
            return cards
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return []


class Aggregator:
    def __init__(self, adapters: list[Adapter], cache: CardCache, clock=None) -> None:
        self.adapters = adapters
        self.cache = cache
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._refresh_lock = threading.Lock()

    def refresh(self) -> Overview:
        if not self._refresh_lock.acquire(blocking=False):
            return Overview(self.cache.load())
        try:
            cached = {card.provider_id: card for card in self.cache.load()}
            fresh: list[ProviderCard] = []
            for adapter in self.adapters:
                try:
                    result = adapter.fetch()
                except Exception:
                    continue
                cards = result.cards if isinstance(result, Overview) else [result]
                fresh = merge_cards(fresh, cards)

            resolved: list[ProviderCard] = []
            seen: set[str] = set()
            has_fresh_openusage = any(
                card.source == "OpenUsage"
                and card.provider_id != "openusage"
                and card.status == ProviderStatus.OK
                for card in fresh
            )
            for current in fresh:
                seen.add(current.provider_id)
                previous = cached.get(current.provider_id)
                if previous is not None:
                    if (
                        previous.family_id is not None
                        and current.family_id is not None
                        and previous.family_id != current.family_id
                    ):
                        raise ValueError(
                            f"provider instance {current.provider_id!r} has conflicting family IDs"
                        )
                if (
                    current.provider_id in {"kiro_cli", "codex"}
                    and current.source == "OpenUsage"
                    and current.remaining_percent is None
                    and previous is not None
                    and previous.remaining_percent is not None
                ):
                    resolved.append(
                        replace(
                            previous,
                            family_id=previous.family_id or current.family_id,
                            credential_source=(
                                previous.credential_source
                                or current.credential_source
                            ),
                            source_kind=previous.source_kind or current.source_kind,
                            detail=(
                                f"{previous.detail} · Activity {current.primary}"
                                if previous.detail and current.primary
                                else (
                                    f"Activity {current.primary}"
                                    if current.primary
                                    else previous.detail
                                )
                            ),
                            status=ProviderStatus.STALE,
                            stale=True,
                            last_error="Quota enrichment did not return fresh data",
                        )
                    )
                    continue
                if (
                    current.status != ProviderStatus.OK
                    and current.primary is None
                    and previous
                    and previous.primary is not None
                ):
                    status = ProviderStatus.STALE if current.status == ProviderStatus.ERROR else current.status
                    resolved.append(
                        replace(
                            previous,
                            family_id=current.family_id or previous.family_id,
                            credential_source=(
                                current.credential_source
                                or previous.credential_source
                            ),
                            source_kind=current.source_kind or previous.source_kind,
                            status=status,
                            stale=True,
                            last_error=current.last_error or current.detail,
                        )
                    )
                else:
                    resolved.append(current)

            for provider_id, previous in cached.items():
                if provider_id not in seen:
                    if provider_id == "openusage" and has_fresh_openusage:
                        continue
                    resolved.append(
                        replace(
                            previous,
                            status=ProviderStatus.STALE,
                            stale=True,
                            last_error="Provider did not return fresh data",
                        )
                    )
            self.cache.save(resolved)
            return Overview(resolved)
        finally:
            self._refresh_lock.release()


class LedgerRefresher:
    """Headless bridge from provider cards into the canonical activity ledger."""

    def __init__(self, aggregator, collector) -> None:
        self.aggregator = aggregator
        self.collector = collector

    def refresh(self) -> None:
        self.collector.refresh(self.aggregator.refresh())


def build_headless_refresher(activity_store):
    """Build the production collector without importing the AppKit UI module."""
    from .config import ProviderConfigStore
    from .daily_history import ActivityCollector
    from .providers.builtins import default_registry

    clock = lambda: datetime.now(timezone.utc)
    keychain = BoundedReadOnlyKeychain()
    config_store = ProviderConfigStore()
    try:
        configs = config_store.load()
    except (OSError, ValueError):
        configs = []
    bindings = default_registry(clock=clock, keychain=keychain).build(configs)
    adapters = [item[4] for item in sorted(
        (
            getattr(adapter, "source_priority", 100),
            binding.provider_id,
            getattr(adapter, "source_id", type(adapter).__name__),
            type(adapter).__qualname__,
            adapter,
        )
        for binding in bindings for adapter in binding.quota_sources
    )]
    openusage_importer = next(
        source
        for binding in bindings if binding.provider_id == "openusage"
        for source in binding.usage_sources
    )
    official_importers = {}
    for binding in bindings:
        if binding.provider_id == "openusage":
            continue
        sources = {id(source): source for source in (
            *binding.usage_sources, *binding.cost_sources,
        )}
        if len(sources) > 1:
            raise ValueError(
                "legacy collector requires one combined importer per Provider"
            )
        if sources:
            official_importers[binding.provider_id] = next(iter(sources.values()))
    aggregator = Aggregator(adapters, CardCache(), clock)
    collector = ActivityCollector(
        activity_store,
        openusage_importer,
        official_importers=official_importers,
        clock=clock,
    )
    return LedgerRefresher(aggregator, collector)
