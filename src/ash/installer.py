"""Dependency-free public installer for Ash.

The module is intentionally usable both from an installed package and as a
downloaded script.  It owns package-manager detection, repair flags, and final
executable verification so users do not need to understand pipx/uv internals.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"
_RELEASES_API = "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases"
_GITHUB_API_VERSION = "2026-03-10"
SUPPORTED_EXTRAS = (
    "a2a",
    "acp",
    "aws",
    "azure",
    "browser",
    "gcp",
    "local-embeddings",
    "observability",
    "server",
)
_SUPPORTED_EXTRA_SET = frozenset(SUPPORTED_EXTRAS)
_PACKAGE_NAME = "ash-ai"
_EXTRAS_PATTERN = re.compile(
    rf"^\s*{re.escape(_PACKAGE_NAME)}(?:\[([^]]+)\])?(?:\s*@.*)?\s*$",
    re.IGNORECASE,
)
_UV_EXTRAS_PATTERN = re.compile(r"\[extras:\s*([^]]+)\]", re.IGNORECASE)
_UV_ASH_PATH_PATTERN = re.compile(r"^-\s+ash\s+\(([^)]+)\)\s*$", re.MULTILINE)
_UV_PYTHON_PATTERN = re.compile(
    r"\[[A-Za-z][A-Za-z0-9_.+-]*\s+(\d+\.\d+(?:\.\d+)?)\]"
)
_PYTHON_VERSION_PATTERN = re.compile(
    r"^(?:Python\s+)?(\d+)\.(\d+)(?:\.\d+)?$",
    re.IGNORECASE,
)
_RELEASE_REF_PATTERN = re.compile(r"^ash-v\d+(?:\.\d+)+(?:[0-9A-Za-z._+-]*)$")
_RELEASE_WHEEL_PATTERN = re.compile(
    r"^ash_ai-[0-9A-Za-z_.!+\-]+-py3-none-any\.whl$"
)
_INSTALL_TIMEOUT_SECONDS = 15 * 60
_QUERY_TIMEOUT_SECONDS = 30
_VERIFY_TIMEOUT_SECONDS = 30
_MAX_STATE_OUTPUT_BYTES = 1024 * 1024
_MAX_QUERY_OUTPUT_BYTES = 16 * 1024
_MAX_VERSION_OUTPUT_BYTES = 16 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_RELEASE_METADATA_BYTES = 1024 * 1024
_MAX_RELEASE_WHEEL_BYTES = 100 * 1024 * 1024
_CAPTURE_CHUNK_BYTES = 8192
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_CAPTURE_QUEUE_SIZE = 8
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_KILLPG = getattr(os, "killpg", None)
_SIGKILL = getattr(signal, "SIGKILL", None)
_BOOTSTRAP_MIN_PYTHON = (3, 10)
_RUNTIME_MIN_PYTHON = (3, 12)
_RUNTIME_MAX_PYTHON = (3, 15)
_RUNTIME_FALLBACK_PYTHON = "3.14"
_SUPPORTED_NATIVE_PLATFORMS = frozenset({"linux", "darwin"})
_INSTALLER_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "PIPX_HOME",
        "PIPX_BIN_DIR",
        "PIPX_MAN_DIR",
        "PIPX_SHARED_LIBS",
        "UV_CACHE_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
    }
)


def _strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Parse standalone-installer JSON without duplicate keys/non-finite values."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    def reject_constant(raw: str) -> None:
        raise ValueError(f"invalid JSON constant: {raw}")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


class InstallError(RuntimeError):
    """A concise, user-actionable installation failure."""


def _attach_cleanup_failure(
    primary: BaseException,
    cleanup_error: BaseException,
) -> None:
    """Preserve cleanup diagnostics on every supported bootstrap Python."""

    note = f"Process-tree cleanup failed: {cleanup_error}"
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(note)
    elif primary.__cause__ is None:
        # ``BaseException.add_note`` was added in Python 3.11, while the
        # standalone installer intentionally bootstraps on Python 3.10.
        primary.__cause__ = cleanup_error


def _installer_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Keep package-manager essentials without forwarding unrelated secrets."""

    environment = {
        key: value
        for key, value in source.items()
        if key in _INSTALLER_ENV_KEYS or key.startswith("LC_")
    }
    if "PATH" in source:
        environment["PATH"] = _sanitize_install_path(
            source["PATH"],
            home=source.get("HOME"),
        )
    else:
        environment["PATH"] = os.defpath
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _sanitize_install_path(path_value: str, *, home: str | None) -> str:
    """Remove relative and workspace-controlled entries from child PATH."""

    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    resolved_home: Path | None = None
    if home:
        try:
            resolved_home = Path(home).expanduser().resolve()
        except OSError:
            resolved_home = None

    safe_entries: list[str] = []
    seen: set[str] = set()
    for raw_entry in path_value.split(os.pathsep):
        entry = raw_entry.strip('"')
        if not entry:
            continue
        directory = Path(entry).expanduser()
        if not directory.is_absolute():
            continue
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if cwd is not None and _path_is_workspace_controlled(
            directory,
            cwd=cwd,
            home=resolved_home,
        ):
            continue
        rendered = str(directory)
        if rendered in seen:
            continue
        seen.add(rendered)
        safe_entries.append(rendered)
    return os.pathsep.join(safe_entries)


