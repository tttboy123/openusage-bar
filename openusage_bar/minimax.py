from __future__ import annotations

import time as time_module
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from .activity_store import DailyUsageRow
from .config import MiniMaxConfig
from .keychain import MacOSKeychain
from .model_ids import InvalidModelID, canonical_model_id
from .models import Category, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited
from .providers.contracts import ImportFailure, UsageImportResult, UsageImportSuccess
from .providers.contracts import QuotaFetchFailure, QuotaFetchSuccess
from .providers.quota import percent_observation


MINIMAX_ENDPOINT = "https://www.minimaxi.com/v1/token_plan/remains"
MINIMAX_BILLING_ENDPOINT = "https://www.minimaxi.com/account/amount"
MINIMAX_BILLING_SOURCE_ID = "minimax.billing"
MAX_BILLING_HISTORY_DAYS = 365
MAX_BILLING_PAGES = 100
MAX_BILLING_PAGE_SIZE = 100
MAX_TOKEN_VALUE = 9_223_372_036_854_775_807


class MiniMaxParseError(ValueError):
    pass


def parse_minimax_quota_observations(
    config: MiniMaxConfig, payload: dict[str, Any], now: datetime
) -> QuotaFetchSuccess | QuotaFetchFailure:
    base = payload.get("base_resp")
    rows = payload.get("model_remains")
    if (
        not isinstance(base, dict) or base.get("status_code") != 0
        or not isinstance(rows, list)
    ):
        return QuotaFetchFailure("invalid_response")

    def percentage(value: Any) -> float | None:
        return (
            float(value)
            if isinstance(value, (int, float))
            and not isinstance(value, bool) and 0 <= value <= 100
            else None
        )

    def reset_at(row: dict[str, Any], *keys: str) -> datetime | None:
        for key in keys:
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value > 0:
                    try:
                        return datetime.fromtimestamp(
                            float(value) / 1000, tz=timezone.utc
                        )
                    except (OverflowError, OSError, ValueError):
                        return None
        return None

    def interval_reset(row: dict[str, Any]) -> datetime | None:
        reset = reset_at(row, "end_time")
        if reset is not None:
            return reset
        duration = row.get("remains_time")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            return now + timedelta(milliseconds=float(duration))
        return None

    observations = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("model_name") or "general").strip()
        if not name:
            continue
        if name.casefold() == "general":
            applies_to_kind = "subscription"
            model_ids: tuple[str, ...] = ()
        else:
            try:
                model_ids = (canonical_model_id(name),)
            except InvalidModelID:
                continue
            applies_to_kind = "model"

        five_percent = percentage(row.get("current_interval_remaining_percent"))
        total = row.get("current_interval_total_count")
        remaining = row.get("current_interval_usage_count")
        if (
            five_percent is None and isinstance(total, int)
            and not isinstance(total, bool) and total > 0
            and isinstance(remaining, int) and not isinstance(remaining, bool)
            and 0 <= remaining <= total
        ):
            five_percent = remaining / total * 100
        if five_percent is not None:
            observations.append(percent_observation(
                provider_id=config.provider_id, source_id="minimax.coding_plan",
                quota_name=f"{name} 5h", quota_window="five_hour",
                remaining_percent=five_percent,
                resets_at=interval_reset(row), observed_at=now,
                applies_to_kind=applies_to_kind,
                applies_to_model_ids=model_ids,
            ))

        weekly_percent = percentage(row.get("current_weekly_remaining_percent"))
        if weekly_percent is not None:
            observations.append(percent_observation(
                provider_id=config.provider_id, source_id="minimax.coding_plan",
                quota_name=f"{name} Weekly", quota_window="weekly",
                remaining_percent=weekly_percent,
                resets_at=reset_at(
                    row, "current_weekly_end_time", "weekly_end_time"
                ),
                observed_at=now, applies_to_kind=applies_to_kind,
                applies_to_model_ids=model_ids,
            ))
    if not observations:
        return QuotaFetchFailure("quota_unavailable")
    return QuotaFetchSuccess(tuple(sorted(
        observations,
        key=lambda value: (
            value.applies_to_model_ids, value.quota_window, value.quota_name
        ),
    )))


class _MiniMaxBillingResponseError(ValueError):
    pass


