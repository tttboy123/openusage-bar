from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .config import GenericProviderConfig
from .keychain import MacOSKeychain
from .models import Category, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited
from .providers.contracts import QuotaFetchFailure, QuotaFetchSuccess
from .providers.quota import percent_observation


class MissingField(ValueError):
    pass


def extract_path(payload: dict[str, Any], path: str) -> Any:
    segments = path.split(".")
    if not path or any(not segment for segment in segments):
        raise MissingField("Field path is empty or invalid")
    current: Any = payload
    for segment in segments:
        if not isinstance(current, dict) or segment not in current:
            raise MissingField(f"Configured field {path!r} was not found")
        current = current[segment]
    return current


def _parse_reset(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    raise ValueError("Reset value must be an ISO timestamp or Unix timestamp")


class GenericHTTPSAdapter:
    def __init__(
        self,
        config: GenericProviderConfig,
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
    def parse(config: GenericProviderConfig, payload: dict[str, Any], now: datetime) -> ProviderCard:
        primary = str(extract_path(payload, config.primary_path))
        remaining: float | None = None
        if config.remaining_percent_path:
            remaining = float(extract_path(payload, config.remaining_percent_path))
            if not 0 <= remaining <= 100:
                raise ValueError("Remaining percentage must be between 0 and 100")
        detail = str(extract_path(payload, config.detail_path)) if config.detail_path else None
        resets_at = _parse_reset(extract_path(payload, config.reset_path)) if config.reset_path else None
        return ProviderCard(
            provider_id=config.provider_id,
            name=config.name,
            category=Category.SUBSCRIPTION if remaining is not None else Category.API,
            status=ProviderStatus.OK,
            primary=primary,
            detail=detail,
            remaining_percent=remaining,
            resets_at=resets_at,
            source="Direct API",
            refreshed_at=now,
            family_id=config.family_id or config.provider_id,
            credential_source="api_key",
            source_kind="generic_https",
            account_ref=config.account_ref,
        )

    def fetch(self) -> ProviderCard:
        now = self.clock()
        secret = self.keychain.get(self.config.provider_id)
        if not secret:
            self.last_quota_result = QuotaFetchFailure("auth_required")
            return self._error_card(ProviderStatus.AUTH, "Credential required", now)
        value = f"{self.config.auth_prefix} {secret}".strip()
        try:
            payload = self.client.get_json(
                self.config.endpoint, {self.config.header_name: value}
            )
            card = self.parse(self.config, payload, now)
            if card.remaining_percent is not None and self.config.quota_window:
                self.last_quota_result = QuotaFetchSuccess((percent_observation(
                    provider_id=self.config.provider_id,
                    account_ref=self.config.account_ref,
                    source_id="generic.quota",
                    quota_name=self.config.quota_name,
                    quota_window=self.config.quota_window,
                    remaining_percent=card.remaining_percent,
                    resets_at=card.resets_at,
                    observed_at=now,
                    applies_to_kind="account",
                ),))
            else:
                self.last_quota_result = QuotaFetchFailure("quota_unavailable")
            return card
        except AuthenticationRequired:
            self.last_quota_result = QuotaFetchFailure("auth_rejected")
            return self._error_card(ProviderStatus.AUTH, "Credential rejected", now)
        except RateLimited:
            self.last_quota_result = QuotaFetchFailure("rate_limited")
            return self._error_card(ProviderStatus.RATE_LIMITED, "Rate limited", now)
        except (NetworkError, MissingField, TypeError, ValueError):
            self.last_quota_result = QuotaFetchFailure("invalid_response")
            return self._error_card(ProviderStatus.ERROR, "Provider refresh failed", now)

    def _error_card(self, status: ProviderStatus, error: str, now: datetime) -> ProviderCard:
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=Category.API,
            status=status,
            primary=None,
            detail=error,
            remaining_percent=None,
            resets_at=None,
            source="Direct API",
            refreshed_at=now,
            last_error=error,
            family_id=self.config.family_id or self.config.provider_id,
            credential_source="api_key",
            source_kind="generic_https",
            account_ref=self.config.account_ref,
        )
