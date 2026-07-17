from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "openusage-bar" / "providers.json"
FORBIDDEN_KEYS = {"api_key", "secret", "token", "password", "cookie"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class MiniMaxConfig:
    provider_id: str
    name: str
    type: str = "minimax"


@dataclass(frozen=True)
class StepPlanConfig:
    provider_id: str
    name: str
    type: str = "step_plan"
    site: str = "china"


@dataclass(frozen=True)
class OpenAIOrganizationConfig:
    provider_id: str
    name: str
    type: str = "openai_organization"


@dataclass(frozen=True)
class DailyUsageFeedConfig:
    provider_id: str
    name: str
    family_id: str
    endpoint: str
    method: str
    header_name: str
    auth_prefix: str
    items_path: str
    date_path: str
    model_path: str
    input_tokens_path: str
    output_tokens_path: str
    total_tokens_path: str
    cache_read_tokens_path: str | None = None
    cache_creation_tokens_path: str | None = None
    reasoning_tokens_path: str | None = None
    cost_amount_path: str | None = None
    cost_currency: str | None = None
    timestamp_format: str = "date"
    timezone: str = "UTC"
    pagination: str = "none"
    page_parameter: str = "page"
    limit_parameter: str = "limit"
    cursor_parameter: str = "cursor"
    next_cursor_path: str | None = None
    page_size: int = 100
    since_parameter: str | None = None
    until_parameter: str | None = None
    request_body: dict[str, Any] | None = None
    type: str = "daily_usage_feed"


@dataclass(frozen=True)
class GenericProviderConfig:
    provider_id: str
    name: str
    endpoint: str
    header_name: str
    auth_prefix: str
    primary_path: str
    remaining_percent_path: str | None = None
    reset_path: str | None = None
    detail_path: str | None = None
    type: str = "generic"


ProviderConfig = (
    MiniMaxConfig
    | StepPlanConfig
    | OpenAIOrganizationConfig
    | DailyUsageFeedConfig
    | GenericProviderConfig
)


_DOTTED_PATH = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _validate_no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"Secret field {key!r} is not permitted in provider configuration")
            _validate_no_secrets(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_no_secrets(nested)


def _validate_config(config: ProviderConfig) -> None:
    if not ID_PATTERN.fullmatch(config.provider_id):
        raise ValueError("Provider ID may contain only letters, numbers, dot, underscore and dash")
    if not config.name.strip():
        raise ValueError("Provider name must not be empty")
    if isinstance(config, StepPlanConfig) and config.site not in {
        "china",
        "international",
    }:
        raise ValueError("StepFun site must be china or international")
    if (
        isinstance(config, OpenAIOrganizationConfig)
        and config.provider_id != "openai"
    ):
        raise ValueError("OpenAI Organization must use the canonical provider ID 'openai'")
    if isinstance(config, DailyUsageFeedConfig):
        if not ID_PATTERN.fullmatch(config.family_id):
            raise ValueError("Daily feed family ID is invalid")
        endpoint = urllib.parse.urlsplit(config.endpoint)
        if (
            endpoint.scheme.lower() != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.fragment
        ):
            raise ValueError("Daily feed endpoint must be credential-free HTTPS")
        if config.method not in {"GET", "POST"}:
            raise ValueError("Daily feed method must be GET or POST")
        if not _HEADER_NAME.fullmatch(config.header_name):
            raise ValueError("Daily feed header name is invalid")
        if config.header_name.casefold() in {"cookie", "set-cookie", "proxy-authorization"}:
            raise ValueError("Daily feed cookie or proxy credentials are not supported")
        paths = (
            config.items_path,
            config.date_path,
            config.model_path,
            config.input_tokens_path,
            config.output_tokens_path,
            config.total_tokens_path,
            config.cache_read_tokens_path,
            config.cache_creation_tokens_path,
            config.reasoning_tokens_path,
            config.cost_amount_path,
            config.next_cursor_path,
        )
        if any(path is not None and _DOTTED_PATH.fullmatch(path) is None for path in paths):
            raise ValueError("Daily feed field path is invalid")
        if config.timestamp_format not in {
            "date", "iso8601", "unix_seconds", "unix_milliseconds"
        }:
            raise ValueError("Daily feed timestamp format is invalid")
        if config.pagination not in {"none", "page", "offset", "cursor"}:
            raise ValueError("Daily feed pagination is invalid")
        if config.pagination == "cursor" and not config.next_cursor_path:
            raise ValueError("Cursor pagination requires a next cursor path")
        if not config.since_parameter or not config.until_parameter:
            raise ValueError("Daily feed must declare bounded date parameters")
        if isinstance(config.page_size, bool) or not 1 <= config.page_size <= 1000:
            raise ValueError("Daily feed page size must be between 1 and 1000")
        parameters = (
            config.page_parameter,
            config.limit_parameter,
            config.cursor_parameter,
            config.since_parameter,
            config.until_parameter,
        )
        if any(
            parameter is not None and _PARAMETER_NAME.fullmatch(parameter) is None
            for parameter in parameters
        ):
            raise ValueError("Daily feed parameter name is invalid")
        if (config.cost_amount_path is None) != (config.cost_currency is None):
            raise ValueError("Daily feed cost mapping requires amount and currency")
        if config.cost_currency is not None and (
            not config.cost_currency.isascii()
            or ID_PATTERN.fullmatch(config.cost_currency) is None
            or not 3 <= len(config.cost_currency) <= 8
        ):
            raise ValueError("Daily feed currency is invalid")
        if config.request_body is not None and not isinstance(config.request_body, dict):
            raise ValueError("Daily feed request body must be an object")
        _validate_no_secrets(config.request_body)


def validate_provider_config(config: ProviderConfig) -> None:
    _validate_config(config)
    _validate_no_secrets(asdict(config))


class ProviderConfigStore:
    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path

    def save(self, configs: list[ProviderConfig]) -> None:
        ids = [config.provider_id for config in configs]
        if len(ids) != len(set(ids)):
            raise ValueError("Provider IDs must be unique")
        for config in configs:
            validate_provider_config(config)
        payload = {"version": 1, "providers": [asdict(config) for config in configs]}
        _validate_no_secrets(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="providers.", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> list[ProviderConfig]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        _validate_no_secrets(payload)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Unsupported provider configuration version")
        raw_configs = payload.get("providers")
        if not isinstance(raw_configs, list):
            raise ValueError("Provider configuration must contain a providers list")
        configs: list[ProviderConfig] = []
        for raw in raw_configs:
            if not isinstance(raw, dict):
                raise ValueError("Each provider configuration must be an object")
            kind = raw.get("type")
            if kind == "minimax":
                config: ProviderConfig = MiniMaxConfig(**raw)
            elif kind == "step_plan":
                config = StepPlanConfig(**raw)
            elif kind == "openai_organization":
                config = OpenAIOrganizationConfig(**raw)
            elif kind == "daily_usage_feed":
                config = DailyUsageFeedConfig(**raw)
            elif kind == "generic":
                config = GenericProviderConfig(**raw)
            else:
                raise ValueError(f"Unsupported provider type: {kind}")
            _validate_config(config)
            configs.append(config)
        ids = [config.provider_id for config in configs]
        if len(ids) != len(set(ids)):
            raise ValueError("Provider IDs must be unique")
        return configs
