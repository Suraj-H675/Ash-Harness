"""Extension catalog, lifecycle orchestration, and CLI rendering helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import SpecifierSet
from packaging.version import parse as parse_version

from ash.core.redaction import redact_text, redact_urls_in_text
from ash.plugins.catalog import (
    CatalogEntry,
    RegisteredCatalogSource,
    SignedCatalog,
    fetch_catalog,
    default_catalog_source,
    parse_and_verify_catalog,
    trusted_catalog_keys_path,
)
from ash.plugins.inventory import ExtensionInventory
from ash.plugins.errors import PluginDependencyError, PluginLifecycleError
from ash.plugins.install_records import (
    PluginInstallRecord,
    load_plugin_install_records,
    user_plugin_root,
)
from ash.plugins.lifecycle import (
    InstalledPlugin,
    install_git_plugin,
    install_local_plugin,
    load_installed_plugin_manifests,
    load_managed_plugin_for_update,
    recover_plugin_lifecycle,
    set_local_plugin_enabled,
    uninstall_local_plugin,
)
from ash.plugins.state import (
    ExtensionState,
    PendingPluginUpdate,
    begin_coordinated_plugin_update,
    finish_coordinated_plugin_update,
    load_extension_state,
)
from ash.plugins.snapshot import PluginSnapshot
from ash.plugins.validation import validate_plugin_contents, validate_plugin_contents_at
from ash.plugins.manifest import PluginManifest
from ash.ui.safe_text import terminal_safe_text

ExtensionKind = Literal["all", "skills", "agents", "plugins", "hooks"]
PluginAction = Literal["install", "enable", "disable", "uninstall"]
MAX_PLUGIN_DIAGNOSTIC_CHARS = 512
CatalogSource = Path | str
CatalogSelection = (
    CatalogSource
    | Sequence[CatalogSource]
    | Mapping[str, CatalogSource | RegisteredCatalogSource]
    | None
)
ExtensionAction = Literal[
    "all",
    "skills",
    "agents",
    "plugins",
    "hooks",
    "search",
    "install",
    "update",
    "enable",
    "disable",
    "uninstall",
]


def safe_plugin_diagnostic(
    value: object,
    *,
    max_chars: int = MAX_PLUGIN_DIAGNOSTIC_CHARS,
) -> str:
    """Render one plugin/catalog diagnostic without leaking URL secrets or controls."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    rendered = redact_urls_in_text(str(value)).strip() or type(value).__name__
    rendered = terminal_safe_text(rendered, single_line=True)
    if len(rendered) > max_chars:
        return rendered[: max_chars - 3] + "..."
    return rendered



def render_extension_inventory(
    inventory: ExtensionInventory,
    *,
    kind: ExtensionKind = "all",
    json_output: bool = False,
) -> str:
    payload = inventory.as_dict()
    if kind != "all":
        payload = {
            "workspace": payload["workspace"],
            "project_trusted": payload["project_trusted"],
            kind: payload[kind],
            "errors": payload["errors"],
        }
    if json_output:
        return json.dumps(payload, sort_keys=True)

    lines = [
        f"Workspace: {inventory.workspace}",
        f"Project extensions: {'trusted' if inventory.project_trusted else 'untrusted'}",
    ]
    if kind in {"all", "skills"}:
        lines.append("Skills:")
        lines.extend(
            f"  {skill.name}: {skill.description} ({skill.path})"
            for skill in inventory.skills
        )
        if not inventory.skills:
            lines.append("  (none)")
    if kind in {"all", "agents"}:
        lines.append("Agents:")
        lines.extend(
            f"  {agent.name} [{agent.base_role}]: {agent.description} ({agent.path})"
            for agent in inventory.agents
        )
        if not inventory.agents:
            lines.append("  (none)")
    if kind in {"all", "plugins"}:
        lines.append("Plugins:")
        lines.extend(
            f"  {plugin.name} {plugin.version} [{plugin.source}; "
            f"{'enabled' if plugin.enabled else 'disabled'}] - "
            f"{plugin.description or '(no description)'}"
            + (
                f" (runtime v{plugin.runtime_protocol}: {', '.join(plugin.tools)})"
                if plugin.runtime_protocol is not None
                else ""
            )
            for plugin in inventory.plugins
        )
        if not inventory.plugins:
            lines.append("  (none)")
    if kind in {"all", "hooks"}:
        lines.append("Hooks:")
        for hook in inventory.hooks:
            counts = {
                name: getattr(hook, name)
                for name in (
                    "pre_tool",
                    "post_tool",
                    "session_start",
                    "session_end",
                    "turn_start",
                    "turn_end",
                    "turn_error",
                    "pre_model",
                    "post_model",
                    "tool_error",
                )
            }
            active = ", ".join(
                f"{name}={count}" for name, count in counts.items() if count
            )
            lines.append(f"  {hook.path} [{hook.source}]: {active or 'empty'}")
        if not inventory.hooks:
            lines.append("  (none)")
    if inventory.errors:
        lines.append("Errors:")
        lines.extend(f"  {error}" for error in inventory.errors)
    return "\n".join(lines)


