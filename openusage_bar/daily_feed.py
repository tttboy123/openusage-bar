from __future__ import annotations

import copy
import time as time_module
import urllib.parse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activity_store import DailyUsageRow
from .config import DailyUsageFeedConfig
from .generic import MissingField, extract_path
from .keychain import MacOSKeychain
from .model_ids import InvalidModelID, canonical_model_id
from .models import Category, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited
from .providers.contracts import ImportFailure, UsageImportResult, UsageImportSuccess
from .provider_catalog import catalog


CUSTOM_DAILY_FEED_SOURCE_ID = "custom.daily_feed"
MAX_FEED_HISTORY_DAYS = 365
MAX_FEED_PAGES = 64
MAX_FEED_ITEMS_PER_PAGE = 10_000
MAX_FEED_RECORDS = 100_000
MAX_CURSOR_LENGTH = 4096
MAX_TOKEN_VALUE = 9_223_372_036_854_775_807


class _InvalidFeed(ValueError):
    pass


def _category(config: DailyUsageFeedConfig) -> Category:
    try:
        value = catalog.require(config.family_id).category
    except KeyError:
        value = "api"
    return {
        "subscription": Category.SUBSCRIPTION,
        "local_tool": Category.LOCAL,
        "api": Category.API,
    }[value]


class DailyUsageFeedCardAdapter:
    """Publish configured/auth state without exposing endpoint or fabricating quota."""

    def __init__(
        self,
        config: DailyUsageFeedConfig,
        keychain: MacOSKeychain,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.account_ref = config.account_ref
        self.keychain = keychain
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> ProviderCard:
        now = self.clock()
        try:
            configured = bool(self.keychain.get(self.config.provider_id))
        except Exception:
            configured = False
        status = ProviderStatus.OK if configured else ProviderStatus.AUTH
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=_category(self.config),
            status=status,
            primary="Configured" if configured else None,
            detail="Daily model Token feed" if configured else "Credential required",
            remaining_percent=None,
            resets_at=None,
            source="Custom Daily Usage Feed",
            refreshed_at=now,
            last_error=None if configured else "Credential required",
            family_id=self.config.family_id,
            credential_source="api_key",
            source_kind="generic_https",
            account_ref=self.config.account_ref,
        )


