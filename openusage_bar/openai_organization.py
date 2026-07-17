from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode

from .activity_store import DailyCostRow, DailyUsageRow
from .config import ID_PATTERN, OpenAIOrganizationConfig
from .keychain import MacOSKeychain
from .models import Category, ProviderCard, ProviderStatus
from .model_ids import InvalidModelID, canonical_model_id
from .network import (
    AuthenticationRequired,
    BoundedHTTPClient,
    NetworkError,
    RateLimited,
)


USAGE_ENDPOINT = "https://api.openai.com/v1/organization/usage/completions"
COSTS_ENDPOINT = "https://api.openai.com/v1/organization/costs"
USAGE_SOURCE_ID = "openai.organization.usage"
COST_SOURCE_ID = "openai.organization.costs"
MAX_HISTORY_DAYS = 365
MAX_PAGES = 64
MAX_RESULTS_PER_PAGE = 4096
MAX_LABEL_LENGTH = 4096


@dataclass(frozen=True)
class ImportFailure:
    error_code: str

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class UsageImportSuccess:
    since: date
    until: date
    rows: tuple[DailyUsageRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class CostImportSuccess:
    since: date
    until: date
    rows: tuple[DailyCostRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))

    @property
    def ok(self) -> bool:
        return True


UsageImportResult = UsageImportSuccess | ImportFailure
CostImportResult = CostImportSuccess | ImportFailure


class _InvalidResponse(ValueError):
    pass