def _resolve_install_manager(
    command: str,
    *,
    environment: Mapping[str, str],
) -> str | None:
    """Resolve pipx/uv without executing a workspace-controlled PATH entry."""

    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    home: Path | None = None
    raw_home = environment.get("HOME")
    if raw_home:
        try:
            home = Path(raw_home).expanduser().resolve()
        except OSError:
            home = None

    for raw_entry in environment.get("PATH", os.defpath).split(os.pathsep):
        entry = raw_entry.strip('"')
        if not entry:
            continue
        directory = Path(entry).expanduser()
        if not directory.is_absolute():
            continue
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if cwd is not None and _path_is_workspace_controlled(
            directory,
            cwd=cwd,
            home=home,
        ):
            continue
        resolved = shutil.which(command, path=str(directory))
        if resolved is None:
            continue
        try:
            candidate = Path(resolved).resolve()
        except OSError:
            continue
        if cwd is not None and _path_is_workspace_controlled(
            candidate,
            cwd=cwd,
            home=home,
        ):
            continue
        return str(candidate)
    return None


def _path_is_workspace_controlled(path: Path, *, cwd: Path, home: Path | None) -> bool:
    if path == cwd:
        return True
    if cwd.parent == cwd or (home is not None and cwd == home):
        return False
    return path.is_relative_to(cwd)


def _platform_support_error(platform: str) -> str | None:
    if platform in _SUPPORTED_NATIVE_PLATFORMS:
        return None
    if platform == "win32":
        return (
            "Native Windows is not currently supported by Ash. "
            "Install and run Ash inside WSL2 instead."
        )
    return (
        f"Ash does not currently support this native platform ({platform}). "
        "Supported native platforms are Linux and macOS."
    )


@dataclass(frozen=True)
class _InstallerProcessTreePlan:
    """Dependency-free preflight for installer-managed subprocesses."""

    spawn_options: dict[str, Any]


def _prepare_process_tree() -> _InstallerProcessTreePlan:
    return _InstallerProcessTreePlan(
        spawn_options={"start_new_session": True},
    )


@dataclass(frozen=True)
class InstallResult:
    manager: str
    executable: str
    version: str
    shell_restart_required: bool = False


@dataclass(frozen=True)
class _PipxState:
    installed: bool = False
    extras: tuple[str, ...] = ()
    executable: str | None = None
    runtime_python: str | None = None


@dataclass(frozen=True)
class _PipxInspection:
    state: _PipxState | None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.state is not None


@dataclass(frozen=True)
class _UvState:
    installed: bool = False
    extras: tuple[str, ...] = ()
    executable: str | None = None
    runtime_python: str | None = None


@dataclass(frozen=True)
class _CapturedResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class _ReleaseWheel:
    name: str
    download_url: str
    sha256: str
    size: int


