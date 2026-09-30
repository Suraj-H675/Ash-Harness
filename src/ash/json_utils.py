"""Strict JSON parsing for configuration and protocol trust boundaries."""

from __future__ import annotations

import json
from typing import Any


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Parse JSON while rejecting duplicate object keys and invalid constants."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = item
        return result

    def reject_constant(raw: str) -> None:
        raise ValueError(f"invalid JSON constant: {raw}")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