def _verified_catalog(
    catalog: CatalogSource | None = None,
    *,
    transport: Any | None = None,
):
    catalog_file: Path | None
    remotely_fetched = False
    if isinstance(catalog, str):
        remotely_fetched = "://" in catalog
        try:
            catalog_file = (
                fetch_catalog(catalog, transport=transport)
                if remotely_fetched
                else Path(catalog).expanduser()
            )
        except ValueError as exc:
            raise PluginLifecycleError(str(exc)) from exc
    else:
        catalog_file = catalog
    if catalog_file is None:
        configured = default_catalog_source()
        if configured is None:
            raise PluginLifecycleError(
                "plugin catalog is not configured; pass --catalog or set "
                "ASH_PLUGIN_CATALOG"
            )
        remotely_fetched = isinstance(configured, str)
        try:
            catalog_file = (
                fetch_catalog(configured)
                if isinstance(configured, str)
                else configured
            )
        except ValueError as exc:
            raise PluginLifecycleError(str(exc)) from exc
    try:
        verified = parse_and_verify_catalog(
            catalog_file,
            trusted_keys_path=trusted_catalog_keys_path(),
        )
    except ValueError as exc:
        raise PluginLifecycleError(str(exc)) from exc
    if remotely_fetched and any(
        entry.source.casefold().startswith("file://")
        for entry in verified.entries.values()
    ):
        raise PluginLifecycleError(
            "remotely fetched plugin catalogs may only reference HTTPS Git sources"
        )
    return verified


