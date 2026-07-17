from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .bounded_process import BoundedProcessError, run_bounded
from .keychain import KeychainError
from .models import Category, Overview, ProviderCard, ProviderStatus
from .network import AuthenticationRequired, BoundedHTTPClient, NetworkError, RateLimited


logger = logging.getLogger(__name__)
KIRO_SOCIAL_SERVICE = "kirocli:social:token"
REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z0-9-]+-[0-9]+$")


class KiroCredentialError(ValueError):
    """A fixed, sanitized credential error safe for diagnostics."""


class KiroParseError(ValueError):
    """A fixed, sanitized response error safe for diagnostics."""


@dataclass(frozen=True)
class KiroCredentials:
    access_token: str
    profile_arn: str
    region: str


class KiroTokenReader(Protocol):
    def read(self) -> str | None: ...


class SecurityKiroTokenReader:
    """Read the current Kiro Social token without mutating Keychain state."""

    def __init__(self, runner=None, security_executable: str = "/usr/bin/security") -> None:
        self.runner = runner
        self.security_executable = security_executable

    def read(self) -> str | None:
        try:
            runner = self.runner or run_bounded
            options: dict[str, Any] = {}
            if self.runner is None:
                options = {"stdout_limit": 64 * 1024, "stderr_limit": 4 * 1024}
            completed = runner(
                [
                    self.security_executable,
                    "find-generic-password",
                    "-s",
                    KIRO_SOCIAL_SERVICE,
                    "-w",
                ],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
                **options,
            )
        except BoundedProcessError as error:
            message = "timed out" if error.code == "timeout" else "failed safely"
            raise KeychainError(f"Kiro Keychain read {message}") from None
        except subprocess.TimeoutExpired:
            raise KeychainError("Kiro Keychain read timed out") from None
        except OSError as error:
            raise KeychainError(
                f"Kiro Keychain reader failed ({type(error).__name__})"
            ) from None
        if completed.returncode == 44:
            return None
        if completed.returncode != 0:
            raise KeychainError(
                f"Kiro Keychain read failed with status {completed.returncode}"
            )
        try:
            return bytes(completed.stdout).decode("utf-8")
        except (TypeError, UnicodeDecodeError) as error:
            raise KeychainError("Kiro Keychain value is not valid UTF-8") from error


def parse_kiro_credentials(raw: str) -> KiroCredentials:
    if not isinstance(raw, str) or not raw:
        raise KiroCredentialError("Kiro credential data is missing")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeError):
        raise KiroCredentialError("Kiro credential data is invalid JSON") from None
    if not isinstance(payload, dict):
        raise KiroCredentialError("Kiro credential data has an invalid shape")

    access_token = payload.get("access_token") or payload.get("accessToken")
    profile_arn = payload.get("profile_arn") or payload.get("profileArn")
    if not isinstance(access_token, str) or not access_token.strip():
        raise KiroCredentialError("Kiro access credential is missing")
    if not isinstance(profile_arn, str) or not profile_arn.strip():
        raise KiroCredentialError("Kiro profile ARN is missing")

    parts = profile_arn.split(":")
    if (
        len(parts) < 6
        or parts[0] != "arn"
        or parts[2] != "codewhisperer"
        or not REGION_PATTERN.fullmatch(parts[3])
        or not parts[5]
    ):
        raise KiroCredentialError("Kiro profile ARN is invalid")
    return KiroCredentials(access_token.strip(), profile_arn.strip(), parts[3])


def _number(*values: Any) -> float | None:
    for value in values:
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            return float(value)
    return None


