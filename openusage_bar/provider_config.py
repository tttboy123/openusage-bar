"""CC Switch-style Provider configuration presets and agent config writers.

UsageHub can configure the coding agents it observes the same way CC Switch
does: pick an official/gateway preset, fill API key / base URL / model (custom
endpoints allowed), save, and the credential is written into that agent's own
config file (``~/.claude/settings.json``, ``~/.codex/config.toml``,
``~/.gemini/settings.json`` or ``~/.config/opencode/opencode.json``).

The writer is deliberately bounded: it only ever touches the one target config
file under the current user's home directory, preserves every unrelated field
already present in that file, writes atomically, and never echoes the API key
back to the caller or into diagnostics.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

RESOURCE_PATH = Path(__file__).parent / "resources" / "provider-config-presets.v1.json"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_FIELD_BYTES = 4096
AGENTS = frozenset({"claude_code", "codex", "gemini_cli", "opencode"})
CATEGORIES = frozenset({"official", "gateway"})
AGENT_CONFIG_RELATIVES = {
    "claude_code": Path(".claude") / "settings.json",
    "codex": Path(".codex") / "config.toml",
    "gemini_cli": Path(".gemini") / "settings.json",
    "opencode": Path(".config") / "opencode" / "opencode.json",
}
_CTRL = frozenset(chr(c) for c in range(0x20)) | {"\x7f"}


@dataclass(frozen=True)
class ProviderConfigPreset:
    preset_id: str
    name: str
    category: str
    agent: str
    family_id: str
    console_url: str
    api_key_url: str | None
    template: Mapping[str, str]
    allow_custom_endpoints: bool

    def template_value(self, key: str) -> str | None:
        value = self.template.get(key)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class AgentConfigResult:
    agent: str
    preset_id: str
    name: str
    category: str
    status: str  # created | updated


def _valid_field(value: object, *, required: bool = True, allow_env: bool = False) -> str | None:
    if value is None:
        return None if not required else None
    if not isinstance(value, str):
        raise ValueError("provider config field is invalid")
    if len(value.encode("utf-8")) > MAX_FIELD_BYTES:
        raise ValueError("provider config field is too large")
    if any(ch in _CTRL for ch in value):
        raise ValueError("provider config field contains control characters")
    stripped = value.strip()
    if allow_env and stripped.startswith("${") and stripped.endswith("}"):
        return value
    if not stripped:
        return None if not required else None
    return value


def load_presets(path: Path | None = None) -> tuple[ProviderConfigPreset, ...]:
    source = RESOURCE_PATH if path is None else path
    if not source.is_file() or source.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("provider config presets are unavailable")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("provider config presets are unavailable") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("provider config preset schema is unsupported")
    raw_presets = payload.get("presets")
    if not isinstance(raw_presets, list) or not raw_presets:
        raise ValueError("provider config preset catalog is empty")
    presets: list[ProviderConfigPreset] = []
    seen: set[str] = set()
    for raw in raw_presets:
        if not isinstance(raw, dict):
            raise ValueError("provider config preset is invalid")
        preset_id = raw.get("preset_id")
        name = raw.get("name")
        category = raw.get("category")
        agent = raw.get("agent")
        family_id = raw.get("family_id")
        console_url = raw.get("console_url")
        template = raw.get("template")
        if (
            not isinstance(preset_id, str) or not preset_id
            or not isinstance(name, str) or not name
            or category not in CATEGORIES
            or agent not in AGENTS
            or not isinstance(family_id, str) or not family_id
            or not isinstance(console_url, str) or not console_url.startswith("https://")
            or not isinstance(template, dict)
        ):
            raise ValueError("provider config preset is invalid")
        if preset_id in seen or len(preset_id) > 128:
            raise ValueError("provider config preset identity is invalid")
        api_key_url = raw.get("api_key_url")
        if api_key_url is not None and (
            not isinstance(api_key_url, str) or not api_key_url.startswith("https://")
        ):
            raise ValueError("provider config preset is invalid")
        allow_custom = raw.get("allow_custom_endpoints")
        if type(allow_custom) is not bool:
            raise ValueError("provider config preset is invalid")
        clean_template: dict[str, str] = {}
        for key, value in template.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("provider config preset template is invalid")
            clean_template[key] = value
        seen.add(preset_id)
        presets.append(ProviderConfigPreset(
            preset_id=preset_id,
            name=name,
            category=category,
            agent=agent,
            family_id=family_id,
            console_url=console_url,
            api_key_url=api_key_url,
            template=MappingProxy(clean_template),
            allow_custom_endpoints=allow_custom,
        ))
    return tuple(presets)


class MappingProxy:
    """Minimal immutable mapping facade (avoids importing types.MappingProxyType)."""

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_values", dict(values))

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def find_preset(presets: tuple[ProviderConfigPreset, ...], preset_id: str) -> ProviderConfigPreset:
    for preset in presets:
        if preset.preset_id == preset_id:
            return preset
    raise ValueError("provider config preset is unknown")


def agent_config_path(home: Path, agent: str) -> Path:
    if agent not in AGENTS:
        raise ValueError("provider config agent is unsupported")
    relative = AGENT_CONFIG_RELATIVES[agent]
    if not home.is_absolute():
        raise ValueError("provider config home must be absolute")
    candidate = home / relative
    # Bound the resolved path to the home directory.
    try:
        candidate.relative_to(home)
    except ValueError:
        raise ValueError("provider config path is invalid") from None
    return candidate


def _read_existing(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("provider config file is too large")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".usagehub-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_claude_settings(existing: str | None, *, base_url: str, api_key: str, model: str) -> str:
    payload: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(existing)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    env = payload.get("env")
    if not isinstance(env, dict):
        env = {}
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["ANTHROPIC_MODEL"] = model
    payload["env"] = env
    return json.dumps(payload, ensure_ascii=True, indent=2) + "\n"


def render_gemini_settings(existing: str | None, *, base_url: str, api_key: str, model: str) -> str:
    payload: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(existing)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    payload["GOOGLE_API_KEY"] = api_key
    payload["GOOGLE_GEMINI_BASE_URL"] = base_url
    payload["GOOGLE_GEMINI_MODEL"] = model
    return json.dumps(payload, ensure_ascii=True, indent=2) + "\n"


def render_opencode_config(existing: str | None, *, provider_id: str, npm: str, base_url: str, api_key: str, model: str) -> str:
    payload: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(existing)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    providers = payload.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider_id] = {
        "npm": npm,
        "name": provider_id,
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": {model: {"name": model}},
    }
    payload["provider"] = providers
    payload["model"] = f"{provider_id}/{model}"
    return json.dumps(payload, ensure_ascii=True, indent=2) + "\n"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for char in ("\n", "\r", "\t"):
        escaped = escaped.replace(char, {"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
    return f'"{escaped}"'


_BARE_KEY = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _toml_key(key: str) -> str:
    if key and all(ch in _BARE_KEY for ch in key):
        return key
    return _toml_string(key)


def _dump_toml(value: Any, *, path: str = "") -> str:
    if not isinstance(value, dict):
        return ""
    lines: list[str] = []
    scalars: list[str] = []
    tables: list[tuple[str, dict]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            tables.append((str(key), item))
        elif isinstance(item, list):
            rendered = ", ".join(_toml_string(str(v)) for v in item)
            scalars.append(f"{_toml_key(str(key))} = [{rendered}]")
        elif isinstance(item, bool):
            scalars.append(f"{_toml_key(str(key))} = {'true' if item else 'false'}")
        elif isinstance(item, int):
            scalars.append(f"{_toml_key(str(key))} = {item}")
        elif item is None:
            continue
        else:
            scalars.append(f"{_toml_key(str(key))} = {_toml_string(str(item))}")
    lines.extend(scalars)
    for key, table in tables:
        table_segment = _toml_key(str(key))
        table_path = f"{path}.{table_segment}" if path else table_segment
        rendered = _dump_toml(table, path=table_path).rstrip("\n")
        if rendered:
            if lines:
                lines.append("")
            lines.append(f"[{table_path}]")
            lines.append(rendered)
    return "\n".join(lines) + "\n"


def render_codex_toml(existing: str | None, *, provider_id: str, base_url: str, env_key: str, model: str) -> str:
    parsed: dict[str, Any] = {}
    if existing:
        try:
            loaded = tomllib.loads(existing)
        except tomllib.TOMLDecodeError:
            # Never rewrite a config we cannot parse: doing so could corrupt an
            # unrelated section. Fail closed instead of clobbering the file.
            raise ValueError("existing Codex config is not valid TOML") from None
        if isinstance(loaded, dict):
            parsed = loaded
    providers = parsed.get("model_providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[provider_id] = {
        "name": provider_id,
        "base_url": base_url,
        "env_key": env_key,
        "wire_api": "responses",
    }
    parsed["model_providers"] = providers
    parsed["model"] = model
    parsed["model_provider"] = provider_id
    # Keep a stable key order for deterministic diffs.
    ordered = {
        "model": parsed["model"],
        "model_provider": parsed["model_provider"],
        "model_providers": providers,
    }
    for key, value in parsed.items():
        if key not in ordered:
            ordered[key] = value
    return _dump_toml(ordered)


def apply_provider_config(
    *,
    preset_id: str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    home: Path | None = None,
    presets: tuple[ProviderConfigPreset, ...] | None = None,
    clock=None,
) -> AgentConfigResult:
    catalog = load_presets() if presets is None else presets
    preset = find_preset(catalog, preset_id)
    home_path = Path.home() if home is None else home
    if not home_path.is_absolute():
        raise ValueError("provider config home must be absolute")
    secret = _valid_field(api_key, required=True)
    if secret is None:
        raise ValueError("provider config API key is required")
    resolved_base = _valid_field(
        base_url if base_url is not None else preset.template_value("base_url"),
        required=True,
    )
    resolved_model = _valid_field(
        model if model is not None else preset.template_value("model"),
        required=True,
    )
    if resolved_base is None or resolved_model is None:
        raise ValueError("provider config template is incomplete")
    if not preset.allow_custom_endpoints and base_url is not None and base_url != preset.template_value("base_url"):
        raise ValueError("provider config custom endpoints are not allowed for this preset")

    target = agent_config_path(home_path, preset.agent)
    existing = _read_existing(target)
    status = "updated" if existing is not None else "created"
    env_key = preset.template_value("api_key_env") or "API_KEY"
    provider_id = _provider_id(preset)

    if preset.agent == "claude_code":
        content = render_claude_settings(
            existing, base_url=resolved_base, api_key=secret, model=resolved_model
        )
    elif preset.agent == "gemini_cli":
        content = render_gemini_settings(
            existing, base_url=resolved_base, api_key=secret, model=resolved_model
        )
    elif preset.agent == "opencode":
        npm = preset.template_value("npm") or "@ai-sdk/openai-compatible"
        content = render_opencode_config(
            existing,
            provider_id=provider_id,
            npm=npm,
            base_url=resolved_base,
            api_key=secret,
            model=resolved_model,
        )
    elif preset.agent == "codex":
        content = render_codex_toml(
            existing,
            provider_id=provider_id,
            base_url=resolved_base,
            env_key=env_key,
            model=resolved_model,
        )
    else:
        raise ValueError("provider config agent is unsupported")

    _atomic_write(target, content)
    return AgentConfigResult(
        agent=preset.agent,
        preset_id=preset.preset_id,
        name=preset.name,
        category=preset.category,
        status=status,
    )


def _provider_id(preset: ProviderConfigPreset) -> str:
    slug = "".join(ch for ch in preset.family_id.lower() if ch.isalnum() or ch in "_-")
    if not slug:
        raise ValueError("provider config preset family is invalid")
    return slug
