"""User-owned signed plugin marketplace registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ash.commands.config import load_config, save_config
from ash.config import (
    MAX_PLUGIN_MARKETPLACES,
    validate_plugin_marketplace_source,
    validate_plugin_marketplaces,
)
from ash.plugins.catalog import (
    PluginCatalogError,
    fetch_catalog,
    parse_and_verify_catalog,
    trusted_catalog_keys_path,
    validate_catalog_publisher,
)
from ash.ui.safe_text import terminal_safe_text


def _normalized_source(source: str) -> str:
    validated = validate_plugin_marketplace_source(source, publisher="marketplace")
    if "://" in validated:
        return validated
    return str(Path(os.path.abspath(Path(validated).expanduser())))


def _verify_source(source: str):
    try:
        path = fetch_catalog(source) if "://" in source else Path(source)
        return parse_and_verify_catalog(
            path,
            trusted_keys_path=trusted_catalog_keys_path(),
        )
    except (OSError, ValueError) as exc:
        raise PluginCatalogError(str(exc)) from exc


def registered_marketplaces() -> dict[str, str]:
    """Load only the current profile's user-owned marketplace registry."""

    raw = load_config(strict=True)
    if not isinstance(raw, dict):
        raise ValueError("user configuration must contain a TOML table")
    return validate_plugin_marketplaces(raw.get("plugin_marketplaces", {}))


def add_marketplace(source: str, *, replace: bool = False) -> dict[str, Any]:
    """Verify a signed v2 catalog and persist its signed publisher identity."""

    normalized_source = _normalized_source(source)
    verified = _verify_source(normalized_source)
    if verified.publisher is None:
        raise PluginCatalogError(
            "persistent marketplaces require catalog version 2 signed publisher identity"
        )
    publisher = validate_catalog_publisher(verified.publisher)

    user_config = load_config(strict=True)
    if not isinstance(user_config, dict):
        raise ValueError("user configuration must contain a TOML table")
    marketplaces = validate_plugin_marketplaces(
        user_config.get("plugin_marketplaces", {})
    )
    existing = marketplaces.get(publisher)
    if existing is not None and existing != normalized_source and not replace:
        raise ValueError(
            f"marketplace @{publisher} is already registered from {existing}; "
            "pass --replace to change its source"
        )
    if publisher not in marketplaces and len(marketplaces) >= MAX_PLUGIN_MARKETPLACES:
        raise ValueError(
            f"plugin_marketplaces supports at most {MAX_PLUGIN_MARKETPLACES} entries"
        )
    marketplaces[publisher] = normalized_source
    user_config["plugin_marketplaces"] = marketplaces
    save_config(user_config)
    return {
        "action": "add",
        "publisher": publisher,
        "source": normalized_source,
    }


def remove_marketplace(publisher: str) -> dict[str, Any]:
    """Remove one publisher mapping without contacting its catalog source."""

    normalized = validate_catalog_publisher(publisher)
    user_config = load_config(strict=True)
    if not isinstance(user_config, dict):
        raise ValueError("user configuration must contain a TOML table")
    marketplaces = validate_plugin_marketplaces(
        user_config.get("plugin_marketplaces", {})
    )
    removed = marketplaces.pop(normalized, None) is not None
    if marketplaces:
        user_config["plugin_marketplaces"] = marketplaces
    else:
        user_config.pop("plugin_marketplaces", None)
    save_config(user_config)
    return {"action": "remove", "publisher": normalized, "removed": removed}


def render_marketplaces(
    marketplaces: dict[str, str],
    *,
    json_output: bool = False,
) -> str:
    payload = {
        "marketplaces": [
            {"publisher": publisher, "source": source}
            for publisher, source in sorted(marketplaces.items())
        ]
    }
    if json_output:
        return json.dumps(payload, sort_keys=True)
    if not marketplaces:
        return "No plugin marketplaces registered."
    return "\n".join(
        f"@{terminal_safe_text(publisher, single_line=True)} "
        f"{terminal_safe_text(source, single_line=True)}"
        for publisher, source in sorted(marketplaces.items())
    )


def render_marketplace_action(result: dict[str, Any], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(result, sort_keys=True)
    publisher = terminal_safe_text(str(result["publisher"]), single_line=True)
    if result["action"] == "add":
        source = terminal_safe_text(str(result["source"]), single_line=True)
        return f"Registered marketplace @{publisher}: {source}"
    return (
        f"Removed marketplace @{publisher}."
        if result.get("removed")
        else f"Marketplace @{publisher} was not registered."
    )