class DailyUsageFeedImporter:
    usage_source_id = CUSTOM_DAILY_FEED_SOURCE_ID
    cost_source_id = None

    def __init__(
        self,
        config: DailyUsageFeedConfig,
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
        if not self._valid_range(since, until):
            return ImportFailure("invalid_request")
        try:
            local_timezone = ZoneInfo(self.config.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return ImportFailure("invalid_request")
        secret = self._secret()
        if secret is None:
            return ImportFailure("auth_required")
        try:
            records = self._fetch_records(since, until, secret)
            rows = self._parse_records(records, since, until, local_timezone)
            return UsageImportSuccess(since, until, rows)
        except AuthenticationRequired:
            return ImportFailure("auth_rejected")
        except RateLimited:
            return ImportFailure("rate_limited")
        except NetworkError:
            return ImportFailure("network_error")
        except (
            _InvalidFeed,
            MissingField,
            InvalidModelID,
            InvalidOperation,
            TypeError,
            ValueError,
            OverflowError,
        ):
            return ImportFailure("invalid_response")

    def _secret(self) -> str | None:
        try:
            value = self.keychain.get(self.config.provider_id)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _valid_range(since: date, until: date) -> bool:
        return (
            isinstance(since, date)
            and not isinstance(since, datetime)
            and isinstance(until, date)
            and not isinstance(until, datetime)
            and since <= until
            and (until - since).days + 1 <= MAX_FEED_HISTORY_DAYS
        )

    def _fetch_records(
        self, since: date, until: date, secret: str
    ) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        started_at = self.monotonic()
        headers = {
            self.config.header_name: f"{self.config.auth_prefix} {secret}".strip()
        }
        for index in range(MAX_FEED_PAGES):
            if self.monotonic() - started_at > 60:
                raise _InvalidFeed("operation deadline exceeded")
            parameters: dict[str, Any] = {
                self.config.since_parameter: since.isoformat(),
                self.config.until_parameter: until.isoformat(),
            }
            if self.config.pagination == "page":
                parameters[self.config.page_parameter] = index + 1
                parameters[self.config.limit_parameter] = self.config.page_size
            elif self.config.pagination == "offset":
                parameters[self.config.page_parameter] = index * self.config.page_size
                parameters[self.config.limit_parameter] = self.config.page_size
            elif self.config.pagination == "cursor" and cursor is not None:
                parameters[self.config.cursor_parameter] = cursor
            payload = self._request(parameters, headers)
            raw_items = extract_path(payload, self.config.items_path)
            if (
                not isinstance(raw_items, list)
                or len(raw_items) > MAX_FEED_ITEMS_PER_PAGE
            ):
                raise _InvalidFeed("invalid feed items")
            for item in raw_items:
                if not isinstance(item, dict):
                    raise _InvalidFeed("feed item must be an object")
                records.append(item)
                if len(records) > MAX_FEED_RECORDS:
                    raise _InvalidFeed("feed record limit exceeded")

            if self.config.pagination == "none":
                return tuple(records)
            if self.config.pagination in {"page", "offset"}:
                if len(raw_items) < self.config.page_size:
                    return tuple(records)
                continue

            next_cursor = extract_path(payload, self.config.next_cursor_path or "")
            if next_cursor is None or next_cursor == "":
                return tuple(records)
            if (
                not isinstance(next_cursor, str)
                or len(next_cursor) > MAX_CURSOR_LENGTH
                or any(ord(character) < 32 for character in next_cursor)
                or next_cursor in seen_cursors
            ):
                raise _InvalidFeed("invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise _InvalidFeed("feed page limit exceeded")

    def _request(
        self, parameters: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        if self.config.method == "POST":
            body = copy.deepcopy(self.config.request_body or {})
            for key, value in parameters.items():
                if key in body:
                    raise _InvalidFeed("request parameter collides with fixed body")
                body[key] = value
            return self.client.post_json(self.config.endpoint, headers, body)
        parsed = urllib.parse.urlsplit(self.config.endpoint)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        existing = {key for key, _ in query}
        if existing.intersection(parameters):
            raise _InvalidFeed("request parameter collides with endpoint query")
        query.extend((key, str(value)) for key, value in parameters.items())
        endpoint = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
        )
        return self.client.get_json(endpoint, headers)

    def _parse_records(
        self,
        records: tuple[dict[str, Any], ...],
        since: date,
        until: date,
        local_timezone: ZoneInfo,
    ) -> tuple[DailyUsageRow, ...]:
        imported_at = self._imported_at()
        totals: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in records:
            day = self._day(extract_path(raw, self.config.date_path), local_timezone)
            if not since <= day <= until:
                continue
            model = canonical_model_id(extract_path(raw, self.config.model_path))
            input_tokens = self._token(extract_path(raw, self.config.input_tokens_path))
            output_tokens = self._token(extract_path(raw, self.config.output_tokens_path))
            cache_read = self._optional_token(raw, self.config.cache_read_tokens_path, 0)
            cache_creation = self._optional_token(
                raw, self.config.cache_creation_tokens_path, 0
            )
            reasoning = self._optional_token(
                raw, self.config.reasoning_tokens_path, None
            )
            total = self._token(extract_path(raw, self.config.total_tokens_path))
            component_total = (
                input_tokens + output_tokens + cache_read + cache_creation + (reasoning or 0)
            )
            if total != component_total:
                raise _InvalidFeed("total does not match mapped token components")
            cost = (
                self._decimal(extract_path(raw, self.config.cost_amount_path))
                if self.config.cost_amount_path
                else None
            )
            key = (day.isoformat(), model)
            aggregate = totals.setdefault(
                key,
                {
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_creation": 0,
                    "reasoning": 0 if reasoning is not None else None,
                    "total": 0,
                    "cost": Decimal(0) if cost is not None else None,
                },
            )
            if (aggregate["reasoning"] is None) != (reasoning is None):
                raise _InvalidFeed("inconsistent reasoning mapping")
            if (aggregate["cost"] is None) != (cost is None):
                raise _InvalidFeed("inconsistent cost mapping")
            aggregate["input"] += input_tokens
            aggregate["output"] += output_tokens
            aggregate["cache_read"] += cache_read
            aggregate["cache_creation"] += cache_creation
            if reasoning is not None:
                aggregate["reasoning"] += reasoning
            aggregate["total"] += total
            if cost is not None:
                aggregate["cost"] += cost
            if any(
                aggregate[field] > MAX_TOKEN_VALUE
                for field in ("input", "output", "cache_read", "cache_creation", "total")
            ):
                raise _InvalidFeed("token sum overflow")
        return tuple(
            DailyUsageRow(
                day=day,
                provider_id=self.config.provider_id,
                account_ref=self.config.account_ref,
                model_id=model,
                input_tokens=value["input"],
                output_tokens=value["output"],
                cache_read_tokens=value["cache_read"],
                cache_creation_tokens=value["cache_creation"],
                reasoning_tokens=value["reasoning"],
                total_tokens=value["total"],
                cost_amount=(
                    None if value["cost"] is None else self._decimal_text(value["cost"])
                ),
                cost_currency=(
                    None
                    if value["cost"] is None
                    else (self.config.cost_currency or "").upper()
                ),
                cost_basis=None if value["cost"] is None else "provider_reported",
                quality="direct",
                imported_at=imported_at,
            )
            for (day, model), value in sorted(totals.items())
        )

    def _day(self, value: Any, local_timezone: ZoneInfo) -> date:
        if self.config.timestamp_format == "date":
            if not isinstance(value, str):
                raise _InvalidFeed("invalid date")
            result = date.fromisoformat(value)
            if result.isoformat() != value:
                raise _InvalidFeed("invalid date")
            return result
        if self.config.timestamp_format == "iso8601":
            if not isinstance(value, str) or len(value) > 128:
                raise _InvalidFeed("invalid timestamp")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=local_timezone)
            return parsed.astimezone(local_timezone).date()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidFeed("invalid timestamp")
        seconds = float(value)
        if self.config.timestamp_format == "unix_milliseconds":
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=local_timezone).date()

    @staticmethod
    def _token(value: Any) -> int:
        if isinstance(value, bool):
            raise _InvalidFeed("invalid token")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            result = int(value)
        else:
            raise _InvalidFeed("invalid token")
        if not 0 <= result <= MAX_TOKEN_VALUE:
            raise _InvalidFeed("invalid token")
        return result

    def _optional_token(
        self, raw: dict[str, Any], path: str | None, default: int | None
    ) -> int | None:
        return default if path is None else self._token(extract_path(raw, path))

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _InvalidFeed("invalid cost")
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise _InvalidFeed("invalid cost")
        return result

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"

    def _imported_at(self) -> str:
        current = self.clock()
        if not isinstance(current, datetime):
            raise _InvalidFeed("invalid clock")
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
