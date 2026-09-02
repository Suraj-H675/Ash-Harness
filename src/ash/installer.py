"""Dependency-free public installer for Ash.

The module is intentionally usable both from an installed package and as a
downloaded script.  It owns package-manager detection, repair flags, and final
executable verification so users do not need to understand pipx/uv internals.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"
SUPPORTED_EXTRAS = ("a2a", "acp", "browser", "server", "vector")
_PACKAGE_NAME = "ash-ai"
_EXTRAS_PATTERN = re.compile(
    rf"^\s*{re.escape(_PACKAGE_NAME)}(?:\[([^]]+)\])?(?:\s*@.*)?\s*$",
    re.IGNORECASE,
)
_UV_EXTRAS_PATTERN = re.compile(r"\[extras:\s*([^]]+)\]", re.IGNORECASE)
_UV_ASH_PATH_PATTERN = re.compile(r"^-\s+ash\s+\(([^)]+)\)\s*$", re.MULTILINE)
_INSTALL_TIMEOUT_SECONDS = 15 * 60
_QUERY_TIMEOUT_SECONDS = 30
_VERIFY_TIMEOUT_SECONDS = 30
_MAX_STATE_OUTPUT_BYTES = 1024 * 1024
_MAX_QUERY_OUTPUT_BYTES = 16 * 1024
_MAX_VERSION_OUTPUT_BYTES = 16 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_CAPTURE_CHUNK_BYTES = 8192
_CAPTURE_QUEUE_SIZE = 8


class InstallError(RuntimeError):
    """A concise, user-actionable installation failure."""


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


@dataclass(frozen=True)
class _CapturedResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def install(
    *,
    extras: Sequence[str] = (),
    ref: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> InstallResult:
    """Install or repair Ash, preserving existing pipx capability extras."""

    environment = dict(os.environ if environ is None else environ)
    pipx = which("pipx")
    uv = which("uv")
    if pipx is None:
        if uv is None:
            raise InstallError("Neither pipx nor uv is installed.")
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        return _install_with_uv(
            uv,
            extras=extras or previous_uv.extras,
            ref=ref,
            runner=runner,
            environment=environment,
            previous=previous_uv,
        )

    try:
        inspection = _read_pipx_state(
            pipx,
            runner=runner,
            environment=environment,
        )
    except OSError as exc:
        if uv is None:
            raise InstallError(f"pipx was found but could not start ({exc}).") from exc
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        return _install_with_uv(
            uv,
            extras=extras or previous_uv.extras,
            ref=ref,
            runner=runner,
            environment=environment,
            previous=previous_uv,
        )
    quarantine_path: Path | None = None
    repairing_corrupt_metadata = False
    if inspection.succeeded:
        previous = inspection.state
        assert previous is not None
    else:
        pipx_home = _pipx_home_directory(
            pipx,
            runner=runner,
            environment=environment,
        )
        metadata_path = _corrupt_ash_metadata_path(pipx_home)
        if metadata_path is None:
            detail = inspection.error or "unknown pipx inspection failure"
            raise InstallError(
                f"Could not inspect the existing pipx installation: {detail}"
            )
        quarantine_path = _quarantine_pipx_metadata(metadata_path)
        repairing_corrupt_metadata = True
        previous = _PipxState()
        if not extras:
            print(
                "Warning: Ash's corrupt pipx metadata did not preserve optional "
                "extras; repairing the base installation.",
                file=sys.stderr,
            )
    if (
        not previous.installed
        and uv is not None
        and not repairing_corrupt_metadata
    ):
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        if previous_uv.installed:
            return _install_with_uv(
                uv,
                extras=extras or previous_uv.extras,
                ref=ref,
                runner=runner,
                environment=environment,
                previous=previous_uv,
            )
    # ``--extra`` augments the installed capability set. There is no public
    # remove-extra operation, so a repair cannot silently uninstall an
    # already-enabled pack when a user adds another one later.
    selected_extras = _normalize_extras([*previous.extras, *extras])
    package_spec = _package_spec(selected_extras, ref=ref)
    install_environment = dict(environment)
    install_environment["UV_VENV_CLEAR"] = "1"
    try:
        _run_streaming(
            [pipx, "install", "--force", package_spec],
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
        executable = (
            current.executable
            or previous.executable
            or _executable_in_directory(launcher_directory)
            or which("ash")
        )
        if not executable:
            raise InstallError(
                "Ash was installed, but its executable could not be located."
            )
        version = _verify_executable(
            executable,
            runner=runner,
            environment=environment,
        )
        restart_required = _ensure_shell_path(
            pipx,
            manager="pipx",
            launcher_directory=launcher_directory,
            runner=runner,
            environment=environment,
        )
        if quarantine_path is not None:
            quarantine_path.unlink()
    except Exception as exc:
        if not repairing_corrupt_metadata or quarantine_path is None:
            raise
        detail = str(exc).strip() or type(exc).__name__
        raise InstallError(
            "Corrupt pipx metadata for Ash was detected, but automatic repair "
            "failed. "
            f"The original metadata was preserved at {quarantine_path}. "
            f"{detail}"
        ) from exc
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
    executable_name = "ash.exe" if os.name == "nt" else "ash"
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


def _corrupt_ash_metadata_path(pipx_home: str | None) -> Path | None:
    if not pipx_home:
        return None
    metadata_path = Path(pipx_home) / "venvs" / _PACKAGE_NAME / "pipx_metadata.json"
    try:
        contents = _read_bounded_file(metadata_path, max_bytes=_MAX_METADATA_BYTES)
        text = contents.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return metadata_path
    return None


def _quarantine_pipx_metadata(metadata_path: Path) -> Path:
    while True:
        quarantine_path = metadata_path.with_name(
            f"{metadata_path.name}.corrupt-{uuid.uuid4().hex}"
        )
        if not quarantine_path.exists():
            break
    metadata_path.rename(quarantine_path)
    return quarantine_path


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
    runner: Callable[..., Any],
    environment: Mapping[str, str],
    previous: _UvState,
) -> InstallResult:
    # Keep existing capability packs when adding one through a later
    # pipx/uv invocation. This makes upgrades and repairs additive and safe.
    selected_extras = _normalize_extras([*previous.extras, *extras])
    package_spec = _package_spec(selected_extras, ref=ref)
    _run_streaming(
        [uv, "tool", "install", "--force", "--reinstall", package_spec],
        runner=runner,
        environment=environment,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        description="uv installation",
        failure_message="uv could not install Ash.",
    )
    executable = previous.executable
    if not executable:
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
        executable_name = "ash.exe" if os.name == "nt" else "ash"
        executable = str(
            Path(str(getattr(directory, "stdout", "")).strip()) / executable_name
        )
    version = _verify_executable(executable, runner=runner, environment=environment)
    restart_required = _ensure_shell_path(
        uv,
        manager="uv",
        launcher_directory=str(Path(executable).parent),
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
    _run_captured(
        command,
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_QUERY_OUTPUT_BYTES,
        description=f"{manager} shell path setup",
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
        payload = json.loads(str(getattr(completed, "stdout", "")))
    except json.JSONDecodeError as exc:
        return _PipxInspection(None, f"pipx returned invalid JSON ({exc.msg})")
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
    return _PipxInspection(
        _PipxState(installed=True, extras=extras, executable=executable)
    )


def _read_uv_state(
    uv: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> _UvState:
    completed = _run_captured(
        [
            uv,
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
        ],
        runner=runner,
        environment=environment,
        timeout=_QUERY_TIMEOUT_SECONDS,
        max_bytes=_MAX_STATE_OUTPUT_BYTES,
        description="uv state query",
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        return _UvState()
    output = str(getattr(completed, "stdout", ""))
    if not re.search(
        rf"^{re.escape(_PACKAGE_NAME)}\s",
        output,
        re.MULTILINE | re.IGNORECASE,
    ):
        return _UvState()
    extras_match = _UV_EXTRAS_PATTERN.search(output)
    extras = _normalize_extras(extras_match.group(1).split(",") if extras_match else ())
    path_match = _UV_ASH_PATH_PATTERN.search(output)
    executable = path_match.group(1).strip() if path_match else None
    return _UvState(installed=True, extras=extras, executable=executable)


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
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True
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
    try:
        while len(finished_streams) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
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
                _terminate_process(process)
                raise InstallError(
                    f"{description.capitalize()} returned more than {max_bytes} bytes."
                )
            target.extend(chunk)
        returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise InstallError(
            f"{description.capitalize()} timed out after {timeout:g} seconds."
        ) from exc
    except BaseException:
        _terminate_process(process)
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


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
        else:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ValueError(f"refusing to read symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            contents = handle.read(max_bytes + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(contents) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    return contents


def _normalize_extras(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted({value.strip().casefold() for value in values if value.strip()})
    )


def _package_spec(extras: Sequence[str], *, ref: str | None) -> str:
    suffix = f"[{','.join(extras)}]" if extras else ""
    revision = f"@{ref}" if ref else ""
    return f"{_PACKAGE_NAME}{suffix} @ git+{REPOSITORY_URL}{revision}"


def main(
    argv: Sequence[str] | None = None,
    *,
    installer: Callable[..., InstallResult] = install,
    stdout: Any = None,
    stderr: Any = None,
    python_version: Sequence[int] = sys.version_info[:2],
) -> int:
    """Run the standalone installer CLI."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    major, minor = int(python_version[0]), int(python_version[1])
    if (major, minor) < (3, 11):
        print(
            f"Ash requires Python 3.11 or newer; this interpreter is Python {major}.{minor}.",
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
    parser.add_argument("--ref", help="Install a specific Git branch, tag, or commit.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = installer(extras=args.extra, ref=args.ref)
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