class MiniMaxBillingImporter:
    """Import provider-reported daily model tokens from MiniMax's web billing feed.

    The feed is used by the MiniMax platform UI but is not a documented public API.
    It is therefore isolated behind a distinct source ID and always remains
    replaceable by a future official/OpenUsage adapter.
    """

    usage_source_id = MINIMAX_BILLING_SOURCE_ID
    cost_source_id = None

    def __init__(
        self,
        config: MiniMaxConfig,
        keychain: MacOSKeychain,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        local_timezone=timezone(timedelta(hours=8)),
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time_module.monotonic
        self.local_timezone = local_timezone

    def fetch_usage(self, since: date, until: date) -> UsageImportResult:
        request = self._request_bounds(since, until)
        if isinstance(request, ImportFailure):
            return request
        covered_since, covered_until, start_time, end_time = request
        secret = self._secret()
        if secret is None:
            return ImportFailure("auth_required")
        try:
            rows = self._fetch_rows(start_time, end_time, secret)
            return UsageImportSuccess(covered_since, covered_until, rows)
        except AuthenticationRequired:
            return ImportFailure("auth_rejected")
        except RateLimited:
            return ImportFailure("rate_limited")
        except (_MiniMaxBillingResponseError, InvalidModelID, TypeError, ValueError, OverflowError):
            return ImportFailure("invalid_response")
        except NetworkError:
            return ImportFailure("network_error")

    def _secret(self) -> str | None:
        try:
            secret = self.keychain.get(self.config.provider_id)
        except Exception:
            return None
        return secret if isinstance(secret, str) and secret else None

    def _request_bounds(
        self, since: date, until: date
    ) -> tuple[date, date, int, int] | ImportFailure:
        if (
            not isinstance(since, date)
            or isinstance(since, datetime)
            or not isinstance(until, date)
            or isinstance(until, datetime)
            or since > until
            or (until - since).days + 1 > MAX_BILLING_HISTORY_DAYS
        ):
            return ImportFailure("invalid_request")
        current = self.clock()
        if not isinstance(current, datetime):
            return ImportFailure("invalid_request")
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=timezone.utc)
        available_until = min(
            until,
            current.astimezone(self.local_timezone).date() - timedelta(days=1),
        )
        if since > available_until:
            return ImportFailure("not_available_yet")
        start = datetime.combine(since, datetime.min.time(), tzinfo=self.local_timezone)
        end = datetime.combine(
            available_until + timedelta(days=1),
            datetime.min.time(),
            tzinfo=self.local_timezone,
        )
        return since, available_until, int(start.timestamp()), int(end.timestamp())

    def _fetch_rows(
        self, start_time: int, end_time: int, secret: str
    ) -> tuple[DailyUsageRow, ...]:
        totals: dict[tuple[str, str], list[int]] = {}
        imported_at = self._imported_at()
        started_at = self.monotonic()
        previous_timestamp: int | None = None
        records_seen = 0
        expected_total: int | None = None
        for page in range(1, MAX_BILLING_PAGES + 1):
            if self.monotonic() - started_at > 60:
                raise _MiniMaxBillingResponseError("operation deadline exceeded")
            payload = self.client.get_json(
                f"{MINIMAX_BILLING_ENDPOINT}?{urlencode({'page': page, 'limit': MAX_BILLING_PAGE_SIZE, 'aggregate': 'false'})}",
                {"Authorization": f"Bearer {secret}"},
            )
            base = payload.get("base_resp")
            if not isinstance(base, dict) or base.get("status_code") != 0:
                raise _MiniMaxBillingResponseError("billing request rejected")
            records = payload.get("charge_records")
            total = payload.get("total_cnt")
            if (
                not isinstance(records, list)
                or len(records) > MAX_BILLING_PAGE_SIZE
                or isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
            ):
                raise _MiniMaxBillingResponseError("invalid billing page")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise _MiniMaxBillingResponseError("billing total changed during import")
            reached_before_range = False
            for raw in records:
                timestamp, day, model, input_tokens, output_tokens, total_tokens = (
                    self._record(raw)
                )
                if previous_timestamp is not None and timestamp > previous_timestamp:
                    raise _MiniMaxBillingResponseError("billing records are not ordered")
                previous_timestamp = timestamp
                records_seen += 1
                if records_seen > total:
                    raise _MiniMaxBillingResponseError("billing count exceeds total")
                if timestamp < start_time:
                    reached_before_range = True
                    continue
                if timestamp >= end_time or total_tokens == 0:
                    continue
                aggregate = totals.setdefault((day, model), [0, 0, 0])
                aggregate[0] += input_tokens
                aggregate[1] += output_tokens
                aggregate[2] += total_tokens
                if any(value > MAX_TOKEN_VALUE for value in aggregate):
                    raise _MiniMaxBillingResponseError("token sum overflow")
            if reached_before_range or records_seen == total:
                return self._rows(totals, imported_at)
            if not records or len(records) < MAX_BILLING_PAGE_SIZE:
                raise _MiniMaxBillingResponseError("incomplete billing pagination")
        raise _MiniMaxBillingResponseError("billing page limit exceeded")

    def _record(self, raw: Any) -> tuple[int, str, str, int, int, int]:
        if not isinstance(raw, dict):
            raise _MiniMaxBillingResponseError("invalid billing record")
        timestamp = raw.get("created_at")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
        ):
            raise _MiniMaxBillingResponseError("invalid billing timestamp")
        try:
            day = datetime.fromtimestamp(timestamp, tz=self.local_timezone).date().isoformat()
        except (OSError, ValueError, OverflowError):
            raise _MiniMaxBillingResponseError("invalid billing timestamp") from None
        model = canonical_model_id(raw.get("model"))
        input_tokens = self._token(raw.get("consume_input_token"))
        output_tokens = self._token(raw.get("consume_output_token"))
        total_tokens = self._token(raw.get("consume_token"))
        if total_tokens != input_tokens + output_tokens:
            raise _MiniMaxBillingResponseError("billing token total mismatch")
        return timestamp, day, model, input_tokens, output_tokens, total_tokens

    @staticmethod
    def _token(value: Any) -> int:
        if isinstance(value, bool):
            raise _MiniMaxBillingResponseError("invalid token value")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            result = int(value)
        else:
            raise _MiniMaxBillingResponseError("invalid token value")
        if not 0 <= result <= MAX_TOKEN_VALUE:
            raise _MiniMaxBillingResponseError("invalid token value")
        return result

    def _rows(
        self, totals: dict[tuple[str, str], list[int]], imported_at: str
    ) -> tuple[DailyUsageRow, ...]:
        return tuple(
            DailyUsageRow(
                day=day,
                provider_id=self.config.provider_id,
                model_id=model,
                input_tokens=values[0],
                output_tokens=values[1],
                cache_read_tokens=0,
                cache_creation_tokens=0,
                reasoning_tokens=None,
                total_tokens=values[2],
                cost_amount=None,
                cost_currency=None,
                cost_basis=None,
                quality="direct",
                imported_at=imported_at,
            )
            for (day, model), values in sorted(totals.items())
        )

    def _imported_at(self) -> str:
        current = self.clock()
        if not isinstance(current, datetime):
            raise _MiniMaxBillingResponseError("invalid clock")
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()