def _verified_catalogs(
    catalog: CatalogSelection = None,
    *,
    transport: Any | None = None,
) -> tuple[SignedCatalog, ...]:
    expected_publishers: tuple[str, ...] | None = None
    expected_key_ids: tuple[str | None, ...] | None = None
    expected_key_fingerprints: tuple[str | None, ...] | None = None
    registered_sequences: dict[str, tuple[str, int]] = {}
    sources: tuple[CatalogSource | None, ...]
    if isinstance(catalog, Mapping):
        expected_publishers = tuple(catalog.keys())
        bound_sources: list[CatalogSource | None] = []
        bound_key_ids: list[str | None] = []
        bound_key_fingerprints: list[str | None] = []
        for value in catalog.values():
            if isinstance(value, RegisteredCatalogSource):
                bound_sources.append(value.source)
                bound_key_ids.append(value.key_id)
                bound_key_fingerprints.append(value.key_fingerprint)
            else:
                bound_sources.append(value)
                bound_key_ids.append(None)
                bound_key_fingerprints.append(None)
        sources = tuple(bound_sources)
        expected_key_ids = tuple(bound_key_ids)
        expected_key_fingerprints = tuple(bound_key_fingerprints)
        if not sources:
            sources = (None,)
            expected_publishers = None
            expected_key_ids = None
            expected_key_fingerprints = None
    elif isinstance(catalog, Path | str) or catalog is None:
        sources = (catalog,)
    else:
        sources = tuple(catalog)
        if not sources:
            sources = (None,)
    verified = tuple(
        _verified_catalog(source, transport=transport) for source in sources
    )
    if expected_publishers is not None:
        assert expected_key_ids is not None
        assert expected_key_fingerprints is not None
        for expected, expected_key_id, expected_key_fingerprint, item in zip(
            expected_publishers,
            expected_key_ids,
            expected_key_fingerprints,
            verified,
            strict=True,
        ):
            if item.publisher != expected:
                actual = item.publisher or "legacy-v1"
                raise PluginLifecycleError(
                    f"registered marketplace @{expected} returned publisher {actual!r}"
                )
            if expected_key_id is not None and item.key_id != expected_key_id:
                raise PluginLifecycleError(
                    f"registered marketplace @{expected} signing key changed from "
                    f"{expected_key_id!r} to {item.key_id!r}"
                )
            if (
                expected_key_fingerprint is not None
                and item.key_fingerprint != expected_key_fingerprint
            ):
                raise PluginLifecycleError(
                    f"registered marketplace @{expected} signing key material changed"
                )
            if expected_key_fingerprint is not None:
                registered_sequences[expected] = (
                    expected_key_fingerprint,
                    item.sequence,
                )
    if len(verified) > 1:
        publishers = [item.publisher for item in verified]
        if any(publisher is None for publisher in publishers):
            raise PluginLifecycleError(
                "multiple plugin catalogs require version 2 signed publisher identity"
            )
        publisher_names = [publisher for publisher in publishers if publisher is not None]
        if len(set(publisher_names)) != len(publisher_names):
            raise PluginLifecycleError("duplicate plugin catalog publisher in selection")
    if registered_sequences:
        from ash.commands.marketplace import accept_registered_marketplace_sequences

        accept_registered_marketplace_sequences(
            registered_sequences,
            legacy_fingerprints={
                publisher: fingerprint
                for publisher, (fingerprint, _sequence) in registered_sequences.items()
            },
        )
    return verified


def search_catalog_plugins(
    query: str = "",
    *,
    catalog: CatalogSelection = None,
    transport: Any | None = None,
) -> tuple[tuple[SignedCatalog, ...], tuple[CatalogEntry, ...]]:
    verified = _verified_catalogs(catalog, transport=transport)
    normalized = query.strip().casefold()
    entries = sorted(
        (entry for item in verified for entry in item.entries.values()),
        key=lambda entry: ((entry.publisher or "").casefold(), entry.name.casefold()),
    )
    if normalized:
        entries = [
            entry
            for entry in entries
            if normalized
            in " ".join(
                (
                    entry.publisher or "",
                    f"@{entry.publisher}/{entry.name}" if entry.publisher else entry.name,
                    entry.name,
                    entry.version,
                    entry.source,
                    entry.ref,
                )
            ).casefold()
        ]
    return verified, tuple(entries)


