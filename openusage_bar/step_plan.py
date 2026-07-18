from __future__ import annotations

import base64
import json
import math
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import StepPlanConfig
from .keychain import KeychainError, MacOSKeychain
from .models import Category, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited
from .providers.contracts import QuotaFetchFailure, QuotaFetchSuccess
from .providers.quota import percent_observation


STEP_PLAN_MODELS_ENDPOINT = "https://api.stepfun.com/step_plan/v1/models"
STEP_PLAN_RATE_LIMIT_ENDPOINT = (
    "https://platform.stepfun.com/api/"
    "step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit"
)
STEP_PLAN_STATUS_ENDPOINT = (
    "https://platform.stepfun.com/api/"
    "step.openapi.devcenter.Dashboard/GetStepPlanStatus"
)
STEP_PLAN_REFRESH_ENDPOINT = (
    "https://platform.stepfun.com/passport/"
    "proto.api.passport.v1.PassportService/RefreshToken"
)
STEP_PLAN_TOKEN_SUFFIX = ".oasis-token"
STEP_PLAN_WEBID_SUFFIX = ".oasis-webid"


@dataclass(frozen=True)
class StepPlanEndpoints:
    platform_host: str
    api_host: str
    models: str
    quota: str
    status: str
    refresh: str


def endpoints_for_site(site: str) -> StepPlanEndpoints:
    hosts = {
        "china": ("platform.stepfun.com", "api.stepfun.com"),
        "international": ("platform.stepfun.ai", "api.stepfun.ai"),
    }
    try:
        platform_host, api_host = hosts[site]
    except KeyError as error:
        raise StepPlanParseError("Unsupported StepFun site") from error
    return StepPlanEndpoints(
        platform_host=platform_host,
        api_host=api_host,
        models=f"https://{api_host}/step_plan/v1/models",
        quota=(
            f"https://{platform_host}/api/"
            "step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit"
        ),
        status=(
            f"https://{platform_host}/api/"
            "step.openapi.devcenter.Dashboard/GetStepPlanStatus"
        ),
        refresh=(
            f"https://{platform_host}/passport/"
            "proto.api.passport.v1.PassportService/RefreshToken"
        ),
    )