def install(
    *,
    extras: Sequence[str] = (),
    ref: str | None = None,
    runtime_python: str | None = None,
    host_platform: str = sys.platform,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    release_opener: Callable[..., Any] = urllib.request.urlopen,
) -> InstallResult:
    """Install or repair Ash, preserving existing pipx capability extras."""

    unsupported_platform = _platform_support_error(host_platform)
    if unsupported_platform is not None:
        raise InstallError(unsupported_platform)

    requested_extras = _normalize_requested_extras(extras)
    environment = _installer_environment(os.environ if environ is None else environ)
    resolver = which or (lambda name: _resolve_install_manager(name, environment=environment))
    if runtime_python is None:
        runtime_python = _runtime_python_override(sys.version_info[:2])
    pipx = resolver("pipx")
    uv = resolver("uv")
    existing_ash = resolver("ash")
    if pipx is None:
        if uv is None:
            raise InstallError("Neither pipx nor uv is installed.")
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        if not previous_uv.installed and existing_ash is not None:
            raise InstallError(
                "An existing Ash executable was found at "
                f"{existing_ash}, but uv does not report owning it. Remove or "
                "migrate that installation before creating a new managed install."
            )
        return _install_with_uv(
            uv,
            extras=requested_extras,
            ref=ref,
            runtime_python=runtime_python,
            runner=runner,
            environment=environment,
            previous=previous_uv,
            release_opener=release_opener,
        )

    try:
        inspection = _read_pipx_state(
            pipx,
            runner=runner,
            environment=environment,
        )
    except OSError as exc:
        raise InstallError(
            "pipx was found but could not start; Ash cannot safely determine "
            f"package-manager ownership ({exc}). Repair or remove pipx before "
            "running the Ash installer again."
        ) from exc
    if inspection.succeeded:
        previous = inspection.state
        assert previous is not None
        if previous.installed and uv is not None:
            previous_uv = _read_uv_state(
                uv,
                runner=runner,
                environment=environment,
            )
            if previous_uv.installed:
                raise InstallError(
                    "Ash is installed by both pipx and uv. Remove one installation "
                    "before running the Ash installer again."
                )
    else:
        pipx_home = _pipx_home_directory(
            pipx,
            runner=runner,
            environment=environment,
        )
        corrupt_metadata = _corrupt_ash_metadata(pipx_home)
        if corrupt_metadata is None:
            detail = inspection.error or "unknown pipx inspection failure"
            raise InstallError(
                f"Could not inspect the existing pipx installation: {detail}"
            )
        # POSIX offers no portable way to unlink/rename the exact object held
        # by an open fd. A check-then-unlink sequence can delete a same-user
        # replacement, so corrupt metadata is intentionally a manual boundary.
        raise InstallError(
            "Corrupt pipx metadata for Ash was detected at "
            f"{corrupt_metadata}. Ash will not move or delete this file "
            "automatically because the pathname may change concurrently. "
            "After confirming no pipx process is using it, move the file aside "
            "manually and rerun the Ash installer."
        )
    if not previous.installed and uv is not None:
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        if previous_uv.installed:
            return _install_with_uv(
                uv,
                extras=requested_extras,
                ref=ref,
                runtime_python=runtime_python,
                runner=runner,
                environment=environment,
                previous=previous_uv,
                release_opener=release_opener,
            )
    if not previous.installed and existing_ash is not None:
        raise InstallError(
            "An existing Ash executable was found at "
            f"{existing_ash}, but neither pipx nor uv reports owning it. Remove "
            "or migrate that installation before creating a new managed install."
        )
    # ``--extra`` augments the installed capability set. There is no public
    # remove-extra operation, so a repair cannot silently uninstall an
    # already-enabled pack when a user adds another one later.
    selected_extras = _merge_capability_extras(previous.extras, requested_extras)
    selected_runtime_python = previous.runtime_python or runtime_python
    install_environment = dict(environment)
    install_environment["UV_VENV_CLEAR"] = "1"
    with _prepared_package_spec(
        selected_extras,
        ref=ref,
        release_opener=release_opener,
    ) as package_spec:
        install_command = [pipx, "install", "--force"]
        if selected_runtime_python is not None:
            install_command.extend(
                ["--python", selected_runtime_python, "--fetch-python=missing"]
            )
        install_command.append(package_spec)
        _run_streaming(
            install_command,
            runner=runner,
            environment=install_environment,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            description="pipx installation",
            failure_message="pipx could not install Ash.",
        )

    current_inspection = _read_pipx_state(
        pipx,
        runner=runner,
        environment=environment,
    )
    if current_inspection.error is not None:
        detail = current_inspection.error or "unknown pipx inspection failure"
        raise InstallError(
            "pipx installed Ash but its resulting state could not be read: "
            f"{detail}"
        )
    current = current_inspection.state
    if current is None or not current.installed:
        raise InstallError(
            "pipx reported a successful install, but Ash is absent from the "
            "resulting pipx state."
        )
    launcher_directory = _pipx_bin_directory(
        pipx,
        runner=runner,
        environment=environment,
    )
    executable = _executable_in_directory(launcher_directory)
    if not executable:
        raise InstallError(
            "Ash was installed, but pipx did not report its executable directory."
        )
    version = _verify_executable(
        executable,
        runner=runner,
        environment=environment,
    )
    _verify_release_version(version, ref=ref)
    restart_required = _ensure_shell_path(
        pipx,
        manager="pipx",
        launcher_directory=launcher_directory,
        runner=runner,
        environment=environment,
    )
    return InstallResult(
        manager="pipx",
        executable=executable,
        version=version,
        shell_restart_required=restart_required,
    )