def _number_text(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _timestamp(value: Any) -> datetime | None:
    numeric = _number(value)
    if numeric is None or numeric <= 0:
        return None
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _request_id() -> str:
    value = os.urandom(16).hex()
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


def parse_kiro_quota(payload: dict[str, Any], now: datetime) -> ProviderCard:
    if not isinstance(payload, dict):
        raise KiroParseError("Kiro quota response has an invalid shape")
    rows = payload.get("usageBreakdownList")
    if not isinstance(rows, list):
        raise KiroParseError("Kiro quota response has no usage rows")
    row = next(
        (
            candidate
            for candidate in rows
            if isinstance(candidate, dict) and candidate.get("resourceType") == "CREDIT"
        ),
        None,
    )
    if row is None:
        raise KiroParseError("Kiro quota response has no credit quota")

    limit = _number(row.get("usageLimitWithPrecision"), row.get("usageLimit"))
    if limit is None or limit <= 0:
        raise KiroParseError("Kiro credit quota limit is unavailable")
    used = _number(row.get("currentUsageWithPrecision"), row.get("currentUsage"))
    used = min(limit, max(0.0, 0.0 if used is None else used))
    remaining = limit - used
    remaining_percent = remaining / limit * 100

    subscription = payload.get("subscriptionInfo")
    plan = (
        subscription.get("subscriptionTitle")
        if isinstance(subscription, dict)
        and isinstance(subscription.get("subscriptionTitle"), str)
        and subscription.get("subscriptionTitle").strip()
        else "Kiro"
    )
    detail = f"{plan} · {_number_text(used)} used"

    bonus = row.get("freeTrialInfo")
    if isinstance(bonus, dict) and bonus.get("freeTrialStatus") == "ACTIVE":
        bonus_limit = _number(
            bonus.get("usageLimitWithPrecision"), bonus.get("usageLimit")
        )
        bonus_used = _number(
            bonus.get("currentUsageWithPrecision"), bonus.get("currentUsage")
        )
        if bonus_limit is not None and bonus_limit > 0:
            bonus_used = min(
                bonus_limit, max(0.0, 0.0 if bonus_used is None else bonus_used)
            )
            detail += (
                f" · Bonus {_number_text(bonus_limit - bonus_used)}"
                f" / {_number_text(bonus_limit)} remaining"
            )

    reset = _timestamp(row.get("nextDateReset")) or _timestamp(
        payload.get("nextDateReset")
    )
    return ProviderCard(
        provider_id="kiro_cli",
        name="Kiro",
        category=Category.SUBSCRIPTION,
        status=ProviderStatus.OK,
        primary=(
            f"{_number_text(remaining)} / {_number_text(limit)} credits remaining"
        ),
        detail=detail,
        remaining_percent=remaining_percent,
        resets_at=reset,
        source="Kiro subscription quota",
        refreshed_at=now,
        family_id="kiro_cli",
        credential_source="kiro_codewhisperer_api",
        source_kind="official_api",
    )


class KiroQuotaAdapter:
    def __init__(
        self,
        client: BoundedHTTPClient | None = None,
        clock: Callable[[], datetime] | None = None,
        token_reader: KiroTokenReader | None = None,
    ) -> None:
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.token_reader = token_reader or SecurityKiroTokenReader()

    def fetch(self) -> Overview:
        try:
            raw = self.token_reader.read()
            if raw is None:
                return Overview([])
            credentials = parse_kiro_credentials(raw)
            endpoint = self._endpoint(credentials)
            client = self.client or BoundedHTTPClient(
                allowed_reserved_hosts={f"q.{credentials.region}.amazonaws.com"},
                allowed_redirect_hosts={f"q.{credentials.region}.amazonaws.com"},
            )
            payload = client.get_json(endpoint, self._headers(credentials))
            return Overview([parse_kiro_quota(payload, self.clock())])
        except KeychainError:
            return self._unavailable("keychain read failed")
        except KiroCredentialError:
            return self._unavailable("credential data invalid")
        except AuthenticationRequired:
            return self._unavailable("authentication required")
        except RateLimited:
            return self._unavailable("rate limit reached")
        except NetworkError:
            return self._unavailable("network request failed")
        except (KiroParseError, TypeError, ValueError):
            return self._unavailable("response data invalid")
        except Exception:
            return self._unavailable("unexpected failure")

    @staticmethod
    def _endpoint(credentials: KiroCredentials) -> str:
        query = urllib.parse.urlencode(
            {
                "origin": "AI_EDITOR",
                "resourceType": "AGENTIC_REQUEST",
                "profileArn": credentials.profile_arn,
            }
        )
        return (
            f"https://q.{credentials.region}.amazonaws.com/getUsageLimits?{query}"
        )

    @staticmethod
    def _headers(credentials: KiroCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "User-Agent": "KiroIDE",
            "x-amz-user-agent": "aws-sdk-js/1.0.0 KiroIDE",
            "amz-sdk-invocation-id": _request_id(),
            "amz-sdk-request": "attempt=1; max=1",
        }

    @staticmethod
    def _unavailable(reason: str) -> Overview:
        logger.warning("Kiro quota enrichment unavailable: %s", reason)
        return Overview([])