class StepPlanParseError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class StepPlanSession:
    token: str
    webid: str

    @classmethod
    def parse(cls, raw: str) -> StepPlanSession:
        value = raw.strip()
        if not value:
            raise StepPlanParseError("StepFun session is required")

        cookie_values: dict[str, str] = {}
        for part in value.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if separator and name.casefold() in {"oasis-token", "oasis-webid"}:
                cookie_values[name.casefold()] = urllib.parse.unquote(cookie_value.strip())

        token = cookie_values.get("oasis-token")
        if token is None and "=" not in value:
            token = urllib.parse.unquote(value)
        webid = cookie_values.get("oasis-webid") or cls._webid_from_token(token or "")

        if not token or len(token) > 8192 or any(character in token for character in "\r\n;"):
            raise StepPlanParseError("StepFun session has no valid Oasis-Token")
        if (
            not webid
            or len(webid) > 128
            or re.fullmatch(r"[A-Za-z0-9._-]+", webid) is None
        ):
            raise StepPlanParseError("StepFun session has no valid Oasis-Webid")
        return cls(token=token, webid=webid)

    @staticmethod
    def _webid_from_token(token: str) -> str | None:
        try:
            refresh_token = token.split("...", 1)[1]
            payload_segment = refresh_token.split(".")[1]
            padding = "=" * (-len(payload_segment) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
            device_id = payload.get("device_id") if isinstance(payload, dict) else None
            return device_id.strip() if isinstance(device_id, str) else None
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            return None


def _fraction(payload: dict[str, Any], field: str) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StepPlanParseError(f"StepFun response has no valid {field}")
    result = float(value)
    if not 0 <= result <= 1:
        raise StepPlanParseError(f"StepFun response has out-of-range {field}")
    return result


def _optional_timestamp(payload: dict[str, Any], field: str) -> datetime | None:
    value = payload.get(field)
    if isinstance(value, bool):
        raise StepPlanParseError(f"StepFun response has no valid {field}")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise StepPlanParseError(f"StepFun response has no valid {field}") from error
    if seconds == 0:
        return None
    if seconds < 0:
        raise StepPlanParseError(f"StepFun response has no valid {field}")
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _optional_rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0 <= result <= 1 else None


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _compact_credit(value: float) -> str:
    for suffix, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if value >= scale:
            compact = f"{value / scale:.1f}".rstrip("0").rstrip(".")
            return f"{compact}{suffix}"
    return f"{value:,.0f}"


class StepPlanAdapter:
    def __init__(
        self,
        config: StepPlanConfig,
        keychain: MacOSKeychain,
        client: BoundedHTTPClient,
        clock: Callable[[], datetime],
    ) -> None:
        self.config = config
        self.keychain = keychain
        self.client = client
        self.clock = clock
        self.endpoints = endpoints_for_site(config.site)
        self.last_quota_result = QuotaFetchFailure("not_collected")

    @staticmethod
    def _site_label(config: StepPlanConfig) -> str:
        return "International" if config.site == "international" else "China"

    @staticmethod
    def parse(
        config: StepPlanConfig, payload: dict[str, Any], now: datetime
    ) -> ProviderCard:
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raise StepPlanParseError("Step Plan response has no model list")
        model_ids = [
            model["id"].strip()
            for model in raw_models
            if isinstance(model, dict)
            and isinstance(model.get("id"), str)
            and model["id"].strip()
        ]
        if not model_ids:
            raise StepPlanParseError("Step Plan response has no valid models")
        return ProviderCard(
            provider_id=config.provider_id,
            name=config.name,
            category=Category.SUBSCRIPTION,
            status=ProviderStatus.OK,
            primary="Plan connected",
            detail=(
                f"{len(model_ids)} models · add a web session for 5h/week quota"
            ),
            remaining_percent=None,
            resets_at=None,
            source=f"StepFun {StepPlanAdapter._site_label(config)} Step Plan",
            refreshed_at=now,
            family_id="step_plan",
            credential_source="step_plan_official_api",
            source_kind="official_api",
        )

    @staticmethod
    def parse_quota(
        config: StepPlanConfig,
        payload: dict[str, Any],
        status_payload: dict[str, Any] | None,
        now: datetime,
    ) -> ProviderCard:
        if payload.get("status") != 1:
            raise StepPlanParseError("StepFun quota request was rejected")
        credit_card = StepPlanAdapter._parse_credit_quota(
            config, payload, status_payload, now
        )
        if credit_card is not None:
            return credit_card
        five_remaining = _fraction(payload, "five_hour_usage_left_rate") * 100
        weekly_remaining = _fraction(payload, "weekly_usage_left_rate") * 100
        five_reset = _optional_timestamp(payload, "five_hour_usage_reset_time")
        weekly_reset = _optional_timestamp(payload, "weekly_usage_reset_time")

        detail_parts = [f"Weekly {round(weekly_remaining)}% remaining"]
        if weekly_reset is not None:
            detail_parts.append(f"resets {weekly_reset:%b} {weekly_reset.day}")
        if isinstance(status_payload, dict):
            subscription = status_payload.get("subscription")
            plan_name = subscription.get("name") if isinstance(subscription, dict) else None
            if isinstance(plan_name, str) and plan_name.strip():
                detail_parts.append(" ".join(plan_name.split())[:80])

        return ProviderCard(
            provider_id=config.provider_id,
            name=config.name,
            category=Category.SUBSCRIPTION,
            status=(
                ProviderStatus.RATE_LIMITED
                if five_remaining <= 0 or weekly_remaining <= 0
                else ProviderStatus.OK
            ),
            primary=f"5h {round(five_remaining)}% remaining",
            detail=" · ".join(detail_parts),
            remaining_percent=five_remaining,
            resets_at=five_reset,
            source=(
                f"StepFun {StepPlanAdapter._site_label(config)} Web Session"
            ),
            refreshed_at=now,
            family_id="step_plan",
            credential_source="step_plan_browser_session",
            source_kind="browser_session",
        )

    @staticmethod
    def _parse_credit_quota(
        config: StepPlanConfig,
        payload: dict[str, Any],
        status_payload: dict[str, Any] | None,
        now: datetime,
    ) -> ProviderCard | None:
        credit_limit = payload.get("plan_credit_rate_limit")
        if not isinstance(credit_limit, dict):
            return None

        subscription_rate = _optional_rate(
            credit_limit.get("subscription_credit_left_rate")
        )
        topup_rate = _optional_rate(credit_limit.get("topup_credit_left_rate"))
        rates = [rate for rate in (subscription_rate, topup_rate) if rate is not None]
        if not rates:
            return None
        remaining = max(rates) * 100

        detail_parts: list[str] = []
        raw_buckets = credit_limit.get("credit_buckets")
        buckets = raw_buckets if isinstance(raw_buckets, list) else []
        valid_buckets: list[tuple[dict[str, Any], float, float]] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            total = _nonnegative_number(bucket.get("credit_total"))
            residual = _nonnegative_number(bucket.get("credit_residual"))
            if total is None or total <= 0 or residual is None:
                continue
            valid_buckets.append((bucket, min(residual, total), total))
        if valid_buckets:
            bucket, residual, total = next(
                (
                    item
                    for item in valid_buckets
                    if item[0].get("type") == 1
                ),
                valid_buckets[0],
            )
            detail_parts.append(
                f"{_compact_credit(residual)} / {_compact_credit(total)} credits"
            )
            try:
                expires_at = _optional_timestamp(bucket, "expire_at")
            except StepPlanParseError:
                expires_at = None
            if expires_at is not None:
                detail_parts.append(f"expires {expires_at:%b} {expires_at.day}")

        plan_name = None
        if isinstance(status_payload, dict):
            subscription = status_payload.get("subscription")
            raw_name = subscription.get("name") if isinstance(subscription, dict) else None
            if isinstance(raw_name, str) and raw_name.strip():
                plan_name = " ".join(raw_name.split())[:80]
        if plan_name:
            detail_parts.append(plan_name)

        try:
            resets_at = _optional_timestamp(
                credit_limit, "subscription_credit_reset_time"
            )
        except StepPlanParseError:
            resets_at = None

        return ProviderCard(
            provider_id=config.provider_id,
            name=config.name,
            category=Category.SUBSCRIPTION,
            status=(
                ProviderStatus.RATE_LIMITED
                if remaining <= 0
                else ProviderStatus.OK
            ),
            primary=f"{round(remaining)}% remaining",
            detail=" · ".join(detail_parts) or "Credit plan",
            remaining_percent=remaining,
            resets_at=resets_at,
            source=f"StepFun {StepPlanAdapter._site_label(config)} Web Session",
            refreshed_at=now,
            family_id="step_plan",
            credential_source="step_plan_browser_session",
            source_kind="browser_session",
        )

    @staticmethod
    def quota_observations(
        config: StepPlanConfig, payload: dict[str, Any], now: datetime
    ) -> QuotaFetchSuccess | QuotaFetchFailure:
        if payload.get("status") != 1:
            return QuotaFetchFailure("invalid_response")
        credit_limit = payload.get("plan_credit_rate_limit")
        if isinstance(credit_limit, dict):
            rates = [
                rate for rate in (
                    _optional_rate(credit_limit.get("subscription_credit_left_rate")),
                    _optional_rate(credit_limit.get("topup_credit_left_rate")),
                ) if rate is not None
            ]
            if rates:
                try:
                    reset = _optional_timestamp(
                        credit_limit, "subscription_credit_reset_time"
                    )
                except StepPlanParseError:
                    reset = None
                return QuotaFetchSuccess((percent_observation(
                    provider_id=config.provider_id, source_id="step_plan.quota",
                    quota_name="Credit Plan", quota_window="billing_cycle",
                    remaining_percent=max(rates) * 100, resets_at=reset,
                    observed_at=now, applies_to_kind="subscription",
                ),))
        try:
            values = (
                (
                    "5h Subscription", "five_hour",
                    _fraction(payload, "five_hour_usage_left_rate") * 100,
                    _optional_timestamp(payload, "five_hour_usage_reset_time"),
                ),
                (
                    "Weekly Subscription", "weekly",
                    _fraction(payload, "weekly_usage_left_rate") * 100,
                    _optional_timestamp(payload, "weekly_usage_reset_time"),
                ),
            )
        except StepPlanParseError:
            return QuotaFetchFailure("invalid_response")
        return QuotaFetchSuccess(tuple(
            percent_observation(
                provider_id=config.provider_id, source_id="step_plan.quota",
                quota_name=name, quota_window=window,
                remaining_percent=remaining, resets_at=reset,
                observed_at=now, applies_to_kind="subscription",
            )
            for name, window, remaining, reset in values
        ))

    def fetch(self) -> ProviderCard:
        now = self.clock()
        try:
            token = self.keychain.get(
                self.config.provider_id + STEP_PLAN_TOKEN_SUFFIX
            )
            webid = self.keychain.get(
                self.config.provider_id + STEP_PLAN_WEBID_SUFFIX
            )
            api_key = self.keychain.get(self.config.provider_id)
            if token or webid:
                if not token or not webid:
                    raise StepPlanParseError("StepFun web session is incomplete")
                return self._fetch_session(StepPlanSession(token=token, webid=webid), now)
            if not api_key:
                self.last_quota_result = QuotaFetchFailure("auth_required")
                return self._error_card(ProviderStatus.AUTH, "Credential required", now)
            payload = self.client.get_json(
                self.endpoints.models,
                {"Authorization": f"Bearer {api_key}"},
            )
            self.last_quota_result = QuotaFetchFailure("quota_unavailable")
            return self.parse(self.config, payload, now)
        except AuthenticationRequired:
            self.last_quota_result = QuotaFetchFailure("auth_rejected")
            return self._error_card(ProviderStatus.AUTH, "Credential rejected", now)
        except RateLimited:
            self.last_quota_result = QuotaFetchFailure("rate_limited")
            return self._error_card(ProviderStatus.RATE_LIMITED, "Rate limited", now)
        except (
            KeychainError,
            NetworkError,
            StepPlanParseError,
            TypeError,
            ValueError,
        ):
            self.last_quota_result = QuotaFetchFailure("invalid_response")
            return self._error_card(
                ProviderStatus.ERROR, "Step Plan refresh failed", now
            )

    def _fetch_session(self, session: StepPlanSession, now: datetime) -> ProviderCard:
        try:
            quota_payload = self.client.post_json(
                self.endpoints.quota,
                self._session_headers(session),
                {},
            )
        except AuthenticationRequired:
            session = self._refresh_session(session)
            self.keychain.set(
                self.config.provider_id + STEP_PLAN_TOKEN_SUFFIX,
                session.token,
            )
            quota_payload = self.client.post_json(
                self.endpoints.quota,
                self._session_headers(session),
                {},
            )

        status_payload: dict[str, Any] | None = None
        try:
            status_payload = self.client.post_json(
                self.endpoints.status,
                self._session_headers(session),
                {},
            )
        except NetworkError:
            pass
        self.last_quota_result = self.quota_observations(
            self.config, quota_payload, now
        )
        return self.parse_quota(self.config, quota_payload, status_payload, now)

    def _refresh_session(self, session: StepPlanSession) -> StepPlanSession:
        payload = self.client.post_json(
            self.endpoints.refresh,
            self._session_headers(session),
            {},
        )
        access = payload.get("accessToken")
        refresh = payload.get("refreshToken")
        access_raw = access.get("raw") if isinstance(access, dict) else None
        refresh_raw = refresh.get("raw") if isinstance(refresh, dict) else None
        if not isinstance(access_raw, str) or not access_raw.strip():
            raise StepPlanParseError("StepFun token refresh returned no access token")
        if not isinstance(refresh_raw, str) or not refresh_raw.strip():
            old_parts = session.token.split("...", 1)
            refresh_raw = old_parts[1] if len(old_parts) == 2 else None
        combined = (
            f"{access_raw.strip()}...{refresh_raw.strip()}"
            if isinstance(refresh_raw, str) and refresh_raw.strip()
            else access_raw.strip()
        )
        return StepPlanSession(token=combined, webid=session.webid)

    @staticmethod
    def _session_headers(session: StepPlanSession) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Oasis-Appid": "10300",
            "Oasis-Platform": "web",
            "Oasis-Webid": session.webid,
            "Oasis-Token": session.token,
            "Cookie": f"Oasis-Token={session.token}",
        }

    def _error_card(
        self, status: ProviderStatus, error: str, now: datetime
    ) -> ProviderCard:
        return ProviderCard(
            provider_id=self.config.provider_id,
            name=self.config.name,
            category=Category.SUBSCRIPTION,
            status=status,
            primary=None,
            detail=error,
            remaining_percent=None,
            resets_at=None,
            source=f"StepFun {self._site_label(self.config)} Step Plan",
            refreshed_at=now,
            last_error=error,
            family_id="step_plan",
            credential_source="step_plan_official_api",
            source_kind="official_api",
        )