def render_catalog_search(
    catalogs: tuple[SignedCatalog, ...],
    entries: tuple[CatalogEntry, ...],
    *,
    json_output: bool = False,
) -> str:
    plugin_payload = [
        {
            **({"publisher": entry.publisher} if entry.publisher is not None else {}),
            "name": entry.name,
            "version": entry.version,
            "source": entry.source,
            "ref": entry.ref,
            "digest": entry.digest,
        }
        for entry in entries
    ]
    if len(catalogs) == 1:
        catalog = catalogs[0]
        payload = {
            "sequence": catalog.sequence,
            **({"publisher": catalog.publisher} if catalog.publisher is not None else {}),
            "plugins": plugin_payload,
        }
    else:
        payload = {
            "catalogs": [
                {"publisher": item.publisher, "sequence": item.sequence}
                for item in catalogs
            ],
            "plugins": plugin_payload,
        }
    if json_output:
        return json.dumps(payload, sort_keys=True)
    if len(catalogs) == 1:
        catalog = catalogs[0]
        heading = (
            f"Catalog @{catalog.publisher} sequence: {catalog.sequence}"
            if catalog.publisher is not None
            else f"Catalog sequence: {catalog.sequence}"
        )
        lines = [heading, "Plugins:"]
    else:
        lines = ["Catalogs:"]
        lines.extend(
            f"  @{item.publisher} sequence {item.sequence}" for item in catalogs
        )
        lines.append("Plugins:")
    for entry in entries:
        publisher = (
            terminal_safe_text(entry.publisher, single_line=True)
            if entry.publisher is not None
            else None
        )
        name = terminal_safe_text(entry.name, single_line=True)
        version = terminal_safe_text(entry.version, single_line=True)
        source = terminal_safe_text(entry.source, single_line=True)
        ref = terminal_safe_text(entry.ref, single_line=True)
        lines.append(
            f"  @{publisher}/{name} {version} [{source}@{ref}]"
            if publisher is not None
            else f"  {name} {version} [{source}@{ref}]"
        )
    if not entries:
        lines.append("  (none)")
    return "\n".join(lines)


def catalog_entry_for_name(
    name: str,
    *,
    catalog: CatalogSelection = None,
) -> CatalogEntry:
    catalogs = _verified_catalogs(catalog)
    requested_publisher: str | None = None
    requested_name = name
    if name.startswith("@"):
        qualified = name[1:].split("/", 1)
        if len(qualified) != 2 or not all(qualified) or "/" in qualified[1]:
            raise PluginLifecycleError(
                "publisher-qualified plugin must use @publisher/name"
            )
        requested_publisher, requested_name = qualified
    matches = [
        entry
        for item in catalogs
        for entry in item.entries.values()
        if entry.name.casefold() == requested_name.casefold()
        and (
            requested_publisher is None
            or (entry.publisher or "").casefold() == requested_publisher.casefold()
        )
    ]
    if not matches:
        raise PluginLifecycleError(f"unknown catalog plugin: {name}")
    if len(matches) != 1:
        choices = ", ".join(
            f"@{entry.publisher}/{entry.name}"
            for entry in matches
            if entry.publisher is not None
        )
        suffix = f"; choose one of: {choices}" if choices else ""
        raise PluginLifecycleError(f"ambiguous catalog plugin: {name}{suffix}")
    if not matches[0].source.lower().startswith(("https://", "file://")):
        raise PluginLifecycleError(
            f"catalog plugin {name!r} does not use an HTTPS or local file Git source"
        )
    return matches[0]


