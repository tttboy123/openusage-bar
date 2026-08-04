"""DeepSeek account balance adapter (real balance endpoint)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .activity_records import BalanceObservation, canonical_decimal
from .models import Category, ProviderCard, ProviderStatus
from .network import (
    AuthenticationRequired,
    BoundedHTTPClient,
    MalformedResponse,
    NetworkError,
    RateLimited,
    ResponseTooLarge,
)
from .providers.contracts import BalanceFetchFailure, BalanceFetchSuccess


DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


def _amount(value: Any, field: str, *, required: bool = False) -> str | None:
    canonical = canonical_decimal(value, field)
    if canonical is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        if Decimal(canonical) < 0:
            raise ValueError(f"{field} must be nonnegative")
    except InvalidOperation as error:
        raise ValueError(f"{field} is invalid") from error
    return canonical


def _compact_amount(value: str) -> str:
    decimal = Decimal(value)
    if decimal == decimal.to_integral():
        return str(decimal.quantize(Decimal(1)))
    return format(decimal.normalize(), "f")


class DeepSeekBalanceAdapter:
    """Publish the DeepSeek account balance as a measured quota-hub fact."""

    source_id = "deepseek.balance"
    source_priority = 20
    performance_source_class = "network"

    def __init__(
        self,
        *,
        keychain: object | None = None,
        client: BoundedHTTPClient | None = None,
        clock: Callable[[], datetime] | None = None,
        provider_id: str = "deepseek",
    ) -> None:
        self.provider_id = provider_id
        self.keychain = keychain
        self.client = client or BoundedHTTPClient(allowed_redirect_hosts=set())
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_balance_result = BalanceFetchFailure("not_collected")

    def _secret(self) -> str | None:
        value = os.environ.get(DEEPSEEK_API_KEY_ENV)
        if value:
            return value
        if self.keychain is not None:
            try:
                return self.keychain.get(self.provider_id)
            except Exception:
                return None
        return None

    def _observation(
        self, payload: dict[str, Any], observed_at: datetime
    ) -> BalanceObservation:
        if payload.get("is_available") is not True:
            raise ValueError("DeepSeek account is not available")
        infos = payload.get("balance_infos")
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
            raise ValueError("DeepSeek response is missing balance_infos")
        info = infos[0]
        currency = info.get("currency") or "CNY"
        available = _amount(info.get("total_balance"), "total_balance", required=True)
        assert available is not None
        return BalanceObservation(
            record_id=f"{self.provider_id}.balance",
            observed_at=observed_at.astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            provider_id=self.provider_id,
            account_ref="deepseek",
            currency=currency,
            available=available,
            voucher=_amount(info.get("granted_balance"), "granted_balance"),
            cash=_amount(info.get("topped_up_balance"), "topped_up_balance"),
            state="ok",
            quality="direct",
            stale=False,
            source_id=self.source_id,
        )

    def fetch(self) -> ProviderCard:
        now = self.clock()
        secret = self._secret()
        if not secret:
            self.last_balance_result = BalanceFetchFailure(
                "authentication_required"
            )
            return self._card(
                ProviderStatus.AUTH,
                "DEEPSEEK_API_KEY required",
                now,
            )
        try:
            payload = self.client.get_json(
                DEEPSEEK_BALANCE_URL,
                {"Authorization": f"Bearer {secret}"},
            )
            observation = self._observation(payload, now)
            self.last_balance_result = BalanceFetchSuccess((observation,))
            return self._card(
                ProviderStatus.OK,
                "Available API balance",
                now,
                primary=(
                    f"{observation.currency} "
                    f"{_compact_amount(observation.available or '0')}"
                ),
            )
        except AuthenticationRequired:
            self.last_balance_result = BalanceFetchFailure(
                "authentication_required"
            )
            return self._card(ProviderStatus.AUTH, "API key rejected", now)
        except RateLimited:
            self.last_balance_result = BalanceFetchFailure("rate_limited")
            return self._card(
                ProviderStatus.RATE_LIMITED, "Balance request rate limited", now
            )
        except (KeyError, TypeError, ValueError):
            self.last_balance_result = BalanceFetchFailure("invalid_response")
            return self._card(
                ProviderStatus.ERROR, "Balance response was invalid", now
            )
        except MalformedResponse:
            self.last_balance_result = BalanceFetchFailure("invalid_response")
            return self._card(
                ProviderStatus.ERROR, "Balance response was invalid", now
            )
        except ResponseTooLarge:
            self.last_balance_result = BalanceFetchFailure("response_too_large")
            return self._card(
                ProviderStatus.ERROR, "Balance response was too large", now
            )
        except NetworkError:
            self.last_balance_result = BalanceFetchFailure("network_error")
            return self._card(
                ProviderStatus.ERROR, "Balance request failed", now
            )

    def _card(
        self,
        status: ProviderStatus,
        message: str,
        now: datetime,
        *,
        primary: str | None = None,
    ) -> ProviderCard:
        return ProviderCard(
            provider_id=self.provider_id,
            name="DeepSeek",
            category=Category.API,
            status=status,
            primary=primary,
            detail=message,
            remaining_percent=None,
            resets_at=None,
            source="DeepSeek Balance API",
            refreshed_at=now,
            last_error=None if status is ProviderStatus.OK else message,
            family_id="deepseek",
            credential_source="deepseek_official_api",
            source_kind="official_api",
            account_ref="deepseek",
        )
