"""Isolated JSON Schema validation worker for untrusted MCP schemas."""

from __future__ import annotations

import json
import sys
from typing import Any

from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry  # type: ignore[import-untyped]
from referencing.exceptions import NoSuchResource  # type: ignore[import-untyped]


MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _reject_remote_reference(uri: str) -> Any:
    raise NoSuchResource(uri)


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    schema = payload.get("schema")
    if not isinstance(schema, dict):
        return {"valid": False, "internal": True, "message": "schema is not an object"}
    dialect = schema.get("$schema", payload.get("defaultDialect"))
    if not isinstance(dialect, str) or not dialect:
        return {
            "valid": False,
            "internal": True,
            "message": "schema dialect is unavailable",
        }
    validator_class = validator_for({"$schema": dialect}, default=None)
    if validator_class is None:
        return {
            "valid": False,
            "internal": True,
            "message": f"unsupported JSON Schema dialect {dialect!r}",
        }
    registry: Registry[Any] = Registry(  # type: ignore[call-arg]
        retrieve=_reject_remote_reference
    )
    validator = validator_class(schema, registry=registry)
    try:
        validator.validate(payload.get("instance"))
    except ValidationError as exc:
        return {
            "valid": False,
            "message": exc.message,
            "path": [str(part) for part in exc.absolute_path],
        }
    except Exception as exc:  # noqa: BLE001 - returned across the process boundary
        return {
            "valid": False,
            "internal": True,
            "message": str(exc).strip() or type(exc).__name__,
        }
    return {"valid": True}


def _apply_resource_limits() -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        memory = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (OSError, ValueError):
        return


def main() -> int:
    _apply_resource_limits()
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        response = {
            "valid": False,
            "internal": True,
            "message": "validation request is too large",
        }
    else:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("validation request must be an object")
            response = _validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "valid": False,
                "internal": True,
                "message": str(exc),
            }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the runtime
    raise SystemExit(main())