class MiniMaxCodingPlanAdapter:
    def __init__(
        self,
        config: MiniMaxConfig,
        keychain: MacOSKeychain,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime],
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.client = client
        self.clock = clock
        self.last_quota_result = QuotaFetchFailure("not_collected")

    @staticmethod
    def parse(config: MiniMaxConfig, payload: dict[str, Any], now: datetime) -> ProviderCard:
        base = payload.get("base_resp")
        if not isinstance(base, dict) or base.get("status_code") != 0:
            status_code = base.get("status_code") if isinstance(base, dict) else "unknown"
            raw_message = base.get("status_msg") if isinstance(base, dict) else None
            message = " ".join(str(raw_message).split())[:160] if raw_message else "request rejected"
            raise MiniMaxParseError(f"MiniMax {status_code}: {message}")
        remains = payload.get("model_remains")
        if not isinstance(remains, list) or not remains or not isinstance(remains[0], dict):
            raise MiniMaxParseError("MiniMax response has no model quota")
        def percentage(value: Any) -> float | None:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                return float(value)
            return None

        valid_models = []
        for candidate in remains:
            if not isinstance(candidate, dict):
                continue
            candidate_total = candidate.get("current_interval_total_count")
            candidate_remaining = candidate.get("current_interval_usage_count")
            count_is_valid = (
                isinstance(candidate_total, int)
                and not isinstance(candidate_total, bool)
                and candidate_total >= 0
                and isinstance(candidate_remaining, int)
                and not isinstance(candidate_remaining, bool)
                and 0 <= candidate_remaining <= candidate_total
            )
            if (
                count_is_valid
                or percentage(candidate.get("current_interval_remaining_percent")) is not None
                or percentage(candidate.get("current_weekly_remaining_percent")) is not None
            ):
                valid_models.append(candidate)
        general_models = [
            candidate
            for candidate in valid_models
            if str(candidate.get("model_name", "")).casefold() == "general"
        ]
        if general_models:
            model = general_models[0]
        elif valid_models:
            model = max(
                valid_models,
                key=lambda candidate: candidate.get("current_interval_total_count", 0)
                if isinstance(candidate.get("current_interval_total_count"), int)
                else 0,
            )
        else:
            raise MiniMaxParseError("MiniMax response has no active model quota")
        total = model.get("current_interval_total_count")
        remaining = model.get("current_interval_usage_count")
        remaining_percentage = percentage(model.get("current_interval_remaining_percent"))
        weekly_percentage = percentage(model.get("current_weekly_remaining_percent"))

        if remaining_percentage is not None:
            rounded_percentage = round(remaining_percentage)
            if isinstance(total, int) and not isinstance(total, bool) and total > 0:
                remaining_count = round(total * remaining_percentage / 100)
                primary = f"{remaining_count} / {total} remaining"
            else:
                primary = f"5h {rounded_percentage}% remaining"
        elif (
            isinstance(total, int)
            and not isinstance(total, bool)
            and total > 0
            and isinstance(remaining, int)
            and not isinstance(remaining, bool)
        ):
            primary = f"{remaining} / {total} remaining"
            remaining_percentage = remaining / total * 100
        else:
            raise MiniMaxParseError("MiniMax response has no usable current quota")

        reset_at: datetime | None = None
        end_time = model.get("end_time")
        if isinstance(end_time, (int, float)) and end_time > 0:
            reset_at = datetime.fromtimestamp(float(end_time) / 1000, tz=timezone.utc)
        else:
            duration = model.get("remains_time")
            if isinstance(duration, (int, float)) and duration >= 0:
                reset_at = now + timedelta(milliseconds=float(duration))

        model_name = model.get("model_name")
        detail = str(model_name) if model_name else "MiniMax Coding Plan"
        if weekly_percentage is not None:
            detail += f" · Weekly {round(weekly_percentage)}% remaining"
        return ProviderCard(
            provider_id=config.provider_id,
            name=config.name,
            category=Category.SUBSCRIPTION,
            status=ProviderStatus.OK,
            primary=primary,
            detail=detail,
            remaining_percent=remaining_percentage,
            resets_at=reset_at,
            source="MiniMax Coding Plan",
            refreshed_at=now,
            family_id="minimax",
            credential_source="minimax_builtin_api",
            source_kind="builtin_api",
        )

    def fetch(self) -> ProviderCard:
        now = self.clock()
        secret = self.keychain.get(self.config.provider_id)
        if not secret:
            self.last_quota_result = QuotaFetchFailure("auth_required")
            return self._error_card(ProviderStatus.AUTH, "Credential required", now)
        try:
            payload = self.client.get_json(
                MINIMAX_ENDPOINT,
                {
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
            )
            self.last_quota_result = parse_minimax_quota_observations(
                self.config, payload, now
            )
            return self.parse(self.config, payload, now)
        except AuthenticationRequired:
            self.last_quota_result = QuotaFetchFailure("auth_rejected")
            return self._error_card(ProviderStatus.AUTH, "Credential rejected", now)
        except RateLimited:
            self.last_quota_result = QuotaFetchFailure("rate_limited")
            return self._error_card(ProviderStatus.RATE_LIMITED, "Rate limited", now)
        except MiniMaxParseError as error:
            self.last_quota_result = QuotaFetchFailure("invalid_response")
            return self._error_card(ProviderStatus.ERROR, str(error), now)
        except (TypeError, ValueError, OverflowError):
            self.last_quota_result = QuotaFetchFailure("invalid_response")
            return self._error_card(ProviderStatus.ERROR, "MiniMax refresh failed", now)
        except NetworkError:
            self.last_quota_result = QuotaFetchFailure("network_error")
            return self._error_card(ProviderStatus.ERROR, "MiniMax refresh failed", now)

    def _error_card(self, status: ProviderStatus, error: str, now: datetime) -> ProviderCard:
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=Category.SUBSCRIPTION,
            status=status,
            primary=None,
            detail=error,
            remaining_percent=None,
            resets_at=None,
            source="MiniMax Coding Plan",
            refreshed_at=now,
            last_error=error,
            family_id="minimax",
            credential_source="minimax_builtin_api",
            source_kind="builtin_api",
        )
