from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    ID_PATTERN,
    ProviderConfigStore,
)


REQUIRED_CASES = frozenset({
    "success",
    "empty_result",
    "authentication_expiry",
    "rate_limit",
    "timeout",
    "malformed_response",
    "oversized_response",
    "pagination_loop",
    "partial_coverage",
    "last_good_preservation",
    "source_priority",
    "multi_account_isolation",
    "secret_non_disclosure",
    "unknown_not_zero",
})
EXPECTED_OUTCOMES = {
    "success": "fact",
    "empty_result": "covered_empty",
    "authentication_expiry": "auth_failure",
    "rate_limit": "rate_limited",
    "timeout": "temporarily_unavailable",
    "malformed_response": "invalid_response",
    "oversized_response": "response_too_large",
    "pagination_loop": "invalid_response",
    "partial_coverage": "partial",
    "last_good_preservation": "stale_last_good",
    "source_priority": "one_effective_source",
    "multi_account_isolation": "isolated",
    "secret_non_disclosure": "redacted",
    "unknown_not_zero": "missing",
}

_FORBIDDEN_KEYS = frozenset({
    "cookie", "cookies", "set_cookie", "prompt", "prompts", "response",
    "responses", "raw_payload", "raw_metadata", "conversation", "messages",
})
_FORBIDDEN_TEXT = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/)[^\s\"']+"),
    re.compile(r"\b(?:Oasis-Token|INGRESSCOOKIE|_wafdytoken|__stripe_mid)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class ProviderFixture:
    fixture_id: str
    families: frozenset[str]
    runtime_sources: frozenset[str]
    catalog_source_ids: frozenset[str]
    catalog_provenances: frozenset[str]
    cases: frozenset[str]
    unknown_value: None
    fake_account_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProviderAdapterBundle:
    fixture: ProviderFixture
    configs: tuple[object, ...]


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield None, child
            yield from _walk(child)


def _validate_redacted(value: Any) -> None:
    for key, child in _walk(value):
        if key is not None and key.lower().replace("-", "_") in _FORBIDDEN_KEYS:
            raise ValueError("provider fixture contains forbidden user-content metadata")
        if isinstance(child, str):
            if len(child) > 4096 or any(pattern.search(child) for pattern in _FORBIDDEN_TEXT):
                raise ValueError("provider fixture contains credential or identity material")


def load_provider_fixture(path: Path) -> ProviderFixture:
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("provider fixture is unavailable or oversized")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("provider fixture schema is unsupported")
    _validate_redacted(raw)
    cases = frozenset(raw.get("cases", ()))
    if cases != REQUIRED_CASES:
        raise ValueError("provider fixture does not cover the conformance matrix")
    if raw.get("unknown_value", "missing") is not None:
        raise ValueError("unknown facts must be represented by null, never zero")
    accounts = tuple(raw.get("fake_account_refs", ()))
    if len(accounts) < 2 or len(accounts) != len(set(accounts)):
        raise ValueError("provider fixture must prove isolated synthetic accounts")
    fixture_id = raw.get("fixture_id")
    if (
        not isinstance(fixture_id, str)
        or len(fixture_id) > 64
        or ID_PATTERN.fullmatch(fixture_id) is None
    ):
        raise ValueError("provider fixture identity is invalid")
    fixture = ProviderFixture(
        fixture_id=fixture_id,
        families=frozenset(raw.get("families", ())),
        runtime_sources=frozenset(raw.get("runtime_sources", ())),
        catalog_source_ids=frozenset(raw.get("catalog_source_ids", ())),
        catalog_provenances=frozenset(raw.get("catalog_provenances", ())),
        cases=cases,
        unknown_value=None,
        fake_account_refs=accounts,
    )
    if fixture.fixture_id != path.parent.name:
        raise ValueError("provider fixture identity must match its directory")
    return fixture


def load_provider_fixtures(root: Path) -> tuple[ProviderFixture, ...]:
    manifests = sorted(root.glob("*/manifest.json"))
    if not manifests:
        raise ValueError("provider conformance fixtures are unavailable")
    fixtures = [load_provider_fixture(path) for path in manifests]
    ids = [fixture.fixture_id for fixture in fixtures]
    if len(ids) != len(set(ids)):
        raise ValueError("provider fixture identities must be unique")
    return tuple(fixtures)


def load_provider_adapter_bundle(root: Path) -> ProviderAdapterBundle:
    fixture = load_provider_fixture(root / "manifest.json")
    conformance_path = root / "fixtures" / "conformance.json"
    if not conformance_path.is_file() or conformance_path.stat().st_size > 64 * 1024:
        raise ValueError("provider conformance example is unavailable or oversized")
    conformance = json.loads(conformance_path.read_text(encoding="utf-8"))
    if (
        not isinstance(conformance, dict)
        or conformance.get("schema_version") != 1
        or conformance.get("unknown_value", "missing") is not None
    ):
        raise ValueError("provider conformance example schema is unsupported")
    _validate_redacted(conformance)
    cases = conformance.get("cases")
    if not isinstance(cases, dict) or set(cases) != REQUIRED_CASES:
        raise ValueError("provider conformance example does not cover the conformance matrix")
    for name, expected in EXPECTED_OUTCOMES.items():
        if cases.get(name) != {"expected": expected}:
            raise ValueError("provider conformance example has an invalid outcome")
    if tuple(conformance.get("fake_account_refs", ())) != fixture.fake_account_refs:
        raise ValueError("provider conformance example account scopes do not match")

    templates = (
        ("quota.providers.json", GenericProviderConfig),
        ("daily-usage.providers.json", DailyUsageFeedConfig),
        ("daily-cost.providers.json", DailyCostFeedConfig),
    )
    configs: list[object] = []
    for filename, expected_type in templates:
        path = root / filename
        if not path.is_file():
            raise ValueError("provider bundle requires three declarative templates")
        loaded = ProviderConfigStore(path).load()
        if len(loaded) != 1 or type(loaded[0]) is not expected_type:
            raise ValueError("provider bundle template kind is invalid")
        configs.append(loaded[0])
    provider_ids = [getattr(config, "provider_id", None) for config in configs]
    if len(provider_ids) != len(set(provider_ids)):
        raise ValueError("provider bundle template IDs must be unique")
    expected_sources = frozenset({
        "generic.quota",
        "custom.daily_feed",
        "custom.cost_feed",
    })
    if fixture.runtime_sources != expected_sources:
        raise ValueError("provider bundle sources must use the declarative adapters")
    built_in_fixtures = load_provider_fixtures(
        Path(__file__).parent / "fixtures" / "providers"
    )
    covered_sources = frozenset(
        source
        for built_in_fixture in built_in_fixtures
        for source in built_in_fixture.runtime_sources
    )
    if not expected_sources <= covered_sources:
        raise ValueError("provider bundle sources lack runtime conformance evidence")
    return ProviderAdapterBundle(fixture, tuple(configs))


def source_identifier(source: object, family: str) -> str:
    for attribute in ("source_id", f"{family}_source_id"):
        value = getattr(source, attribute, None)
        if isinstance(value, str) and value:
            return value
    source_type = type(source)
    return f"{source_type.__module__}.{source_type.__qualname__}"


def runtime_inventory(bindings: Iterable[object]) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    for binding in bindings:
        for fact, attribute in (
            ("quota", "quota_sources"),
            ("usage", "usage_sources"),
            ("cost", "cost_sources"),
        ):
            for source in getattr(binding, attribute):
                rows.append((binding.family_id, fact, source_identifier(source, fact)))
    return tuple(sorted(rows))


def assert_registry_catalog_agreement(
    bindings: Iterable[object], catalog: object, fixtures: Iterable[ProviderFixture]
) -> None:
    fixtures = tuple(fixtures)
    covered_runtime = frozenset(
        source for fixture in fixtures for source in fixture.runtime_sources
    )
    inventory = runtime_inventory(bindings)
    missing_runtime = sorted(
        source for _, _, source in inventory if source not in covered_runtime
    )
    if missing_runtime:
        raise AssertionError(f"runtime sources lack conformance provenance: {missing_runtime}")

    families = {family.family_id: family for family in catalog.families}
    runtime_families = {family for family, _, _ in inventory}
    unknown_families = runtime_families - set(families) - {"openusage"}
    if unknown_families:
        raise AssertionError(f"runtime families are absent from catalog: {sorted(unknown_families)}")

    covered_families = frozenset(
        family for fixture in fixtures for family in fixture.families
    )
    covered_source_ids = frozenset(
        source for fixture in fixtures for source in fixture.catalog_source_ids
    )
    covered_provenances = frozenset(
        provenance
        for fixture in fixtures
        for provenance in fixture.catalog_provenances
    )
    missing_supported: list[str] = []
    for family in catalog.families:
        capabilities = family.capabilities
        supported = (
            capabilities.quota_windows.state == "supported"
            or any(
                getattr(capabilities, field) == "supported"
                for field in (
                    "token_history", "model_breakdown", "reset_timestamps",
                    "billing", "credits", "balance", "cost", "rate_limits",
                    "service_status",
                )
            )
        )
        if not supported:
            continue
        has_evidence = family.family_id in covered_families or any(
            source.source_id in covered_source_ids
            or source.provenance in covered_provenances
            for source in family.sources
        )
        if not has_evidence:
            missing_supported.append(family.family_id)
    if missing_supported:
        raise AssertionError(
            f"supported catalog families lack source fixtures: {sorted(missing_supported)}"
        )