def manage_local_plugin(
    action: PluginAction,
    target: str,
    *,
    replace: bool = False,
    confirmed: bool = False,
    git_ref: str | None = None,
    catalog: CatalogSelection = None,
) -> dict[str, Any]:
    if action == "install":
        def validate_install(root: Path, manifest: PluginManifest) -> None:
            validate_plugin_contents(root, manifest)

        def validate_install_at(
            snapshot: PluginSnapshot,
            manifest: PluginManifest,
        ) -> None:
            validate_plugin_contents_at(snapshot, manifest)

        def validate_install_topology(
            manifest: PluginManifest,
            manifests: Mapping[str, PluginManifest],
        ) -> None:
            state = load_extension_state()
            _require_enabled_dependencies_from_manifests(
                manifest,
                state.disabled_plugins,
                manifests,
            )

        if target.startswith(("https://", "http://")):
            expected = None
            if git_ref and (catalog is not None or default_catalog_source() is not None):
                verified_catalogs = _verified_catalogs(catalog)
                matches = [
                    entry
                    for verified_catalog in verified_catalogs
                    for entry in verified_catalog.entries.values()
                    if entry.source == target and entry.ref == git_ref
                ]
                if len(matches) != 1:
                    raise PluginLifecycleError(
                        "signed plugin catalog does not contain exactly one entry for "
                        f"{target}@{git_ref}"
                    )
                expected = matches[0]
            installed = install_git_plugin(
                target,
                ref=git_ref or "",
                replace=replace,
                enabled=True,
                validator=validate_install,
                _validator_at=validate_install_at,
                _topology_validator=validate_install_topology,
                expected=expected,
            )
        elif git_ref is not None:
            raise PluginLifecycleError("--ref cannot override a catalog-pinned ref")
        elif Path(target).is_dir() or target.startswith((".", "/", "~")):
            installed = install_local_plugin(
                Path(target).expanduser(),
                replace=replace,
                enabled=True,
                validator=validate_install,
                _validator_at=validate_install_at,
                _topology_validator=validate_install_topology,
            )
        else:
            expected = catalog_entry_for_name(target, catalog=catalog)
            installed = install_git_plugin(
                expected.source,
                ref=expected.ref,
                replace=replace,
                enabled=True,
                validator=validate_install,
                _validator_at=validate_install_at,
                _topology_validator=validate_install_topology,
                expected=expected,
            )
        return {
            "action": action,
            "name": installed.name,
            "version": installed.version,
            "root": str(installed.root),
            "enabled": True,
        }

    if action == "enable":
        plugin = set_local_plugin_enabled(target, enabled=True)
        enabled = True
    elif action == "disable":
        plugin = set_local_plugin_enabled(target, enabled=False)
        enabled = False
    else:
        removed_version = ""
        removed_root = user_plugin_root() / target

        def validate_uninstall(
            manifest: PluginManifest,
            manifests: Mapping[str, PluginManifest],
        ) -> None:
            nonlocal removed_version
            removed_version = manifest.version
            dependents = sorted(
                candidate
                for candidate, installed in manifests.items()
                if candidate != target
                and any(
                    dependency.get("name") == target
                    for dependency in installed.dependencies
                )
            )
            if dependents:
                raise PluginLifecycleError(
                    f"cannot uninstall {target!r}; required by: {', '.join(dependents)}"
                )

        uninstall_local_plugin(
            target,
            confirmed=confirmed,
            _preflight=validate_uninstall,
        )
        return {
            "action": action,
            "name": target,
            "version": removed_version,
            "root": str(removed_root),
            "removed": True,
        }
    return {
        "action": action,
        "name": target,
        "version": plugin.version,
        "root": str(plugin.root),
        "enabled": enabled,
    }


def update_local_plugin(
    target: str,
    *,
    catalog: CatalogSelection = None,
) -> dict[str, Any]:
    """Update one managed Git/catalog plugin from its persisted provenance."""

    def validate_update(root: Path, manifest: PluginManifest) -> None:
        if manifest.name != target:
            raise PluginLifecycleError(
                f"updated plugin manifest {manifest.name!r} does not match "
                f"tracked plugin {target!r}"
            )
        validate_plugin_contents(root, manifest)

    def validate_update_at(
        snapshot: PluginSnapshot,
        manifest: PluginManifest,
    ) -> None:
        if manifest.name != target:
            raise PluginLifecycleError(
                f"updated plugin manifest {manifest.name!r} does not match "
                f"tracked plugin {target!r}"
        )
        validate_plugin_contents_at(snapshot, manifest)

    plugin, record = load_managed_plugin_for_update(target)

    def validate_publication_state(
        manifest: PluginManifest,
        state: ExtensionState,
        manifests: Mapping[str, PluginManifest],
    ) -> None:
        candidate_graph = dict(manifests)
        candidate_graph[target] = manifest
        _require_enabled_plugin_graph(
            candidate_graph,
            state.disabled_plugins,
        )

    if record.origin == "legacy-unknown":
        raise PluginLifecycleError(
            f"plugin {target!r} uses legacy install provenance whose original trust "
            "source cannot be determined; reinstall it before updating"
        )

    if record.origin == "catalog":
        catalog_name = (
            f"@{record.publisher}/{target}"
            if record.publisher is not None
            else target
        )
        expected = catalog_entry_for_name(
            catalog_name,
            catalog=catalog,
        )
        unchanged = (
            expected.name == record.name
            and expected.version == record.version
            and expected.source == record.source
            and expected.ref == record.ref
            and expected.digest == record.digest
            and expected.publisher == record.publisher
        )
        if unchanged:
            installed_result = install_git_plugin(
                expected.source,
                ref=expected.ref,
                replace=True,
                validator=validate_update,
                _validator_at=validate_update_at,
                expected=expected,
                _skip_if_digest=record.digest,
                _unchanged=InstalledPlugin(
                    plugin.name,
                    plugin.version,
                    plugin.root,
                ),
                _expected_previous_record=record,
                _state_validator=validate_publication_state,
                _skip_dependency_checks=True,
            )
        else:
            installed_result = install_git_plugin(
                expected.source,
                ref=expected.ref,
                replace=True,
                validator=validate_update,
                _validator_at=validate_update_at,
                expected=expected,
                _expected_previous_record=record,
                _state_validator=validate_publication_state,
                _skip_dependency_checks=True,
            )
    else:
        installed_result = install_git_plugin(
            record.source,
            ref=record.ref,
            replace=True,
            validator=validate_update,
            _validator_at=validate_update_at,
            _skip_if_digest=record.digest,
            _unchanged=InstalledPlugin(
                plugin.name,
                plugin.version,
                plugin.root,
            ),
            _expected_previous_record=record,
            _state_validator=validate_publication_state,
            _skip_dependency_checks=True,
        )

    after = installed_result.install_record or record
    status: Literal["updated", "unchanged"] = (
        "unchanged" if after == record else "updated"
    )
    return _plugin_update_result(
        target,
        plugin.root,
        before=record,
        after=after,
        status=status,
    )