class OpenAIOrganizationImporter:
    usage_source_id = USAGE_SOURCE_ID
    cost_source_id = COST_SOURCE_ID

    def __init__(
        self,
        config: OpenAIOrganizationConfig,
        keychain: MacOSKeychain,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time_module.monotonic

    def fetch_usage(self, since: date, until: date) -> UsageImportResult:
        request = self._request_bounds(since, until)
        if request is None:
            return ImportFailure("invalid_request")
        secret = self._secret()
        if secret is None:
            return ImportFailure("auth_required")
        start_time, end_time = request
        try:
            payloads = self._fetch_pages(
                USAGE_ENDPOINT,
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "bucket_width": "1d",
                    "limit": 31,
                    "group_by": "model",
                },
                secret,
            )
            rows = self._parse_usage_pages(payloads, start_time, end_time)
            return UsageImportSuccess(since, until, rows)
        except AuthenticationRequired:
            return ImportFailure("auth_rejected")
        except RateLimited:
            return ImportFailure("rate_limited")
        except (_InvalidResponse, TypeError, ValueError, OverflowError):
            return ImportFailure("invalid_response")
        except NetworkError:
            return ImportFailure("network_error")

    def fetch_costs(self, since: date, until: date) -> CostImportResult:
        request = self._request_bounds(since, until)
        if request is None:
            return ImportFailure("invalid_request")
        secret = self._secret()
        if secret is None:
            return ImportFailure("auth_required")
        start_time, end_time = request
        try:
            payloads = self._fetch_pages(
                COSTS_ENDPOINT,
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "bucket_width": "1d",
                    "limit": 180,
                },
                secret,
            )
            rows = self._parse_cost_pages(payloads, start_time, end_time)
            return CostImportSuccess(since, until, rows)
        except AuthenticationRequired:
            return ImportFailure("auth_rejected")
        except RateLimited:
            return ImportFailure("rate_limited")
        except (_InvalidResponse, TypeError, ValueError, OverflowError):
            return ImportFailure("invalid_response")
        except NetworkError:
            return ImportFailure("network_error")

    def _secret(self) -> str | None:
        try:
            secret = self.keychain.get(self.config.provider_id)
        except Exception:
            return None
        return secret if isinstance(secret, str) and secret else None

    @staticmethod
    def _request_bounds(since: date, until: date) -> tuple[int, int] | None:
        if (
            not isinstance(since, date)
            or isinstance(since, datetime)
            or not isinstance(until, date)
            or isinstance(until, datetime)
            or since > until
            or (until - since).days + 1 > MAX_HISTORY_DAYS
        ):
            return None
        start = datetime.combine(since, time.min, tzinfo=timezone.utc)
        end = datetime.combine(until + timedelta(days=1), time.min, tzinfo=timezone.utc)
        return int(start.timestamp()), int(end.timestamp())

    def _fetch_pages(
        self,
        endpoint: str,
        parameters: dict[str, int | str],
        secret: str,
    ) -> tuple[dict[str, Any], ...]:
        pages: list[dict[str, Any]] = []
        started_at = self.monotonic()
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            if self.monotonic() - started_at > 60:
                raise _InvalidResponse("operation deadline exceeded")
            query = dict(parameters)
            if cursor is not None:
                query["page"] = cursor
            payload = self.client.get_json(
                f"{endpoint}?{urlencode(query)}",
                {"Authorization": f"Bearer {secret}"},
            )
            if payload.get("object") != "page":
                raise _InvalidResponse("invalid page envelope")
            data = payload.get("data")
            has_more = payload.get("has_more")
            page_limit = parameters["limit"]
            if (
                not isinstance(data, list)
                or type(has_more) is not bool
                or type(page_limit) is not int
                or len(data) > page_limit
            ):
                raise _InvalidResponse("invalid page fields")
            pages.append(payload)
            next_page = payload.get("next_page")
            if not has_more:
                if next_page is not None:
                    raise _InvalidResponse("unexpected final cursor")
                return tuple(pages)
            if (
                not isinstance(next_page, str)
                or not next_page
                or len(next_page) > MAX_LABEL_LENGTH
                or any(ord(character) < 32 for character in next_page)
                or next_page in seen
            ):
                raise _InvalidResponse("invalid pagination cursor")
            seen.add(next_page)
            cursor = next_page
        raise _InvalidResponse("page limit exceeded")

    def _parse_usage_pages(
        self,
        pages: tuple[dict[str, Any], ...],
        start_time: int,
        end_time: int,
    ) -> tuple[DailyUsageRow, ...]:
        imported_at = self._imported_at()
        totals: dict[tuple[str, str], list[int]] = {}
        seen_buckets: set[int] = set()
        for payload in pages:
            result_count = 0
            for bucket in payload["data"]:
                day, bucket_start, results = self._bucket(bucket, start_time, end_time)
                if bucket_start in seen_buckets:
                    raise _InvalidResponse("duplicate bucket")
                seen_buckets.add(bucket_start)
                result_count += len(results)
                if result_count > MAX_RESULTS_PER_PAGE:
                    raise _InvalidResponse("usage result limit exceeded")
                for raw in results:
                    if not isinstance(raw, dict) or raw.get("object") != "organization.usage.completions.result":
                        raise _InvalidResponse("invalid usage result")
                    input_tokens = self._token(raw.get("input_tokens"))
                    output_tokens = self._token(raw.get("output_tokens"))
                    cached_tokens = self._token(raw.get("input_cached_tokens"))
                    if cached_tokens > input_tokens:
                        raise _InvalidResponse("cached input exceeds input")
                    model = self._model(raw.get("model"))
                    aggregate = totals.setdefault((day, model), [0, 0, 0])
                    aggregate[0] += input_tokens
                    aggregate[1] += output_tokens
                    aggregate[2] += cached_tokens
                    if any(value > 9_223_372_036_854_775_807 for value in aggregate):
                        raise _InvalidResponse("token sum overflow")
        rows = [
            DailyUsageRow(
                day=day,
                provider_id=self.config.provider_id,
                model_id=model,
                input_tokens=values[0],
                output_tokens=values[1],
                cache_read_tokens=values[2],
                cache_creation_tokens=0,
                reasoning_tokens=None,
                total_tokens=values[0] + values[1],
                cost_amount=None,
                cost_currency=None,
                cost_basis=None,
                quality="direct",
                imported_at=imported_at,
            )
            for (day, model), values in sorted(totals.items())
        ]
        return tuple(rows)

    def _parse_cost_pages(
        self,
        pages: tuple[dict[str, Any], ...],
        start_time: int,
        end_time: int,
    ) -> tuple[DailyCostRow, ...]:
        imported_at = self._imported_at()
        totals: dict[tuple[str, str], Decimal] = {}
        seen_buckets: set[int] = set()
        for payload in pages:
            result_count = 0
            for bucket in payload["data"]:
                day, bucket_start, results = self._bucket(bucket, start_time, end_time)
                if bucket_start in seen_buckets:
                    raise _InvalidResponse("duplicate bucket")
                seen_buckets.add(bucket_start)
                result_count += len(results)
                if result_count > MAX_RESULTS_PER_PAGE:
                    raise _InvalidResponse("cost result limit exceeded")
                for raw in results:
                    if not isinstance(raw, dict) or raw.get("object") != "organization.costs.result":
                        raise _InvalidResponse("invalid cost result")
                    amount = raw.get("amount")
                    if not isinstance(amount, dict):
                        raise _InvalidResponse("invalid amount")
                    value = self._decimal(amount.get("value"))
                    currency = amount.get("currency")
                    if (
                        not isinstance(currency, str)
                        or not currency.isascii()
                        or not 3 <= len(currency) <= 8
                        or ID_PATTERN.fullmatch(currency) is None
                    ):
                        raise _InvalidResponse("invalid currency")
                    currency = currency.upper()
                    totals[(day, currency)] = totals.get((day, currency), Decimal(0)) + value
        rows = [
            DailyCostRow(
                day=day,
                provider_id=self.config.provider_id,
                cost_kind="actual",
                currency=currency,
                amount=self._decimal_text(value),
                basis="provider_reported",
                quality="direct",
                imported_at=imported_at,
            )
            for (day, currency), value in sorted(totals.items())
        ]
        return tuple(rows)

    @staticmethod
    def _bucket(
        raw: Any, start_time: int, end_time: int
    ) -> tuple[str, int, list[Any]]:
        if not isinstance(raw, dict) or raw.get("object") != "bucket":
            raise _InvalidResponse("invalid bucket")
        start = raw.get("start_time")
        end = raw.get("end_time")
        results = raw.get("results")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end - start != 86400
            or start % 86400 != 0
            or start < start_time
            or end > end_time
            or not isinstance(results, list)
        ):
            raise _InvalidResponse("invalid daily bucket")
        return (
            datetime.fromtimestamp(start, tz=timezone.utc).date().isoformat(),
            start,
            results,
        )

    @staticmethod
    def _token(value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > 9_223_372_036_854_775_807
        ):
            raise _InvalidResponse("invalid token value")
        return value

    @staticmethod
    def _model(value: Any) -> str:
        try:
            return canonical_model_id(value, allow_missing=True)
        except InvalidModelID as error:
            raise _InvalidResponse("invalid model") from error

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _InvalidResponse("invalid decimal")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise _InvalidResponse("invalid decimal") from None
        if not result.is_finite() or result < 0:
            raise _InvalidResponse("invalid decimal")
        return result

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    def _imported_at(self) -> str:
        current = self.clock()
        if not isinstance(current, datetime):
            raise _InvalidResponse("invalid clock")
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()


class OpenAIOrganizationCardAdapter:
    """Expose configured/auth-required state without fabricating quota."""

    def __init__(
        self,
        config: OpenAIOrganizationConfig,
        keychain: MacOSKeychain,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> ProviderCard:
        now = self.clock().astimezone(timezone.utc)
        try:
            configured = bool(self.keychain.get(self.config.provider_id))
        except Exception:
            configured = False
        status = ProviderStatus.OK if configured else ProviderStatus.AUTH
        detail = (
            "Token activity and billed cost"
            if configured
            else "OpenAI Admin API key required"
        )
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=Category.API,
            status=status,
            primary="Configured" if configured else None,
            detail=detail,
            remaining_percent=None,
            resets_at=None,
            source="OpenAI Admin API",
            refreshed_at=now,
            last_error=None if configured else "Credential required",
            family_id="openai",
            credential_source="openai_admin_api",
            source_kind="official_api",
        )
