from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .activity_records import BalanceObservation, canonical_decimal
from .config import MoonshotConfig
from .keychain import KeychainError
from .models import Category, ProviderCard, ProviderStatus
from .network import (
    AuthenticationRequired,
    BoundedHTTPClient,
    MalformedResponse,
    NetworkError,
    RateLimited,
    ResponseTooLarge,
)
from .providers.contracts import (
    BalanceCollectionResult,
    BalanceFetchFailure,
    BalanceFetchSuccess,
    SourceAttribution,
)


MOONSHOT_ENDPOINTS = {
    "china": "https://api.moonshot.cn/v1/users/me/balance",
    "international": "https://api.moonshot.ai/v1/users/me/balance",
}
MOONSHOT_CURRENCIES = {"china": "CNY", "international": "USD"}


def endpoint_for_site(site: str) -> str:
    try:
        return MOONSHOT_ENDPOINTS[site]
    except KeyError as error:
        raise ValueError("Moonshot site must be china or international") from error


def _amount(value: Any, field: str, *, required: bool = False) -> str | None:
    canonical = canonical_decimal(value, field)
    if canonical is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        if Decimal(canonical) < 0:
            raise ValueError(f"{field} must be nonnegative")
    except InvalidOperation as error:  # defensive; canonical_decimal already parses
        raise ValueError(f"{field} is invalid") from error
    return canonical


def _compact_amount(value: str) -> str:
    decimal = Decimal(value)
    if decimal == decimal.to_integral():
        return str(decimal.quantize(Decimal(1)))
    return format(decimal.normalize(), "f")


class MoonshotBalanceAdapter:
    source_id = "moonshot.balance"
    source_priority = 20
    _ATTRIBUTION = SourceAttribution(
        credential_source="moonshot_official_api",
        source_kind="official_api",
    )

    def __init__(
        self,
        config: MoonshotConfig,
        keychain: object,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime],
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.client = client
        self.clock = clock
        self.last_balance_result = BalanceFetchFailure("not_collected")

    def _observation(
        self, payload: dict[str, Any], observed_at: datetime
    ) -> BalanceObservation:
        if payload.get("code") not in {0, "0"}:
            raise ValueError("Moonshot response reports an error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Moonshot response is missing data")
        available = _amount(
            data.get("available_balance"), "available_balance", required=True
        )
        assert available is not None
        return BalanceObservation(
            record_id=f"{self.config.provider_id}.balance",
            observed_at=observed_at.astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            provider_id=self.config.provider_id,
            account_ref=self.config.account_ref,
            currency=MOONSHOT_CURRENCIES[self.config.site],
            available=available,
            voucher=_amount(data.get("voucher_balance"), "voucher_balance"),
            cash=_amount(data.get("cash_balance"), "cash_balance"),
            state="ok",
            quality="direct",
            stale=False,
            source_id=self.source_id,
        )

    def fetch(self) -> ProviderCard:
        now = self.clock()
        secret = self.keychain.get(self.config.provider_id)
        if not secret:
            self.last_balance_result = BalanceFetchFailure(
                "authentication_required"
            )
            return self._error_card(
                ProviderStatus.AUTH, "API key required", now
            )
        try:
            payload = self.client.get_json(
                endpoint_for_site(self.config.site),
                {"Authorization": f"Bearer {secret}"},
            )
            observation = self._observation(payload, now)
            self.last_balance_result = BalanceFetchSuccess((observation,))
            return ProviderCard(
                provider_id=self.config.provider_id,
                name=self.config.name,
                category=Category.API,
                status=ProviderStatus.OK,
                primary=(
                    f"{observation.currency} "
                    f"{_compact_amount(observation.available or '0')}"
                ),
                detail="Available API balance",
                remaining_percent=None,
                resets_at=None,
                source="Moonshot Balance API",
                refreshed_at=now,
                family_id="moonshot",
                credential_source="moonshot_official_api",
                source_kind="official_api",
                account_ref=self.config.account_ref,
            )
        except AuthenticationRequired:
            self.last_balance_result = BalanceFetchFailure(
                "authentication_required"
            )
            return self._error_card(
                ProviderStatus.AUTH, "API key rejected", now
            )
        except RateLimited:
            self.last_balance_result = BalanceFetchFailure("rate_limited")
            return self._error_card(
                ProviderStatus.RATE_LIMITED, "Balance request rate limited", now
            )
        except (KeyError, TypeError, ValueError):
            self.last_balance_result = BalanceFetchFailure("invalid_response")
            return self._error_card(
                ProviderStatus.ERROR, "Balance response was invalid", now
            )
        except MalformedResponse:
            self.last_balance_result = BalanceFetchFailure("invalid_response")
            return self._error_card(
                ProviderStatus.ERROR, "Balance response was invalid", now
            )
        except ResponseTooLarge:
            self.last_balance_result = BalanceFetchFailure("response_too_large")
            return self._error_card(
                ProviderStatus.ERROR, "Balance response was too large", now
            )
        except NetworkError:
            self.last_balance_result = BalanceFetchFailure("network_error")
            return self._error_card(
                ProviderStatus.ERROR, "Balance request failed", now
            )

    def fetch_balance(self) -> BalanceCollectionResult:
        now = self.clock()
        try:
            secret = self.keychain.get(self.config.provider_id)
        except KeychainError:
            return BalanceCollectionResult(
                result=BalanceFetchFailure("keychain_unavailable"),
                attribution=self._ATTRIBUTION,
            )
        if not secret:
            result = BalanceFetchFailure("authentication_required")
        else:
            try:
                payload = self.client.get_json(
                    endpoint_for_site(self.config.site),
                    {"Authorization": f"Bearer {secret}"},
                )
                result = BalanceFetchSuccess((self._observation(payload, now),))
            except AuthenticationRequired:
                result = BalanceFetchFailure("authentication_required")
            except RateLimited:
                result = BalanceFetchFailure("rate_limited")
            except (KeyError, TypeError, ValueError, MalformedResponse):
                result = BalanceFetchFailure("invalid_response")
            except ResponseTooLarge:
                result = BalanceFetchFailure("response_too_large")
            except NetworkError:
                result = BalanceFetchFailure("network_error")
        return BalanceCollectionResult(
            result=result,
            attribution=self._ATTRIBUTION,
        )

    def _error_card(
        self, status: ProviderStatus, message: str, now: datetime
    ) -> ProviderCard:
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=Category.API,
            status=status,
            primary=None,
            detail=message,
            remaining_percent=None,
            resets_at=None,
            source="Moonshot Balance API",
            refreshed_at=now,
            last_error=message,
            family_id="moonshot",
            credential_source="moonshot_official_api",
            source_kind="official_api",
            account_ref=self.config.account_ref,
        )