def update_all_local_plugins(
    *,
    catalog: CatalogSelection = None,
) -> dict[str, Any]:
    """Update tracked plugins, coordinating dependency migrations when needed."""

    recover_plugin_lifecycle()
    names = sorted(load_plugin_install_records())
    result_by_name: dict[str, dict[str, Any]] = {}
    dependency_failures: set[str] = set()
    dependency_relations: dict[str, frozenset[str]] = {}
    coordination_errors: list[str] = []

    pending = load_extension_state().pending_update
    if pending is not None:
        resumed, resolved, error = _run_coordinated_update(
            pending,
            catalog=catalog,
        )
        result_by_name.update(resumed)
        if error is not None:
            coordination_errors.append(error)
        if not resolved:
            return _plugin_update_all_result(
                names,
                result_by_name,
                coordination_errors=coordination_errors,
            )

    for name in names:
        if name in result_by_name:
            continue
        try:
            result_by_name[name] = update_local_plugin(name, catalog=catalog)
        except PluginDependencyError as exc:
            dependency_failures.add(name)
            dependency_relations[name] = exc.plugins
            result_by_name[name] = _plugin_update_error(name, exc)
        except (OSError, PluginLifecycleError, ValueError) as exc:
            result_by_name[name] = _plugin_update_error(name, exc)

    # A dependency failure can be only an ordering issue: another independent
    # update later in the pass may have made the graph valid. Retry once before
    # escalating to a coordinated quiesce.
    for name in sorted(dependency_failures):
        try:
            result_by_name[name] = update_local_plugin(name, catalog=catalog)
        except PluginDependencyError as exc:
            dependency_relations[name] = exc.plugins
            result_by_name[name] = _plugin_update_error(name, exc)
        except (OSError, PluginLifecycleError, ValueError) as exc:
            dependency_failures.discard(name)
            dependency_relations.pop(name, None)
            result_by_name[name] = _plugin_update_error(name, exc)
        else:
            dependency_failures.discard(name)
            dependency_relations.pop(name, None)

    if dependency_failures:
        manifests = load_installed_plugin_manifests()
        for component, targets in _dependency_failure_components(
            manifests,
            dependency_failures,
            dependency_relations,
        ):
            if len(targets) < 2:
                continue
            try:
                _, quiesced = begin_coordinated_plugin_update(
                    list(component),
                    list(targets),
                )
                assert quiesced.pending_update is not None
                outcomes, resolved, error = _run_coordinated_update(
                    quiesced.pending_update,
                    catalog=catalog,
                )
                result_by_name.update(outcomes)
                if error is not None:
                    coordination_errors.append(error)
                if resolved:
                    dependency_failures.difference_update(targets)
            except (OSError, PluginLifecycleError, ValueError) as exc:
                coordination_errors.append(redact_text(str(exc)))

    return _plugin_update_all_result(
        names,
        result_by_name,
        coordination_errors=coordination_errors,
    )