def _pipx_bin_directory(
    pipx: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> str | None:
    configured = environment.get("PIPX_BIN_DIR")
    if configured:
        directory = configured
    else:
        completed = _run_captured(
            [pipx, "environment", "--value", "PIPX_BIN_DIR"],
            runner=runner,
            environment=environment,
            timeout=_QUERY_TIMEOUT_SECONDS,
            max_bytes=_MAX_QUERY_OUTPUT_BYTES,
            description="pipx environment query",
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            return None
        directory = str(getattr(completed, "stdout", "")).strip()
    if not directory:
        return None
    return directory


def _executable_in_directory(directory: str | None) -> str | None:
    if not directory:
        return None
    executable_name = "ash"
    return str(Path(directory) / executable_name)


def _pipx_home_directory(
    pipx: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> str | None:
    configured = environment.get("PIPX_HOME")
    if configured:
        return os.path.expanduser(configured)
    completed = _run_captured(
        [pipx, "environment", "--value", "PIPX_HOME"],
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_QUERY_OUTPUT_BYTES,
        description="pipx environment query",
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        return None
    directory = str(getattr(completed, "stdout", "")).strip()
    if not directory:
        return None
    return os.path.expanduser(directory)


def _corrupt_ash_metadata(pipx_home: str | None) -> Path | None:
    if not pipx_home:
        return None
    metadata_path = Path(
        os.path.abspath(
            (Path(pipx_home).expanduser() / "venvs" / _PACKAGE_NAME / "pipx_metadata.json")
        )
    )
    try:
        contents, _metadata = _read_bounded_file_with_identity(
            metadata_path,
            max_bytes=_MAX_METADATA_BYTES,
        )
        text = contents.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError):
        return metadata_path
    return None


def _completed_output_detail(completed: Any) -> str:
    for attribute in ("stderr", "stdout"):
        raw_value = getattr(completed, attribute, "")
        if isinstance(raw_value, bytes):
            value = raw_value.decode("utf-8", errors="replace")
        else:
            value = str(raw_value)
        value = value[:_MAX_QUERY_OUTPUT_BYTES].strip()
        if value:
            return " ".join(value.split())[:240]
    return ""


def _install_with_uv(
    uv: str,
    *,
    extras: Sequence[str],
    ref: str | None,
    runtime_python: str | None,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
    previous: _UvState,
    release_opener: Callable[..., Any],
) -> InstallResult:
    # Keep existing capability packs when adding one through a later
    # pipx/uv invocation. This makes upgrades and repairs additive and safe.
    selected_extras = _merge_capability_extras(previous.extras, extras)
    selected_runtime_python = previous.runtime_python or runtime_python
    with _prepared_package_spec(
        selected_extras,
        ref=ref,
        release_opener=release_opener,
    ) as package_spec:
        install_command = [uv, "tool", "install", "--force", "--reinstall"]
        if selected_runtime_python is not None:
            install_command.extend(["--python", selected_runtime_python])
        install_command.append(package_spec)
        _run_streaming(
            install_command,
            runner=runner,
            environment=environment,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            description="uv installation",
            failure_message="uv could not install Ash.",
        )
    current = _read_uv_state(uv, runner=runner, environment=environment)
    if not current.installed:
        raise InstallError(
            "uv reported a successful install, but Ash is absent from the resulting uv state."
        )
    directory = _run_captured(
        [uv, "tool", "dir", "--bin"],
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_QUERY_OUTPUT_BYTES,
        description="uv tool directory query",
    )
    if int(getattr(directory, "returncode", 1)) != 0:
        raise InstallError(
            "Ash was installed, but uv did not report its executable directory."
        )
    launcher_directory = str(getattr(directory, "stdout", "")).strip()
    executable = _executable_in_directory(launcher_directory)
    if not executable:
        raise InstallError(
            "Ash was installed, but uv did not report its executable directory."
        )
    version = _verify_executable(executable, runner=runner, environment=environment)
    _verify_release_version(version, ref=ref)
    restart_required = _ensure_shell_path(
        uv,
        manager="uv",
        launcher_directory=launcher_directory,
        runner=runner,
        environment=environment,
    )
    return InstallResult(
        manager="uv",
        executable=executable,
        version=version,
        shell_restart_required=restart_required,
    )


def _ensure_shell_path(
    manager_executable: str,
    *,
    manager: str,
    launcher_directory: str | None,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> bool:
    if launcher_directory:
        normalized_launcher_directory = _normalize_path(launcher_directory)
        path_directories = {
            _normalize_path(value)
            for value in environment.get("PATH", "").split(os.pathsep)
            if value
        }
        if normalized_launcher_directory in path_directories:
            return False
    command = (
        [manager_executable, "ensurepath"]
        if manager == "pipx"
        else [manager_executable, "tool", "update-shell"]
    )
    completed = _run_captured(
        command,
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_QUERY_OUTPUT_BYTES,
        description=f"{manager} shell path setup",
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = _completed_output_detail(completed)
        suffix = f": {detail}" if detail else ""
        raise InstallError(
            f"{manager} could not configure Ash's executable directory on PATH{suffix}"
        )
    return True


def _normalize_path(value: str) -> str:
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(value)))
    )


def _verify_executable(
    executable: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> str:
    verified = _run_captured(
        [executable, "--version"],
        runner=runner,
        environment=environment,
        timeout=_VERIFY_TIMEOUT_SECONDS,
        max_bytes=_MAX_VERSION_OUTPUT_BYTES,
        description="Ash executable verification",
    )
    if int(getattr(verified, "returncode", 1)) != 0:
        raise InstallError("Ash was installed, but `ash --version` failed.")
    version = str(getattr(verified, "stdout", "")).strip()
    if not version.startswith("ash "):
        raise InstallError("Ash verification returned an unexpected version response.")
    return version


def _read_pipx_state(
    pipx: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> _PipxInspection:
    completed = _run_captured(
        [pipx, "list", "--json"],
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_STATE_OUTPUT_BYTES,
        description="pipx state query",
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = _completed_output_detail(completed)
        if not detail:
            detail = (
                "pipx list --json exited with status "
                f"{int(getattr(completed, 'returncode', 1))}"
            )
        return _PipxInspection(None, detail)
    try:
        payload = _strict_json_loads(str(getattr(completed, "stdout", "")))
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        return _PipxInspection(None, f"pipx returned invalid JSON ({detail})")
    if not isinstance(payload, dict) or not isinstance(payload.get("venvs"), dict):
        return _PipxInspection(None, "pipx returned an unexpected JSON shape")
    ash_venv = payload["venvs"].get(_PACKAGE_NAME)
    if ash_venv is None:
        return _PipxInspection(_PipxState())
    if not isinstance(ash_venv, dict):
        return _PipxInspection(None, "pipx returned an unexpected JSON shape")
    metadata = ash_venv.get("metadata")
    if not isinstance(metadata, dict):
        return _PipxInspection(None, "pipx returned an unexpected JSON shape")
    main = metadata.get("main_package")
    if not isinstance(main, dict):
        return _PipxInspection(None, "pipx returned an unexpected JSON shape")
    package_spec = str(main.get("package_or_url", ""))
    match = _EXTRAS_PATTERN.match(package_spec)
    extras = _normalize_extras(
        match.group(1).split(",") if match and match.group(1) else ()
    )
    executable = None
    app_paths = main.get("app_paths", [])
    if isinstance(app_paths, list):
        for entry in app_paths:
            if isinstance(entry, dict) and entry.get("__Path__"):
                executable = str(entry["__Path__"])
                break
    runtime_python = _supported_runtime_minor(metadata.get("python_version"))
    return _PipxInspection(
        _PipxState(
            installed=True,
            extras=extras,
            executable=executable,
            runtime_python=runtime_python,
        )
    )


def _read_uv_state(
    uv: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> _UvState:
    base_command = [
        uv,
        "tool",
        "list",
        "--show-paths",
        "--show-version-specifiers",
        "--show-extras",
    ]
    completed = _run_captured(
        [*base_command, "--show-python"],
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_STATE_OUTPUT_BYTES,
        description="uv state query",
    )
    show_python = int(getattr(completed, "returncode", 1)) == 0
    if not show_python:
        completed = _run_captured(
            base_command,
            runner=runner,
            environment=environment,
            timeout=_QUERY_TIMEOUT_SECONDS,
            max_bytes=_MAX_STATE_OUTPUT_BYTES,
            description="uv compatibility state query",
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            detail = _completed_output_detail(completed)
            suffix = f": {detail}" if detail else ""
            raise InstallError(f"Could not inspect the existing uv installation{suffix}")
    output = str(getattr(completed, "stdout", ""))
    lines = output.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(
                rf"^{re.escape(_PACKAGE_NAME)}\s",
                line,
                re.IGNORECASE,
            )
        ),
        None,
    )
    if header_index is None:
        return _UvState()
    block_lines = [lines[header_index]]
    for line in lines[header_index + 1 :]:
        if not line.startswith("-"):
            break
        block_lines.append(line)
    block = "\n".join(block_lines)
    extras_match = _UV_EXTRAS_PATTERN.search(block_lines[0])
    extras = _normalize_extras(extras_match.group(1).split(",") if extras_match else ())
    path_match = _UV_ASH_PATH_PATTERN.search(block)
    executable = path_match.group(1).strip() if path_match else None
    python_match = _UV_PYTHON_PATTERN.search(block_lines[0]) if show_python else None
    runtime_python = _supported_runtime_minor(
        python_match.group(1) if python_match else None
    )
    return _UvState(
        installed=True,
        extras=extras,
        executable=executable,
        runtime_python=runtime_python,
    )


def _run_streaming(
    command: Sequence[str],
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
    timeout: float,
    description: str,
    failure_message: str,
) -> Any:
    """Run a user-visible installer command with a hard upper time limit."""

    if runner is subprocess.run:
        process_tree_plan = _prepare_process_tree()
        popen_kwargs: dict[str, Any] = {"env": dict(environment)}
        popen_kwargs.update(process_tree_plan.spawn_options)
        process = subprocess.Popen(list(command), **popen_kwargs)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                _terminate_process(process, plan=process_tree_plan)
            except InstallError as cleanup_error:
                raise InstallError(
                    f"{description} timed out after {timeout:g} seconds; "
                    f"process-tree cleanup failed: {cleanup_error}"
                ) from exc
            raise InstallError(
                f"{description} timed out after {timeout:g} seconds."
            ) from exc
        except OSError as exc:
            try:
                _terminate_process(process, plan=process_tree_plan)
            except InstallError as cleanup_error:
                raise InstallError(
                    f"{description} failed while waiting; "
                    f"process-tree cleanup failed: {cleanup_error}"
                ) from exc
            raise InstallError(
                f"{description} failed while waiting: {type(exc).__name__}"
            ) from exc
        except BaseException as primary:
            try:
                _terminate_process(process, plan=process_tree_plan)
            except InstallError as cleanup_error:
                _attach_cleanup_failure(primary, cleanup_error)
            raise
        completed = _CapturedResult(returncode=returncode)
        if returncode != 0:
            raise InstallError(failure_message)
        return completed

    try:
        completed = runner(
            list(command),
            env=dict(environment),
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"{description} timed out after {timeout:g} seconds."
        ) from exc
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = _completed_output_detail(completed)
        raise InstallError(
            failure_message
            + (f" {detail}" if detail else "")
        )
    return completed


def _run_captured(
    command: Sequence[str],
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
    timeout: float,
    max_bytes: int,
    description: str,
) -> Any:
    """Run a quiet query without retaining unbounded child-process output."""

    if runner is subprocess.run:
        return _run_bounded_subprocess(
            command,
            environment=environment,
            timeout=timeout,
            max_bytes=max_bytes,
            description=description,
        )
    try:
        completed = runner(
            list(command),
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"{description.capitalize()} timed out after {timeout:g} seconds."
        ) from exc
    _validate_captured_result(completed, max_bytes=max_bytes, description=description)
    return completed


def _validate_captured_result(
    completed: Any,
    *,
    max_bytes: int,
    description: str,
) -> None:
    for attribute in ("stdout", "stderr"):
        value = getattr(completed, attribute, "")
        if isinstance(value, bytes):
            size = len(value)
        else:
            size = len(str(value).encode("utf-8", errors="replace"))
        if size > max_bytes:
            raise InstallError(
                f"{description.capitalize()} returned more than {max_bytes} bytes."
            )


def _run_bounded_subprocess(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    max_bytes: int,
    description: str,
) -> _CapturedResult:
    """Capture at most ``max_bytes`` from each pipe and terminate noisy tools."""

    popen_kwargs: dict[str, Any] = {
        "env": dict(environment),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    process_tree_plan = _prepare_process_tree()
    popen_kwargs.update(process_tree_plan.spawn_options)
    process = subprocess.Popen(list(command), **popen_kwargs)
    events: queue.Queue[tuple[str, bytes | None]] = queue.Queue(
        maxsize=_CAPTURE_QUEUE_SIZE
    )
    stop_readers = threading.Event()
    readers: list[threading.Thread] = []

    def drain(name: str, stream: Any) -> None:
        try:
            while not stop_readers.is_set():
                chunk = stream.read(_CAPTURE_CHUNK_BYTES)
                if not chunk:
                    break
                while not stop_readers.is_set():
                    try:
                        events.put((name, chunk), timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass
        finally:
            while not stop_readers.is_set():
                try:
                    events.put((name, None), timeout=0.1)
                    break
                except queue.Full:
                    continue

    assert process.stdout is not None
    assert process.stderr is not None
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        thread = threading.Thread(target=drain, args=(name, stream), daemon=True)
        thread.start()
        readers.append(thread)

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    finished_streams: set[str] = set()
    deadline = time.monotonic() + timeout
    cleanup_done = False
    try:
        while len(finished_streams) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cleanup_done = True
                try:
                    _terminate_process(process, plan=process_tree_plan)
                except InstallError as cleanup_error:
                    raise InstallError(
                        f"{description.capitalize()} timed out after {timeout:g} "
                        f"seconds; process-tree cleanup failed: {cleanup_error}"
                    )
                raise InstallError(
                    f"{description.capitalize()} timed out after {timeout:g} seconds."
                )
            try:
                name, chunk = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                finished_streams.add(name)
                continue
            target = captured[name]
            if len(target) + len(chunk) > max_bytes:
                try:
                    _terminate_process(process, plan=process_tree_plan)
                except InstallError as cleanup_error:
                    cleanup_done = True
                    raise InstallError(
                        f"{description.capitalize()} returned more than {max_bytes} "
                        "bytes; process-tree cleanup failed: "
                        f"{cleanup_error}"
                    )
                cleanup_done = True
                raise InstallError(
                    f"{description.capitalize()} returned more than {max_bytes} bytes."
                )
            target.extend(chunk)
        returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        if not cleanup_done:
            try:
                _terminate_process(process, plan=process_tree_plan)
            except InstallError as cleanup_error:
                raise InstallError(
                    f"{description.capitalize()} timed out after {timeout:g} seconds; "
                    f"process-tree cleanup failed: {cleanup_error}"
                ) from exc
        raise InstallError(
            f"{description.capitalize()} timed out after {timeout:g} seconds."
        ) from exc
    except BaseException as primary:
        if not cleanup_done:
            try:
                _terminate_process(process, plan=process_tree_plan)
            except InstallError as cleanup_error:
                _attach_cleanup_failure(primary, cleanup_error)
        raise
    finally:
        stop_readers.set()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for thread in readers:
            thread.join(timeout=1)
    return _CapturedResult(
        returncode=returncode,
        stdout=bytes(captured["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(captured["stderr"]).decode("utf-8", errors="replace"),
    )


def _terminate_process(
    process: subprocess.Popen[Any],
    *,
    plan: _InstallerProcessTreePlan | None = None,
) -> None:
    options = plan.spawn_options if plan is not None else {"start_new_session": True}
    manages_group = bool(options.get("start_new_session") and _KILLPG is not None)
    root_exited = process.poll() is not None
    signaled_group = manages_group and _signal_process_group(process.pid, signal.SIGTERM)
    if not signaled_group:
        if root_exited:
            return
        process.terminate()

    if not root_exited:
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if not signaled_group:
                process.kill()
            elif _SIGKILL is not None:
                _signal_process_group(process.pid, _SIGKILL)
            try:
                process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass

    if signaled_group and _process_group_exists(process.pid):
        deadline = time.monotonic() + _PROCESS_CLEANUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline and _process_group_exists(process.pid):
            time.sleep(0.05)
        if _process_group_exists(process.pid) and _SIGKILL is not None:
            _signal_process_group(process.pid, _SIGKILL)
    elif not signaled_group and process.poll() is None:
        process.kill()
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _signal_process_group(pid: int, signum: int) -> bool:
    """Signal a POSIX process group when the runtime exposes that primitive."""

    if _KILLPG is None:
        return False
    try:
        _KILLPG(pid, signum)
    except (OSError, ProcessLookupError):
        return False
    return True


def _process_group_exists(pid: int) -> bool:
    if _KILLPG is None:
        return False
    try:
        _KILLPG(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    contents, _metadata = _read_bounded_file_with_identity(path, max_bytes=max_bytes)
    return contents


def _read_bounded_file_with_identity(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, os.stat_result]:
    if path.is_symlink():
        raise ValueError(f"refusing to read symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"refusing to read non-regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            contents = handle.read(max_bytes + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(contents) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    return contents, metadata


def _normalize_extras(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted({value.strip().casefold() for value in values if value.strip()})
    )


def _normalize_requested_extras(values: Sequence[str]) -> tuple[str, ...]:
    normalized = _normalize_extras(values)
    unsupported = tuple(value for value in normalized if value not in _SUPPORTED_EXTRA_SET)
    if unsupported:
        raise InstallError(
            "Unsupported Ash capability extra(s): "
            + ", ".join(unsupported)
            + ". Supported extras: "
            + ", ".join(SUPPORTED_EXTRAS)
        )
    return normalized


def _merge_capability_extras(
    existing: Sequence[str],
    requested: Sequence[str],
) -> tuple[str, ...]:
    normalized_existing = _normalize_extras(existing)
    unsupported_existing = tuple(
        value for value in normalized_existing if value not in _SUPPORTED_EXTRA_SET
    )
    if unsupported_existing:
        print(
            "Warning: ignoring obsolete or unsupported Ash capability extras from "
            "the existing installation: " + ", ".join(unsupported_existing),
            file=sys.stderr,
        )
    return _normalize_extras(
        [
            *(value for value in normalized_existing if value in _SUPPORTED_EXTRA_SET),
            *requested,
        ]
    )


def _supported_runtime_minor(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PYTHON_VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    major, minor = int(match.group(1)), int(match.group(2))
    if _RUNTIME_MIN_PYTHON <= (major, minor) < _RUNTIME_MAX_PYTHON:
        return f"{major}.{minor}"
    return None


def _runtime_python_override(python_version: Sequence[int]) -> str:
    """Select an explicit supported runtime for package-manager installation."""

    major, minor = int(python_version[0]), int(python_version[1])
    version = (major, minor)
    if _RUNTIME_MIN_PYTHON <= version < _RUNTIME_MAX_PYTHON:
        return f"{major}.{minor}"
    return _RUNTIME_FALLBACK_PYTHON


def _release_wheel(
    ref: str,
    *,
    opener: Callable[..., Any],
) -> _ReleaseWheel:
    if _RELEASE_REF_PATTERN.fullmatch(ref) is None:
        raise InstallError(
            f"Ash release ref must use the canonical ash-v<version> form: {ref!r}"
        )
    release_url = f"{_RELEASES_API}/tags/{urllib.parse.quote(ref, safe='')}"
    request = urllib.request.Request(
        release_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ash-installer",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        },
    )
    try:
        with opener(request, timeout=_QUERY_TIMEOUT_SECONDS) as response:
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                final_url = str(geturl())
                final = urllib.parse.urlsplit(final_url)
                expected = urllib.parse.urlsplit(release_url)
                if (
                    final.scheme != "https"
                    or final.hostname != "api.github.com"
                    or final.username is not None
                    or final.password is not None
                    or final.port not in {None, 443}
                    or final.path != expected.path
                    or final.query != expected.query
                    or final.fragment
                ):
                    raise InstallError(
                        f"Ash release {ref} metadata redirected to an unexpected endpoint."
                    )
            raw = response.read(_MAX_RELEASE_METADATA_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise InstallError(
            f"Could not resolve Ash release {ref}: GitHub returned HTTP {exc.code}."
        ) from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise InstallError(f"Could not resolve Ash release {ref}: {exc}") from exc
    if len(raw) > _MAX_RELEASE_METADATA_BYTES:
        raise InstallError("GitHub release metadata exceeded 1 MiB.")

    try:
        payload = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError("GitHub returned invalid Ash release metadata.") from exc
    if not isinstance(payload, dict):
        raise InstallError("GitHub returned invalid Ash release metadata.")
    if payload.get("tag_name") != ref or payload.get("immutable") is not True:
        raise InstallError(
            f"Ash release {ref} is missing, mutable, or does not match its tag."
        )
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise InstallError(f"Ash release {ref} does not contain release assets.")
    wheels = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and asset.get("state") == "uploaded"
        and isinstance(asset.get("name"), str)
        and _RELEASE_WHEEL_PATTERN.fullmatch(asset["name"]) is not None
    ]
    if len(wheels) != 1:
        raise InstallError(
            f"Ash release {ref} must contain exactly one uploaded universal wheel."
        )
    wheel = wheels[0]
    name = wheel["name"]
    download_url = wheel.get("browser_download_url")
    digest = wheel.get("digest")
    size = wheel.get("size")
    if (
        not isinstance(download_url, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or size > _MAX_RELEASE_WHEEL_BYTES
    ):
        raise InstallError(f"Ash release {ref} has invalid wheel metadata.")
    parsed = urllib.parse.urlsplit(download_url)
    parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 7
        or parts[1:5] != ["Suraj-H675", "Ash-Harness", "releases", "download"]
        or urllib.parse.unquote(parts[5]) != ref
        or urllib.parse.unquote(parts[6]) != name
    ):
        raise InstallError(f"Ash release {ref} has an untrusted wheel URL.")
    return _ReleaseWheel(
        name=name,
        download_url=download_url,
        sha256=digest.removeprefix("sha256:"),
        size=size,
    )


@contextlib.contextmanager
def _prepared_package_spec(
    extras: Sequence[str],
    *,
    ref: str | None,
    release_opener: Callable[..., Any] = urllib.request.urlopen,
) -> Iterator[str]:
    suffix = f"[{','.join(extras)}]" if extras else ""
    if ref is None:
        yield f"{_PACKAGE_NAME}{suffix} @ git+{REPOSITORY_URL}"
        return

    wheel = _release_wheel(ref, opener=release_opener)
    request = urllib.request.Request(
        wheel.download_url,
        headers={"User-Agent": "ash-installer"},
    )
    with tempfile.TemporaryDirectory(prefix="ash-install-") as raw_directory:
        directory = Path(raw_directory)
        target = directory / wheel.name
        digest = hashlib.sha256()
        total = 0
        try:
            with release_opener(request, timeout=_QUERY_TIMEOUT_SECONDS) as response:
                final_url = wheel.download_url
                geturl = getattr(response, "geturl", None)
                if callable(geturl):
                    final_url = str(geturl())
                final_parts = urllib.parse.urlsplit(final_url)
                if (
                    final_parts.scheme != "https"
                    or final_parts.hostname is None
                    or final_parts.username is not None
                    or final_parts.password is not None
                ):
                    raise InstallError(
                        f"Ash release {ref} wheel download redirected to an untrusted URL."
                    )
                final_host = final_parts.hostname
                if final_host not in {
                    "github.com",
                    "release-assets.githubusercontent.com",
                }:
                    raise InstallError(
                        f"Ash release {ref} wheel download redirected to an untrusted host."
                    )
                with target.open("xb") as handle:
                    if os.name != "nt":
                        os.chmod(target, 0o600)
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > wheel.size or total > _MAX_RELEASE_WHEEL_BYTES:
                            raise InstallError(
                                f"Ash release {ref} wheel exceeded its declared size."
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        except urllib.error.HTTPError as exc:
            raise InstallError(
                f"Could not download Ash release {ref} wheel: HTTP {exc.code}."
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise InstallError(
                f"Could not download Ash release {ref} wheel: {exc}"
            ) from exc
        if total != wheel.size:
            raise InstallError(
                f"Ash release {ref} wheel size did not match release metadata."
            )
        if digest.hexdigest() != wheel.sha256:
            raise InstallError(
                f"Ash release {ref} wheel SHA-256 did not match release metadata."
            )
        yield (
            f"{_PACKAGE_NAME}{suffix} @ {target.resolve().as_uri()}"
            f"#sha256={wheel.sha256}"
        )


def _verify_release_version(version: str, *, ref: str | None) -> None:
    if ref is None:
        return
    expected = ref.removeprefix("ash-v")
    actual = version.strip()
    prefix = "ash "
    if not actual.casefold().startswith(prefix) or actual[len(prefix) :].strip() != expected:
        raise InstallError(
            "Ash installation completed but the installed version "
            f"{version!r} does not match release {ref}."
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    installer: Callable[..., InstallResult] = install,
    stdout: Any = None,
    stderr: Any = None,
    python_version: Sequence[int] = sys.version_info[:2],
    host_platform: str = sys.platform,
) -> int:
    """Run the standalone installer CLI."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    unsupported_platform = _platform_support_error(host_platform)
    if unsupported_platform is not None:
        print(f"Ash installation could not continue: {unsupported_platform}", file=errors)
        return 1
    major, minor = int(python_version[0]), int(python_version[1])
    if (major, minor) < _BOOTSTRAP_MIN_PYTHON:
        print(
            "Ash's installer requires Python 3.10 or newer; "
            f"this interpreter is Python {major}.{minor}.",
            file=errors,
        )
        return 1
    parser = argparse.ArgumentParser(
        prog="install-ash",
        description="Install, upgrade, or repair Ash.",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        choices=SUPPORTED_EXTRAS,
        help="Install an optional capability pack; repeat for multiple packs.",
    )
    parser.add_argument(
        "--ref",
        help="Install a specific immutable Ash release tag (ash-v...).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.ref or _RELEASE_REF_PATTERN.fullmatch(args.ref) is None:
        print(
            "Ash installation could not continue: --ref must name a canonical "
            "Ash release tag in ash-v<version> form. Use the verified release "
            "bootstrap rather than installing a moving branch.",
            file=errors,
        )
        return 1
    try:
        result = installer(
            extras=args.extra,
            ref=args.ref,
            runtime_python=_runtime_python_override(python_version),
        )
    except InstallError as exc:
        print(f"Ash installation could not continue: {exc}", file=errors)
        if "Neither pipx nor uv" in str(exc):
            print("Install pipx or uv, then run this installer again.", file=errors)
        return 1
    except OSError as exc:
        print(
            "Ash installation could not continue: could not start the installer "
            f"backend ({exc}).",
            file=errors,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "Ash installation cancelled; no Ash configuration was changed.", file=errors
        )
        return 130
    print(f"Ash is ready ({result.version}) via {result.manager}.", file=output)
    print(f"Executable: {result.executable}", file=output)
    if result.shell_restart_required:
        print(
            "PATH setup was requested; restart your terminal before running `ash`.",
            file=output,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
