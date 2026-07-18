from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from .contracts import ProviderBinding


class UnknownProviderConfig(ValueError):
    """Raised when no exact config type has an explicitly registered factory."""


GlobalFactory = Callable[[], ProviderBinding]
ConfigFactory = Callable[[Any], ProviderBinding]


def _source_id(source: object, family: str) -> str:
    for attribute in ("source_id", f"{family}_source_id"):
        value = getattr(source, attribute, None)
        if isinstance(value, str) and value:
            return value
    source_type = type(source)
    return f"{source_type.__module__}.{source_type.__qualname__}"


def _source_priority(source: object) -> int:
    value = getattr(source, "source_priority", 100)
    return value if isinstance(value, int) and not isinstance(value, bool) else 100


def _normalized(binding: ProviderBinding) -> ProviderBinding:
    if not binding.provider_id or not binding.family_id:
        raise ValueError("Provider bindings require stable provider and family IDs")

    def normalize(sources: Iterable[object], family: str) -> tuple[Any, ...]:
        ordered = tuple(sorted(
            sources,
            key=lambda source: (
                _source_priority(source), _source_id(source, family),
                type(source).__module__, type(source).__qualname__,
            ),
        ))
        identifiers = [_source_id(source, family) for source in ordered]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(
                f"provider {binding.provider_id!r} has duplicate {family} source IDs"
            )
        return ordered

    return replace(
        binding,
        quota_sources=normalize(binding.quota_sources, "quota"),
        usage_sources=normalize(binding.usage_sources, "usage"),
        cost_sources=normalize(binding.cost_sources, "cost"),
    )


class AdapterRegistry:
    def __init__(self) -> None:
        self._global_factories: list[GlobalFactory] = []
        self._config_factories: dict[type[object], ConfigFactory] = {}

    def register_global(self, factory: GlobalFactory) -> None:
        self._global_factories.append(factory)

    def register_config(
        self, config_type: type[object], factory: ConfigFactory
    ) -> None:
        if config_type in self._config_factories:
            raise ValueError(f"config type {config_type.__name__!r} is already registered")
        self._config_factories[config_type] = factory

    def build(self, configs: Iterable[object]) -> tuple[ProviderBinding, ...]:
        bindings = [_normalized(factory()) for factory in self._global_factories]
        for config in configs:
            factory = self._config_factories.get(type(config))
            if factory is None:
                raise UnknownProviderConfig(
                    f"provider config type {type(config).__name__!r} is not registered"
                )
            binding = _normalized(factory(config))
            configured_id = getattr(config, "provider_id", None)
            if binding.provider_id != configured_id:
                raise ValueError("Provider factory changed the configured provider ID")
            bindings.append(binding)

        provider_ids = [binding.provider_id for binding in bindings]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Provider registry produced duplicate provider IDs")
        return tuple(sorted(bindings, key=lambda binding: binding.provider_id))