def _plugin_update_error(name: str, exc: BaseException) -> dict[str, Any]:
    return {
        "action": "update",
        "name": name,
        "status": "error",
        "error": redact_text(str(exc)),
    }


def _plugin_update_all_result(
    names: Sequence[str],
    result_by_name: Mapping[str, dict[str, Any]],
    *,
    coordination_errors: Sequence[str] = (),
) -> dict[str, Any]:
    results = [
        result_by_name.get(
            name,
            _plugin_update_error(
                name,
                PluginLifecycleError("plugin update did not produce an outcome"),
            ),
        )
        for name in names
    ]
    updated = sum(result.get("status") == "updated" for result in results)
    unchanged = sum(result.get("status") == "unchanged" for result in results)
    errors = sum(result.get("status") == "error" for result in results)
    errors += len(coordination_errors)
    payload: dict[str, Any] = {
        "action": "update-all",
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
        "results": results,
    }
    if coordination_errors:
        payload["coordination_errors"] = [
            redact_text(error) for error in coordination_errors
        ]
    pending = load_extension_state().pending_update
    if pending is not None:
        payload["quiesced"] = list(pending.plugins)
    return payload


def _run_coordinated_update(
    pending: PendingPluginUpdate,
    *,
    catalog: CatalogSelection,
) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
    outcomes: dict[str, dict[str, Any]] = {}
    for name in pending.targets:
        try:
            outcomes[name] = update_local_plugin(name, catalog=catalog)
        except (OSError, PluginLifecycleError, ValueError) as exc:
            outcomes[name] = _plugin_update_error(name, exc)

    current = load_extension_state()
    if current.pending_update != pending:
        return (
            outcomes,
            False,
            "coordinated plugin update state changed before graph validation",
        )
    restore_disabled = frozenset(
        set(current.disabled_plugins) - set(pending.restore_enabled)
    )
    try:
        _require_enabled_plugin_graph(
            load_installed_plugin_manifests(),
            restore_disabled,
        )
    except PluginDependencyError as exc:
        return outcomes, False, redact_text(str(exc))
    try:
        finish_coordinated_plugin_update(pending)
    except PluginLifecycleError as exc:
        return outcomes, False, redact_text(str(exc))
    return outcomes, True, None


