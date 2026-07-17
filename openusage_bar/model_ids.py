from __future__ import annotations

import hashlib
import re
from typing import Any

from .config import ID_PATTERN


MAX_MODEL_LABEL_LENGTH = 4096
_MODEL_HASH_LENGTH = 12
_MAX_CANONICAL_MODEL_ID_LENGTH = 96


class InvalidModelID(ValueError):
    pass


def canonical_model_id(value: Any, *, allow_missing: bool = False) -> str:
    """Return a bounded ledger-safe model ID without retaining a raw unsafe label."""
    if value is None and allow_missing:
        return "unknown"
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MODEL_LABEL_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise InvalidModelID("invalid model")
    if ID_PATTERN.fullmatch(value) is not None:
        return value
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("._-")
    readable = readable.lower() or "model"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_MODEL_HASH_LENGTH]
    prefix_length = _MAX_CANONICAL_MODEL_ID_LENGTH - len(suffix) - 1
    return f"{readable[:prefix_length].rstrip('._-') or 'model'}-{suffix}"
