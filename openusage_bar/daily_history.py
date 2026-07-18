from __future__ import annotations

import json
import math
import subprocess
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from .activity_store import (
    DAILY_ACTIVITY_SOURCE_ID,
    ActivityStore,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from .bounded_process import BoundedProcessError, run_bounded
from .capabilities import MetricFamily, registry, state_from_card
from .codex_attribution import CodexAttributionResolver
from .config import ID_PATTERN
from .models import Overview, ProviderCard
from .model_ids import InvalidModelID, canonical_model_id
from .openusage_adapter import (
    child_subprocess_environment,
    openusage_path as resolve_openusage_path,
)
from .openusage_catalog import (
    EXPECTED_PROVIDER_IDS,
    CatalogDiagnostic,
    OpenUsageCatalogDiscovery,
)
from .providers.contracts import (
    CostImportSuccess,
    ImportFailure,
    QuotaFetchFailure,
    QuotaFetchSuccess,
    UsageImportSuccess,
)
from .openai_organization import COST_SOURCE_ID, USAGE_SOURCE_ID


DAILY_TIMEOUT_SECONDS = 30
MAX_DAILY_EXPORT_BYTES = 16 * 1024 * 1024
MAX_DAILY_DAYS = 5000
MAX_DAILY_MODELS_PER_DAY = 4096
MAX_DAILY_FIELDS = 64
MAX_DAILY_LABEL_LENGTH = 4096
DAILY_SOURCE_ID = DAILY_ACTIVITY_SOURCE_ID
_AUTHORITATIVE_QUALITIES = frozenset({"direct", "authoritative"})
OPENUSAGE_CATALOG_PROVIDER_ID = "openusage_catalog"
OPENUSAGE_CATALOG_SOURCE_ID = "openusage.detect"
OPENUSAGE_CATALOG_CADENCE = timedelta(hours=24)


class OpenUsageCatalogMonitor:
    """Persist only sanitized OpenUsage compatibility facts on a 24h cadence."""

    def __init__(
        self,
        store: ActivityStore,
        discovery: OpenUsageCatalogDiscovery | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.discovery = discovery or OpenUsageCatalogDiscovery(clock=clock)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _schedule(
        self, current: datetime
    ) -> tuple[bool, datetime | None]:
        try:
            status = next(
                (
                    row
                    for row in self.store.source_statuses()
                    if row.provider_id == OPENUSAGE_CATALOG_PROVIDER_ID
                    and row.source_id == OPENUSAGE_CATALOG_SOURCE_ID
                ),
                None,
            )
            if status is None:
                return True, None
            attempted = datetime.fromisoformat(
                status.last_attempt_at.replace("Z", "+00:00")
            )
            if current < attempted:
                return True, attempted
            return current - attempted >= OPENUSAGE_CATALOG_CADENCE, None
        except Exception:
            # A malformed operational timestamp must not be echoed or trusted.
            return True, None

    def maybe_run(self) -> CatalogDiagnostic | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            current = self.clock().astimezone(timezone.utc)
            due, rollback_attempt = self._schedule(current)
            if not due:
                return None
            try:
                result = self.discovery.run()
            except Exception:
                result = CatalogDiagnostic(
                    "invalid_detect_output", len(EXPECTED_PROVIDER_IDS), 0, 0, 0, current
                )
            try:
                if result.outcome == "ok":
                    self.store.record_source_success(
                        OPENUSAGE_CATALOG_PROVIDER_ID,
                        OPENUSAGE_CATALOG_SOURCE_ID,
                        current,
                        freshness_seconds=int(OPENUSAGE_CATALOG_CADENCE.total_seconds() * 2),
                        replace_if_last_attempt_at=rollback_attempt,
                    )
                else:
                    self.store.record_source_status(
                        OPENUSAGE_CATALOG_PROVIDER_ID,
                        OPENUSAGE_CATALOG_SOURCE_ID,
                        "temporarily_unavailable",
                        current,
                        result.error_code
                        or f"invalid_detect_output_e{len(EXPECTED_PROVIDER_IDS)}_a0_m0_x0",
                        replace_if_last_attempt_at=rollback_attempt,
                    )
            except Exception:
                pass
            return result
        finally:
            self._lock.release()


@dataclass(frozen=True)
class DailyImportResult:
    ok: bool
    rows: tuple[DailyUsageRow, ...]
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


class OpenUsageDailyImporter:
    def __init__(
        self,
        *,
        openusage_path: str | None = None,
        runner=None,
        environment: dict[str, str] | None = None,
        path_exists: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        codex_attribution: CodexAttributionResolver | None = None,
    ) -> None:
        self.openusage_path = openusage_path or resolve_openusage_path()
        self.runner = runner
        self.environment = environment
        self.path_exists = path_exists
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.codex_attribution = codex_attribution or CodexAttributionResolver()

    @staticmethod
    def _valid_request(provider_id: str, since: date, until: date) -> bool:
        return (
            isinstance(provider_id, str)
            and ID_PATTERN.fullmatch(provider_id) is not None
            and isinstance(since, date)
            and not isinstance(since, datetime)
            and isinstance(until, date)
            and not isinstance(until, datetime)
            and since <= until
        )

    @staticmethod
    def _token(raw: dict, field: str, *, nullable: bool = False) -> int | None:
        value = raw.get(field)
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid token field")
        return value

    @staticmethod
    def _canonical_model_id(raw_model_id: str) -> str:
        if raw_model_id == "(unknown)":
            return "unknown"
        try:
            return canonical_model_id(raw_model_id)
        except InvalidModelID as error:
            raise ValueError("invalid model") from error

    @staticmethod
    def _merge_rows(first: DailyUsageRow, second: DailyUsageRow) -> DailyUsageRow:
        if first.cost_amount is None or second.cost_amount is None:
            cost_amount = None
        else:
            cost_amount = str(
                Decimal(first.cost_amount or "0") + Decimal(second.cost_amount or "0")
            )
        if first.reasoning_tokens is None or second.reasoning_tokens is None:
            reasoning_tokens = None
        else:
            reasoning_tokens = (first.reasoning_tokens or 0) + (
                second.reasoning_tokens or 0
            )
        return DailyUsageRow(
            day=first.day,
            provider_id=first.provider_id,
            model_id=first.model_id,
            input_tokens=first.input_tokens + second.input_tokens,
            output_tokens=first.output_tokens + second.output_tokens,
            cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
            cache_creation_tokens=(
                first.cache_creation_tokens + second.cache_creation_tokens
            ),
            reasoning_tokens=reasoning_tokens,
            total_tokens=first.total_tokens + second.total_tokens,
            cost_amount=cost_amount,
            cost_currency="USD" if cost_amount is not None else None,
            cost_basis="price_table_estimated" if cost_amount is not None else None,
            quality=first.quality if first.quality == second.quality else "derived",
            imported_at=first.imported_at,
        )

    def _parse(
        self,
        payload: object,
        provider_id: str,
        since: date,
        until: date,
    ) -> tuple[DailyUsageRow, ...]:
        if not isinstance(payload, dict) or payload.get("kind") != "daily":
            raise LookupError("invalid envelope")
        raw_days = payload.get("rows")
        if not isinstance(raw_days, list):
            raise LookupError("invalid envelope")
        if len(raw_days) > MAX_DAILY_DAYS:
            raise ValueError("too many daily rows")
        imported_at = self.clock().astimezone(timezone.utc).isoformat()
        parsed: dict[tuple[str, str], DailyUsageRow] = {}
        raw_identities: set[tuple[str, str]] = set()
        for raw_day in raw_days:
            if not isinstance(raw_day, dict) or len(raw_day) > MAX_DAILY_FIELDS:
                raise ValueError("invalid day")
            if any(
                not isinstance(field, str) or len(field) > MAX_DAILY_LABEL_LENGTH
                for field in raw_day
            ):
                raise ValueError("invalid day field")
            raw_day_key = raw_day.get("key")
            if not isinstance(raw_day_key, str):
                raise ValueError("invalid day")
            try:
                day = date.fromisoformat(raw_day_key)
            except ValueError as error:
                raise ValueError("invalid day") from error
            if day.isoformat() != raw_day_key:
                raise ValueError("invalid day")
            if not since <= day <= until:
                continue
            models = raw_day.get("model_breakdown")
            if (
                not isinstance(models, list)
                or len(models) > MAX_DAILY_MODELS_PER_DAY
            ):
                raise ValueError("invalid model breakdown")
            for raw_model in models:
                if not isinstance(raw_model, dict) or len(raw_model) > MAX_DAILY_FIELDS:
                    raise ValueError("invalid model")
                if any(
                    not isinstance(field, str) or len(field) > MAX_DAILY_LABEL_LENGTH
                    for field in raw_model
                ):
                    raise ValueError("invalid model field")
                raw_model_id = raw_model.get("key")
                if (
                    not isinstance(raw_model_id, str)
                    or not raw_model_id
                    or len(raw_model_id) > MAX_DAILY_LABEL_LENGTH
                ):
                    raise ValueError("invalid model")
                model_id = self._canonical_model_id(raw_model_id)
                identity = (raw_day_key, model_id)
                raw_identity = (raw_day_key, raw_model_id)
                if raw_identity in raw_identities:
                    raise ValueError("duplicate model")
                raw_identities.add(raw_identity)
                quality = raw_model.get("quality")
                if quality not in _AUTHORITATIVE_QUALITIES:
                    quality = "derived"
                cost = raw_model.get("cost_usd")
                if (
                    cost is not None
                    and (
                        isinstance(cost, bool)
                        or not isinstance(cost, (int, float))
                        or not math.isfinite(cost)
                        or cost < 0
                    )
                ):
                    raise ValueError("invalid cost")
                row = DailyUsageRow(
                        day=raw_day_key,
                        provider_id=provider_id,
                        model_id=model_id,
                        input_tokens=self._token(raw_model, "input_tokens"),
                        output_tokens=self._token(raw_model, "output_tokens"),
                        cache_read_tokens=self._token(raw_model, "cache_read_tokens"),
                        cache_creation_tokens=self._token(
                            raw_model, "cache_creation_tokens"
                        ),
                        reasoning_tokens=self._token(
                            raw_model, "reasoning_tokens", nullable=True
                        ),
                        total_tokens=self._token(raw_model, "total_tokens"),
                        cost_amount=None if cost is None else str(cost),
                        cost_currency=None if cost is None else "USD",
                        cost_basis=None if cost is None else "price_table_estimated",
                        quality=quality,
                        imported_at=imported_at,
                    )
                if identity in parsed:
                    if model_id != "unknown":
                        raise ValueError("canonical model collision")
                    parsed[identity] = self._merge_rows(parsed[identity], row)
                else:
                    parsed[identity] = row
        return tuple(parsed.values())

    def _repair_codex_unknown(
        self,
        rows: tuple[DailyUsageRow, ...],
        since: date,
        until: date,
    ) -> tuple[DailyUsageRow, ...]:
        if not any(row.model_id == "unknown" for row in rows):
            return rows
        targets = self.codex_attribution.target_models(since, until)
        repaired: dict[tuple[str, str], DailyUsageRow] = {}
        for row in rows:
            model_id = targets.get(row.day) if row.model_id == "unknown" else None
            candidate = replace(row, model_id=model_id) if model_id else row
            identity = (candidate.day, candidate.model_id)
            if identity in repaired:
                repaired[identity] = self._merge_rows(repaired[identity], candidate)
            else:
                repaired[identity] = candidate
        return tuple(repaired.values())

    def fetch(
        self, provider_id: str, since: date, until: date
    ) -> DailyImportResult:
        if not self._valid_request(provider_id, since, until):
            return DailyImportResult(False, (), "invalid_request")
        command = [
            self.openusage_path,
            "daily",
            "--json",
            "--breakdown",
            "--offline",
            "--provider",
            provider_id,
            "--since",
            since.isoformat(),
            "--until",
            until.isoformat(),
        ]
        try:
            runner = self.runner or run_bounded
            options = {}
            if self.runner is None:
                options = {
                    "stdout_limit": MAX_DAILY_EXPORT_BYTES,
                    "stderr_limit": 64 * 1024,
                }
            completed = runner(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DAILY_TIMEOUT_SECONDS,
                env=child_subprocess_environment(self.environment, self.path_exists),
                **options,
            )
        except BoundedProcessError as error:
            code = "timeout" if error.code == "timeout" else "output_too_large"
            return DailyImportResult(False, (), code)
        except subprocess.TimeoutExpired:
            return DailyImportResult(False, (), "timeout")
        except OSError:
            return DailyImportResult(False, (), "start_failed")
        except Exception:
            return DailyImportResult(False, (), "runner_failed")
        if completed.returncode != 0:
            return DailyImportResult(False, (), "command_failed")
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            return DailyImportResult(False, (), "invalid_json")
        try:
            rows = self._parse(payload, provider_id, since, until)
        except LookupError:
            return DailyImportResult(False, (), "invalid_envelope")
        except Exception:
            return DailyImportResult(False, (), "invalid_payload")
        if provider_id == "codex":
            rows = self._repair_codex_unknown(rows, since, until)
        return DailyImportResult(True, rows)


class ActivityCollector:
    def __init__(
        self,
        store: ActivityStore,
        importer: OpenUsageDailyImporter,
        *,
        official_importers: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        local_timezone=None,
    ) -> None:
        self.store = store
        self.importer = importer
        self.official_importers = dict(official_importers or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        )
        self._lock = threading.Lock()

    @staticmethod
    def _rows_match_scope(
        rows: Iterable[DailyUsageRow], provider_id: str, since: date, until: date,
        account_ref: str = "",
    ) -> bool:
        try:
            return all(
                row.provider_id == provider_id
                and row.account_ref == account_ref
                and since <= date.fromisoformat(row.day) <= until
                for row in rows
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _source_id(importer: Any, attribute: str, fallback: str | None) -> str | None:
        missing = object()
        candidate = vars(importer).get(attribute, missing)
        if candidate is missing:
            candidate = getattr(type(importer), attribute, missing)
        if candidate is None:
            return None
        if isinstance(candidate, str) and candidate and len(candidate) <= 128:
            return candidate
        return fallback

    @staticmethod
    def _account_ref(importer: Any) -> str:
        missing = object()
        candidate = vars(importer).get("account_ref", missing)
        if candidate is missing:
            candidate = getattr(type(importer), "account_ref", missing)
        if candidate is missing:
            return ""
        if (
            isinstance(candidate, str)
            and (not candidate or ID_PATTERN.fullmatch(candidate) is not None)
        ):
            return candidate
        raise ValueError("importer account scope is invalid")

    @staticmethod
    def _success_bounds_match(
        result: UsageImportSuccess,
        requested_since: date,
        requested_until: date,
    ) -> bool:
        return (
            isinstance(result.since, date)
            and not isinstance(result.since, datetime)
            and isinstance(result.until, date)
            and not isinstance(result.until, datetime)
            and requested_since <= result.since <= result.until <= requested_until
        )

    def _safe_source_failure(
        self,
        provider_id: str,
        error_code: str,
        attempted_at: datetime,
        source_id: str = DAILY_SOURCE_ID,
    ) -> None:
        try:
            self.store.record_source_failure(
                provider_id, source_id, error_code, attempted_at
            )
        except Exception:
            pass

    def _refresh_official_costs(
        self,
        provider_id: str,
        importer: Any,
        source_id: str,
        today: date,
        attempted_at: datetime,
    ) -> None:
        try:
            account_ref = self._account_ref(importer)
        except ValueError:
            self._safe_source_failure(
                provider_id, "invalid_import_scope", attempted_at, source_id
            )
            return
        try:
            has_cost_history = self.store.has_cost_history(provider_id, account_ref)
        except Exception:
            has_cost_history = True
        cost_since = today - timedelta(days=6 if has_cost_history else 364)
        try:
            official_cost = importer.fetch_costs(cost_since, today)
        except Exception:
            official_cost = ImportFailure("import_failed")
        if isinstance(official_cost, CostImportSuccess):
            try:
                self.store.commit_cost_import_success(
                    provider_id,
                    source_id,
                    cost_since,
                    today,
                    official_cost.rows,
                    attempted_at,
                    account_ref=account_ref,
                )
            except Exception:
                self._safe_source_failure(
                    provider_id, "persistence_failed", attempted_at, source_id
                )
            return
        error_code = (
            official_cost.error_code
            if isinstance(official_cost, ImportFailure)
            else "invalid_import_result"
        )
        self._safe_source_failure(provider_id, error_code, attempted_at, source_id)

    @staticmethod
    def _provider_instance(
        card: ProviderCard, observed_at: datetime
    ) -> ProviderInstance | None:
        if (
            card.provider_id == "openusage"
            or not card.family_id
            or not card.credential_source
            or not card.source_kind
        ):
            return None
        category = (
            "local_tool" if card.category.value == "local" else card.category.value
        )
        return ProviderInstance(
            provider_id=card.provider_id,
            family_id=card.family_id,
            display_name=card.name,
            category=category,
            credential_source=card.credential_source,
            source_kind=card.source_kind,
            observed_at=observed_at.isoformat(),
        )

    @staticmethod
    def _quota_observation(card: ProviderCard) -> QuotaObservation | None:
        remaining = card.remaining_percent
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or not 0 <= remaining <= 100
        ):
            return None
        observed_at = card.refreshed_at
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise ValueError("quota observation time must include a timezone")
        reset = card.resets_at
        return QuotaObservation(
            record_id=(
                f"{card.provider_id}."
                f"{card.account_ref + '.' if card.account_ref else ''}subscription"
            ),
            observed_at=observed_at.astimezone(timezone.utc).isoformat(),
            provider_id=card.provider_id,
            account_ref=card.account_ref,
            quota_name="Subscription",
            unit="percent",
            used=str(100 - float(remaining)),
            quota_limit="100",
            remaining=str(float(remaining)),
            remaining_ratio=float(remaining) / 100,
            resets_at=None if reset is None else reset.astimezone(timezone.utc).isoformat(),
            period_start=None,
            period_end=None,
            state=state_from_card(card.status, card.stale).value,
            quality="direct",
            stale=card.stale,
            source_id="current.quota",
            quota_window="subscription",
            applies_to_kind="account",
            applies_to_model_ids=(),
        )

    @staticmethod
    def _tracks_current_quota(card: ProviderCard) -> bool:
        try:
            descriptor = registry.require(card.family_id or card.provider_id)
        except KeyError:
            return True
        return MetricFamily.SUBSCRIPTION_QUOTA in descriptor.metric_families

    def _persist_current_quotas(
        self, overview: Overview, attempted_at: datetime,
        explicit_provider_ids: frozenset[str] = frozenset(),
    ) -> None:
        for card in overview.cards:
            if card.provider_id in explicit_provider_ids:
                continue
            if not self._tracks_current_quota(card):
                try:
                    self.store.delete_source_status(
                        card.provider_id, "current.quota", attempted_at
                    )
                except Exception:
                    pass
                continue
            try:
                observation = self._quota_observation(card)
            except (TypeError, ValueError):
                try:
                    self.store.record_source_status(
                        card.provider_id,
                        "current.quota",
                        "temporarily_unavailable",
                        attempted_at,
                        "invalid_observation_time",
                    )
                except Exception:
                    pass
                continue
            if observation is None:
                state = state_from_card(card.status, card.stale)
                if state.value != "ok":
                    try:
                        self.store.record_source_status(
                            card.provider_id,
                            "current.quota",
                            state.value,
                            attempted_at,
                            "quota_unavailable",
                        )
                    except Exception:
                        pass
                continue
            try:
                self.store.record_quota(observation)
            except Exception:
                try:
                    self.store.record_source_status(
                        card.provider_id,
                        "current.quota",
                        "temporarily_unavailable",
                        attempted_at,
                        "quota_persistence_failed",
                    )
                except Exception:
                    pass
                continue
            state = state_from_card(card.status, card.stale)
            try:
                if state.value == "ok":
                    self.store.record_source_success(
                        card.provider_id, "current.quota", attempted_at
                    )
                else:
                    self.store.record_source_status(
                        card.provider_id,
                        "current.quota",
                        state.value,
                        attempted_at,
                        "quota_unavailable",
                    )
            except Exception:
                pass

    @staticmethod
    def _provider_ids(
        overview: Overview, official_importers: Mapping[str, Any]
    ) -> tuple[str, ...]:
        return tuple(sorted(
            {
                card.provider_id
                for card in overview.cards
                if card.provider_id != "openusage"
            }
            | set(official_importers)
        ))

    def _publish_provider_instances(
        self, overview: Overview, attempted_at: datetime
    ) -> None:
        for card in sorted(overview.cards, key=lambda item: item.provider_id):
            try:
                instance = self._provider_instance(card, attempted_at)
                if instance is not None:
                    self.store.upsert_provider_instance(instance)
            except Exception:
                pass

    def _refresh_quota_sources(
        self,
        overview: Overview,
        attempted_at: datetime,
        quota_results: tuple[tuple[str, str, object], ...],
    ) -> None:
        # Current capacity publishes before slower history sources and remains an
        # independent failure domain.
        explicit_provider_ids: set[str] = set()
        for provider_id, source_id, result in quota_results:
            explicit_provider_ids.add(provider_id)
            if isinstance(result, QuotaFetchSuccess):
                try:
                    if any(
                        observation.provider_id != provider_id
                        or observation.source_id != source_id
                        for observation in result.observations
                    ):
                        raise ValueError("quota result scope mismatch")
                    for observation in result.observations:
                        self.store.record_quota(observation)
                    self.store.record_source_success(
                        provider_id, source_id, attempted_at
                    )
                except Exception:
                    self._safe_source_failure(
                        provider_id, "persistence_failed", attempted_at, source_id
                    )
            elif isinstance(result, QuotaFetchFailure):
                self._safe_source_failure(
                    provider_id, result.error_code, attempted_at, source_id
                )
            else:
                self._safe_source_failure(
                    provider_id, "invalid_import_result", attempted_at, source_id
                )
        try:
            self._persist_current_quotas(
                overview, attempted_at, frozenset(explicit_provider_ids)
            )
        except Exception:
            pass

    def _refresh_usage_sources(
        self,
        overview: Overview,
        provider_ids: tuple[str, ...],
        today: date,
        attempted_at: datetime,
    ) -> None:
        for provider_id in provider_ids:
            official = self.official_importers.get(provider_id)
            use_openusage_fallback = official is None
            if official is not None:
                try:
                    account_ref = self._account_ref(official)
                except ValueError:
                    self._safe_source_failure(
                        provider_id, "invalid_import_scope", attempted_at
                    )
                    continue
                usage_source_id = self._source_id(
                    official, "usage_source_id", USAGE_SOURCE_ID
                )
                if usage_source_id is None:
                    # Cost-only bindings deliberately leave Token history to the
                    # shared OpenUsage importer when it is available.
                    official = None
                    use_openusage_fallback = True
                    usage_source_id = USAGE_SOURCE_ID
                if official is None:
                    pass
                else:
                    try:
                        had_official_usage = self.store.has_source_success(
                            provider_id, usage_source_id
                        )
                    except Exception:
                        had_official_usage = True
                    usage_since = today - timedelta(
                        days=6 if had_official_usage else 364
                    )
                    try:
                        official_usage = official.fetch_usage(usage_since, today)
                    except Exception:
                        official_usage = ImportFailure("import_failed")
                    if isinstance(official_usage, UsageImportSuccess):
                        if not self._success_bounds_match(
                            official_usage, usage_since, today
                        ) or not self._rows_match_scope(
                            official_usage.rows,
                            provider_id,
                            official_usage.since,
                            official_usage.until,
                            account_ref,
                        ):
                            self._safe_source_failure(
                                provider_id, "invalid_import_rows", attempted_at,
                                usage_source_id,
                            )
                        else:
                            try:
                                self.store.commit_usage_import_success(
                                    provider_id, usage_source_id,
                                    official_usage.since, official_usage.until,
                                    official_usage.rows, attempted_at,
                                    account_ref=account_ref,
                                )
                            except Exception:
                                self._safe_source_failure(
                                    provider_id, "persistence_failed", attempted_at,
                                    usage_source_id,
                                )
                        use_openusage_fallback = False
                    else:
                        error_code = (
                            official_usage.error_code
                            if isinstance(official_usage, ImportFailure)
                            else "invalid_import_result"
                        )
                        self._safe_source_failure(
                            provider_id, error_code, attempted_at, usage_source_id
                        )
                        use_openusage_fallback = not had_official_usage

            if not use_openusage_fallback:
                continue
            try:
                since = today - timedelta(
                    days=0 if self.store.has_daily_history(provider_id) else 364
                )
                result = self.importer.fetch(provider_id, since, today)
            except Exception:
                self._safe_source_failure(provider_id, "import_failed", attempted_at)
                continue
            if not result.ok:
                self._safe_source_failure(
                    provider_id, result.error_code or "import_failed", attempted_at
                )
                continue
            if not self._rows_match_scope(result.rows, provider_id, since, today):
                self._safe_source_failure(
                    provider_id, "invalid_import_rows", attempted_at
                )
                continue
            try:
                self.store.replace_provider_days(provider_id, since, today, result.rows)
                self.store.record_source_success(
                    provider_id, DAILY_SOURCE_ID, attempted_at
                )
            except Exception:
                self._safe_source_failure(
                    provider_id, "persistence_failed", attempted_at
                )

    def _refresh_cost_sources(
        self, provider_ids: tuple[str, ...], today: date, attempted_at: datetime
    ) -> None:
        for provider_id in provider_ids:
            official = self.official_importers.get(provider_id)
            if official is None:
                continue
            cost_source_id = self._source_id(
                official, "cost_source_id", COST_SOURCE_ID
            )
            if cost_source_id is None:
                continue
            self._refresh_official_costs(
                provider_id, official, cost_source_id, today, attempted_at
            )

    def refresh(
        self,
        overview: Overview,
        *,
        quota_results: tuple[tuple[str, str, object], ...] = (),
    ) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            current = self.clock()
            attempted_at = current.astimezone(timezone.utc)
            today = current.astimezone(self.local_timezone).date()
            provider_ids = self._provider_ids(overview, self.official_importers)
            self._publish_provider_instances(overview, attempted_at)
            self._refresh_quota_sources(overview, attempted_at, quota_results)
            self._refresh_usage_sources(
                overview, provider_ids, today, attempted_at
            )
            self._refresh_cost_sources(provider_ids, today, attempted_at)
            try:
                self.store.apply_retention(730, attempted_at)
            except Exception:
                pass
            return True
        finally:
            self._lock.release()