def _dependency_failure_components(
    manifests: Mapping[str, PluginManifest],
    failed_targets: set[str],
    failure_relations: Mapping[str, frozenset[str]],
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    adjacency: dict[str, set[str]] = {name: set() for name in manifests}
    for name, manifest in manifests.items():
        for dependency in manifest.dependencies:
            dependency_name = dependency.get("name", "")
            if dependency_name not in adjacency:
                continue
            adjacency[name].add(dependency_name)
            adjacency[dependency_name].add(name)
    for failed_name in failed_targets:
        related = {
            name
            for name in failure_relations.get(failed_name, frozenset())
            if name in adjacency
        }
        related.add(failed_name)
        for name in related:
            adjacency[name].update(related - {name})

    groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    unseen = set(adjacency)
    while unseen:
        start = min(unseen)
        stack = [start]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            unseen.discard(current)
            stack.extend(adjacency[current] - component)
        targets = tuple(sorted(component & failed_targets))
        if targets:
            groups.append((tuple(sorted(component)), targets))
    return groups


def _plugin_update_result(
    name: str,
    root: Path,
    *,
    before: PluginInstallRecord,
    after: PluginInstallRecord,
    status: Literal["updated", "unchanged"],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": "update",
        "name": name,
        "status": status,
        "previous_version": before.version,
        "version": after.version,
        "previous_digest": before.digest,
        "digest": after.digest,
        "root": str(root),
    }
    if after.publisher is not None:
        result["publisher"] = after.publisher
    return result


def render_plugin_action(result: dict[str, Any], *, json_output: bool) -> str:
    if json_output:
        return json.dumps(result, sort_keys=True)
    action = terminal_safe_text(str(result["action"]), single_line=True)
    name = terminal_safe_text(str(result["name"]), single_line=True)
    version = terminal_safe_text(str(result.get("version", "")), single_line=True)
    root = terminal_safe_text(str(result.get("root", "")), single_line=True)
    if action == "install":
        return f"Installed and enabled {name} {version} at {root}"
    if action == "update":
        if result["status"] == "unchanged":
            return f"{name} {version} is already up to date"
        previous_version = terminal_safe_text(
            str(result["previous_version"]), single_line=True
        )
        if previous_version == version:
            return f"Updated {name} {version} at {root}"
        return (
            f"Updated {name} {previous_version} -> {version} "
            f"at {root}"
        )
    if action == "uninstall":
        return f"Uninstalled {name} from {root}"
    return f"{action.capitalize()}d {name}"


def render_plugin_update_all(result: dict[str, Any], *, json_output: bool) -> str:
    if json_output:
        return json.dumps(result, sort_keys=True)
    lines: list[str] = []
    for outcome in result["results"]:
        if outcome.get("status") == "error":
            lines.append(
                "Failed to update "
                f"{outcome['name']}: "
                f"{terminal_safe_text(str(outcome['error']), single_line=True)}"
            )
        else:
            lines.append(render_plugin_action(outcome, json_output=False))
    if not lines:
        lines.append("No tracked plugins to update.")
    for error in result.get("coordination_errors", []):
        lines.append(
            "Coordinated update incomplete: "
            + terminal_safe_text(str(error), single_line=True)
        )
    quiesced = result.get("quiesced", [])
    if quiesced:
        lines.append(
            "Temporarily disabled pending recovery: "
            + ", ".join(
                terminal_safe_text(str(name), single_line=True)
                for name in quiesced
            )
            + ". Rerun ash extensions update --all to resume safely."
        )
    lines.append(
        "Update summary: "
        f"{result['updated']} updated, "
        f"{result['unchanged']} unchanged, "
        f"{result['errors']} failed."
    )
    return "\n".join(lines)


def _require_enabled_dependencies_from_manifests(
    manifest: PluginManifest,
    disabled_plugins: frozenset[str],
    manifests: Mapping[str, PluginManifest],
) -> None:
    versions = {
        name: installed.version
        for name, installed in manifests.items()
        if name not in disabled_plugins and name != manifest.name
    }
    errors = manifest.check_dependencies(versions)
    if errors:
        raise PluginDependencyError("; ".join(errors))


def _require_enabled_plugin_graph(
    manifests: Mapping[str, PluginManifest],
    disabled_plugins: frozenset[str],
) -> None:
    enabled_versions = {
        name: manifest.version
        for name, manifest in manifests.items()
        if name not in disabled_plugins
    }
    for name in sorted(enabled_versions):
        manifest = manifests[name]
        for dependency in manifest.dependencies:
            dependency_name = dependency.get("name", "")
            requirement = dependency.get("version", "")
            installed_version = enabled_versions.get(dependency_name)
            if installed_version is None:
                raise PluginDependencyError(
                    f"{name}: Missing dependency: {dependency_name} "
                    f"({requirement})",
                    plugins=(name, dependency_name),
                )
            if not requirement:
                continue
            if SpecifierSet(requirement).contains(parse_version(installed_version)):
                continue
            raise PluginDependencyError(
                f"{name} requires {dependency_name} {requirement}; "
                f"installed {installed_version} does not satisfy it",
                plugins=(name, dependency_name),
            )
