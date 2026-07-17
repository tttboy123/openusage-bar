from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ID_PATTERN
from .models import Overview


DEFAULT_VISIBILITY_PATH = Path.home() / ".config" / "openusage-bar" / "visibility.json"


@dataclass(frozen=True)
class ProviderVisibilityRow:
    provider_id: str
    name: str
    visible: bool


class ProviderVisibilityStore:
    def __init__(self, path: Path = DEFAULT_VISIBILITY_PATH) -> None:
        self.path = path

    def load(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return set()
            values = payload.get("hidden_provider_ids")
            if not isinstance(values, list):
                return set()
            hidden = {
                value
                for value in values
                if isinstance(value, str) and ID_PATTERN.fullmatch(value)
            }
            return hidden if len(hidden) == len(values) else set()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return set()

    def save(self, hidden_provider_ids: set[str]) -> None:
        if any(
            not isinstance(provider_id, str) or not ID_PATTERN.fullmatch(provider_id)
            for provider_id in hidden_provider_ids
        ):
            raise ValueError("Invalid hidden provider ID")
        payload = {
            "version": 1,
            "hidden_provider_ids": sorted(hidden_provider_ids),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="visibility.", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def visible_overview(
    overview: Overview, hidden_provider_ids: set[str]
) -> Overview:
    return Overview(
        [
            card
            for card in overview.cards
            if card.provider_id not in hidden_provider_ids
        ]
    )


def visibility_rows(
    overview: Overview, hidden_provider_ids: set[str]
) -> list[ProviderVisibilityRow]:
    return [
        ProviderVisibilityRow(
            provider_id=card.provider_id,
            name=card.name,
            visible=card.provider_id not in hidden_provider_ids,
        )
        for card in sorted(overview.cards, key=lambda item: item.name.casefold())
    ]


def hidden_ids_from_selection(
    rows: list[ProviderVisibilityRow], checked_provider_ids: set[str]
) -> set[str]:
    return {row.provider_id for row in rows} - checked_provider_ids
