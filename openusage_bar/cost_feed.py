from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activity_store import DailyCostRow
from .config import DailyCostFeedConfig, ID_PATTERN
from .daily_feed import DailyUsageFeedImporter, _InvalidFeed
from .generic import MissingField, extract_path
from .keychain import MacOSKeychain
from .models import Category, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited
from .provider_catalog import catalog
from .providers.contracts import CostImportResult, CostImportSuccess, ImportFailure


CUSTOM_COST_FEED_SOURCE_ID = "custom.cost_feed"


def _category(config: DailyCostFeedConfig) -> Category:
    try:
        value = catalog.require(config.family_id).category
    except KeyError:
        value = "api"
    return {
        "subscription": Category.SUBSCRIPTION,
        "local_tool": Category.LOCAL,
        "api": Category.API,
    }[value]


class DailyCostFeedCardAdapter:
    """Publish connection health without exposing endpoint or inventing spend."""

    def __init__(
        self,
        config: DailyCostFeedConfig,
        keychain: MacOSKeychain,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> ProviderCard:
        now = self.clock()
        try:
            configured = bool(self.keychain.get(self.config.provider_id))
        except Exception:
            configured = False
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=_category(self.config),
            status=ProviderStatus.OK if configured else ProviderStatus.AUTH,
            primary="Configured" if configured else None,
            detail="Daily monetary cost feed" if configured else "Credential required",
            remaining_percent=None,
            resets_at=None,
            source="Custom Daily Cost Feed",
            refreshed_at=now,
            last_error=None if configured else "Credential required",
            family_id=self.config.family_id,
            credential_source="api_key",
            source_kind="generic_https",
            account_ref=self.config.account_ref,
        )


class DailyCostFeedImporter:
    """Map an allowlisted range JSON feed only into monetary ledger rows."""

    usage_source_id = None
    cost_source_id = CUSTOM_COST_FEED_SOURCE_ID

    def __init__(
        self,
        config: DailyCostFeedConfig,
        keychain: MacOSKeychain,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.account_ref = config.account_ref
        # The range transport is intentionally shared with DailyUsageFeedImporter:
        # identical HTTPS, pagination, cursor, deadline, and Keychain boundaries.
        self._transport = DailyUsageFeedImporter(
            config, keychain, client, clock=clock, monotonic=monotonic
        )

    def fetch_costs(self, since: date, until: date) -> CostImportResult:
        if not self._transport._valid_range(since, until):
            return ImportFailure("invalid_request")
        try:
            local_timezone = ZoneInfo(self.config.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return ImportFailure("invalid_request")
        secret = self._transport._secret()
        if secret is None:
            return ImportFailure("auth_required")
        try:
            records = self._transport._fetch_records(since, until, secret)
            rows = self._parse_records(records, since, until, local_timezone)
            return CostImportSuccess(since, until, rows)
        except AuthenticationRequired:
            return ImportFailure("auth_rejected")
        except RateLimited:
            return ImportFailure("rate_limited")
        except NetworkError:
            return ImportFailure("network_error")
        except (
            _InvalidFeed, MissingField, InvalidOperation, TypeError, ValueError,
            OverflowError,
        ):
            return ImportFailure("invalid_response")

    def _parse_records(
        self,
        records: tuple[dict[str, Any], ...],
        since: date,
        until: date,
        local_timezone: ZoneInfo,
    ) -> tuple[DailyCostRow, ...]:
        totals: dict[tuple[str, str], Decimal] = {}
        for raw in records:
            day = self._transport._day(
                extract_path(raw, self.config.date_path), local_timezone
            )
            if not since <= day <= until:
                continue
            amount = self._decimal(extract_path(raw, self.config.amount_path))
            currency = extract_path(raw, self.config.currency_path)
            if (
                not isinstance(currency, str)
                or not currency.isascii()
                or not 3 <= len(currency) <= 8
                or ID_PATTERN.fullmatch(currency) is None
            ):
                raise _InvalidFeed("invalid currency")
            key = (day.isoformat(), currency.upper())
            totals[key] = totals.get(key, Decimal(0)) + amount
        imported_at = self._transport._imported_at()
        return tuple(
            DailyCostRow(
                day=day,
                provider_id=self.config.provider_id,
                account_ref=self.config.account_ref,
                cost_kind=self.config.cost_kind,
                currency=currency,
                amount=self._decimal_text(amount),
                basis=self.config.basis,
                quality="direct",
                imported_at=imported_at,
            )
            for (day, currency), amount in sorted(totals.items())
        )

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _InvalidFeed("invalid cost")
        text = str(value)
        if len(text) > 128:
            raise _InvalidFeed("invalid cost")
        result = Decimal(text)
        if not result.is_finite() or result < 0:
            raise _InvalidFeed("invalid cost")
        return result

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"
