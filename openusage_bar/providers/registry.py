from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from .contracts import ProviderBinding


class UnknownProviderConfig(ValueError):
    """Raised when no exact config type has an explicitly registered factory."""


GlobalFactory = Callable[[], ProviderBinding]
ConfigFactory = Callable[[Any], ProviderBinding]
GlobalAvailability = Callable[[], bool]
ConfigAvailability = Callable[[Any], bool]


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
        balance_sources=normalize(binding.balance_sources, "balance"),
        quota_sources=normalize(binding.quota_sources, "quota"),
        usage_sources=normalize(binding.usage_sources, "usage"),
        cost_sources=normalize(binding.cost_sources, "cost"),
    )


class AdapterRegistry:
    def __init__(self) -> None:
        self._global_factories: list[
            tuple[GlobalFactory, GlobalAvailability | None]
        ] = []
        self._config_factories: dict[
            type[object], tuple[ConfigFactory, ConfigAvailability | None]
        ] = {}

    def register_global(
        self,
        factory: GlobalFactory,
        availability: GlobalAvailability | None = None,
    ) -> None:
        self._global_factories.append((factory, availability))

    def register_config(
        self,
        config_type: type[object],
        factory: ConfigFactory,
        availability: ConfigAvailability | None = None,
    ) -> None:
        if config_type in self._config_factories:
            raise ValueError(f"config type {config_type.__name__!r} is already registered")
        self._config_factories[config_type] = (factory, availability)

    def build(self, configs: Iterable[object]) -> tuple[ProviderBinding, ...]:
        bindings = [
            _normalized(factory())
            for factory, availability in self._global_factories
            if availability is None or availability() is True
        ]
        for config in configs:
            registration = self._config_factories.get(type(config))
            if registration is None:
                raise UnknownProviderConfig(
                    f"provider config type {type(config).__name__!r} is not registered"
                )
            factory, availability = registration
            if availability is not None and availability(config) is not True:
                continue
            binding = _normalized(factory(config))
            configured_id = getattr(config, "provider_id", None)
            if binding.provider_id != configured_id:
                raise ValueError("Provider factory changed the configured provider ID")
            bindings.append(binding)

        provider_ids = [binding.provider_id for binding in bindings]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("Provider registry produced duplicate provider IDs")
        return tuple(sorted(bindings, key=lambda binding: binding.provider_id))
