"""User-owned signed plugin marketplace registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ash.commands.config import load_config, save_config
from ash.config import (
    MAX_PLUGIN_MARKETPLACES,
    validate_plugin_marketplace_key_ids,
    validate_plugin_marketplace_source,
    validate_plugin_marketplaces,
)
from ash.plugins.catalog import (
    PluginCatalogError,
    RegisteredCatalogSource,
    fetch_catalog,
    parse_and_verify_catalog,
    trusted_catalog_keys_path,
    validate_catalog_publisher,
)
from ash.plugins.anchored_fs import (
    AnchoredDirectory,
    AnchoredFilesystemError,
    AnchoredFilesystemUnavailable,
)
from ash.plugins.lifecycle import PluginLifecycleError
from ash.profiles import active_profile_name, profile_directory
from ash.safe_io import strict_json_loads
from ash.ui.safe_text import terminal_safe_text


MARKETPLACE_TRUST_STATE_VERSION = 1
MARKETPLACE_TRUST_STATE_FILENAME = ".marketplace-trust.json"
MAX_MARKETPLACE_TRUST_STATE_BYTES = 1024 * 1024
MAX_MARKETPLACE_TRUST_PUBLISHERS = 10_000


def _marketplace_trust_state_path() -> Path:
    state_root = Path.home() / ".ash"
    profile = active_profile_name(ash_dir=state_root)
    return profile_directory(profile, ash_dir=state_root) / MARKETPLACE_TRUST_STATE_FILENAME


def _parse_marketplace_trust_state(raw: bytes) -> dict[str, int]:
    try:
        payload = strict_json_loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(
            f"invalid plugin marketplace trust state: {exc}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "publishers"}:
        raise PluginLifecycleError("invalid plugin marketplace trust state")
    if payload["version"] != MARKETPLACE_TRUST_STATE_VERSION:
        raise PluginLifecycleError("unsupported plugin marketplace trust state")
    publishers = payload["publishers"]
    if not isinstance(publishers, dict) or len(publishers) > MAX_MARKETPLACE_TRUST_PUBLISHERS:
        raise PluginLifecycleError("invalid plugin marketplace trust state")
    parsed: dict[str, int] = {}
    for raw_publisher, raw_sequence in publishers.items():
        if not isinstance(raw_publisher, str):
            raise PluginLifecycleError("invalid plugin marketplace trust state")
        publisher = validate_catalog_publisher(raw_publisher)
        if (
            isinstance(raw_sequence, bool)
            or not isinstance(raw_sequence, int)
            or raw_sequence < 1
        ):
            raise PluginLifecycleError("invalid plugin marketplace trust state")
        parsed[publisher] = raw_sequence
    return parsed


def _read_marketplace_trust_state_at(directory: AnchoredDirectory) -> dict[str, int]:
    raw = directory.read_file(
        MARKETPLACE_TRUST_STATE_FILENAME,
        max_bytes=MAX_MARKETPLACE_TRUST_STATE_BYTES,
    )
    if raw is None:
        return {}
    return _parse_marketplace_trust_state(raw)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("could not write plugin marketplace trust state")
        offset += written


def _save_marketplace_trust_state_at(
    directory: AnchoredDirectory,
    publishers: dict[str, int],
) -> None:
    if len(publishers) > MAX_MARKETPLACE_TRUST_PUBLISHERS:
        raise PluginLifecycleError("plugin marketplace trust state is full")
    payload = {
        "version": MARKETPLACE_TRUST_STATE_VERSION,
        "publishers": dict(sorted(publishers.items())),
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(data) > MAX_MARKETPLACE_TRUST_STATE_BYTES:
        raise PluginLifecycleError("plugin marketplace trust state is too large")

    temporary = directory.unique_name(".marketplace-trust-", ".tmp")
    descriptor = -1
    temporary_identity = None
    try:
        descriptor = directory.create_file(temporary, mode=0o600)
        temporary_identity = os.fstat(descriptor)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory.rename(
            temporary,
            MARKETPLACE_TRUST_STATE_FILENAME,
            expected_source=temporary_identity,
        )
        temporary = ""
        directory.sync()
    except BaseException as primary:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                primary.add_note(f"marketplace trust descriptor close failed: {cleanup}")
            descriptor = -1
        if temporary:
            try:
                directory.unlink(
                    temporary,
                    expected=temporary_identity,
                    missing_ok=True,
                )
            except BaseException as cleanup:
                primary.add_note(f"marketplace trust temporary cleanup failed: {cleanup}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def accept_registered_marketplace_sequences(candidates: dict[str, int]) -> None:
    """Atomically reject rollbacks and advance registered marketplace watermarks."""

    if not candidates:
        return
    normalized: dict[str, int] = {}
    for raw_publisher, raw_sequence in candidates.items():
        publisher = validate_catalog_publisher(raw_publisher)
        if (
            isinstance(raw_sequence, bool)
            or not isinstance(raw_sequence, int)
            or raw_sequence < 1
        ):
            raise PluginLifecycleError("invalid plugin marketplace catalog sequence")
        normalized[publisher] = raw_sequence

    state_path = _marketplace_trust_state_path()
    try:
        with (
            AnchoredDirectory.open(state_path.parent, create=True) as directory,
            directory.lock(".marketplace-trust.lock"),
        ):
            directory.prepare_durable_mutation()
            current = _read_marketplace_trust_state_at(directory)
            for publisher, sequence in normalized.items():
                highest = current.get(publisher)
                if highest is not None and sequence < highest:
                    raise PluginLifecycleError(
                        f"registered marketplace @{publisher} catalog sequence rollback: "
                        f"received {sequence}, highest accepted is {highest}"
                    )
            updated = dict(current)
            changed = False
            for publisher, sequence in normalized.items():
                highest = updated.get(publisher)
                if highest is None or sequence > highest:
                    updated[publisher] = sequence
                    changed = True
            if changed:
                _save_marketplace_trust_state_at(directory, updated)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, AnchoredFilesystemUnavailable, OSError) as exc:
        raise PluginLifecycleError(
            f"cannot update plugin marketplace trust state: {exc}"
        ) from exc


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


def registered_marketplace_selection() -> dict[str, RegisteredCatalogSource]:
    """Return registered sources bound to the signing keys accepted at registration."""

    raw = load_config(strict=True)
    if not isinstance(raw, dict):
        raise ValueError("user configuration must contain a TOML table")
    marketplaces = validate_plugin_marketplaces(raw.get("plugin_marketplaces", {}))
    key_ids = validate_plugin_marketplace_key_ids(
        raw.get("plugin_marketplace_key_ids", {})
    )
    missing = sorted(set(marketplaces) - set(key_ids))
    if missing:
        publisher = missing[0]
        raise PluginLifecycleError(
            f"registered marketplace @{publisher} has no signer binding; "
            "re-register it with `ash marketplace add` before use"
        )
    return {
        publisher: RegisteredCatalogSource(
            source=source,
            key_id=key_ids[publisher],
        )
        for publisher, source in marketplaces.items()
    }


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
    key_ids = validate_plugin_marketplace_key_ids(
        user_config.get("plugin_marketplace_key_ids", {})
    )
    existing = marketplaces.get(publisher)
    existing_key_id = key_ids.get(publisher)
    if existing is not None and existing != normalized_source and not replace:
        raise ValueError(
            f"marketplace @{publisher} is already registered from {existing}; "
            "pass --replace to change its source"
        )
    if (
        existing is not None
        and existing_key_id is not None
        and existing_key_id != verified.key_id
        and not replace
    ):
        raise ValueError(
            f"marketplace @{publisher} signing key changed from {existing_key_id!r} "
            f"to {verified.key_id!r}; pass --replace to accept the new signer"
        )
    if publisher not in marketplaces and len(marketplaces) >= MAX_PLUGIN_MARKETPLACES:
        raise ValueError(
            f"plugin_marketplaces supports at most {MAX_PLUGIN_MARKETPLACES} entries"
        )
    accept_registered_marketplace_sequences({publisher: verified.sequence})
    marketplaces[publisher] = normalized_source
    key_ids[publisher] = verified.key_id
    user_config["plugin_marketplaces"] = marketplaces
    user_config["plugin_marketplace_key_ids"] = key_ids
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
    key_ids = validate_plugin_marketplace_key_ids(
        user_config.get("plugin_marketplace_key_ids", {})
    )
    removed = marketplaces.pop(normalized, None) is not None
    key_ids.pop(normalized, None)
    if marketplaces:
        user_config["plugin_marketplaces"] = marketplaces
    else:
        user_config.pop("plugin_marketplaces", None)
    if key_ids:
        user_config["plugin_marketplace_key_ids"] = key_ids
    else:
        user_config.pop("plugin_marketplace_key_ids", None)
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
