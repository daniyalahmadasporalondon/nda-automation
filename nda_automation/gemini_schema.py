from __future__ import annotations

from copy import deepcopy
from typing import Any


GEMINI_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",
    "const",
    "maximum",
    "minimum",
}


def gemini_compatible_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of JSON Schema accepted by Gemini responseSchema."""
    return _strip_unsupported_schema_keys(deepcopy(schema))


def _strip_unsupported_schema_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_unsupported_schema_keys(child)
            for key, child in value.items()
            if key not in GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [_strip_unsupported_schema_keys(item) for item in value]
    return value
