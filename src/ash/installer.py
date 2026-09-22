"""Dependency-free public installer for Ash.

The module is intentionally usable both from an installed package and as a
downloaded script.  It owns package-manager detection, repair flags, and final
executable verification so users do not need to understand pipx/uv internals.
"""

from __future__ import annotations

import argparse
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
_WINDOWS_TASKKILL_TIMEOUT_SECONDS = 5.0
_KILLPG = getattr(os, "killpg", None)
_SIGKILL = getattr(signal, "SIGKILL", None)


class InstallError(RuntimeError):
    """A concise, user-actionable installation failure."""


@dataclass(frozen=True)
class _InstallerProcessTreePlan:
    """Dependency-free preflight for installer-managed subprocesses."""

    spawn_options: dict[str, Any]
    taskkill_path: str | None
    is_windows: bool


def _prepare_process_tree() -> _InstallerProcessTreePlan:
    if os.name == "nt":
        taskkill_path = _resolve_system_taskkill()
        if taskkill_path is None:
            raise InstallError(
                "reliable Windows descendant cleanup is unavailable: "
                "taskkill was not found"
            )
        return _InstallerProcessTreePlan(
            spawn_options={
                "creationflags": getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            },
            taskkill_path=taskkill_path,
            is_windows=True,
        )
    return _InstallerProcessTreePlan(
        spawn_options={"start_new_session": True},
        taskkill_path=None,
        is_windows=False,
    )


def _resolve_system_taskkill() -> str | None:
    """Resolve taskkill from Windows' system directory, never ambient PATH."""

    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        return None
    candidate = os.path.join(system_root, "System32", "taskkill.exe")
    try:
        resolved_root = os.path.realpath(system_root)
        resolved_candidate = os.path.realpath(candidate)
        common_root = os.path.commonpath((resolved_root, resolved_candidate))
    except (OSError, RuntimeError, ValueError):
        return None
    if os.path.normcase(common_root) != os.path.normcase(resolved_root):
        return None
    if os.path.basename(os.path.dirname(resolved_candidate)).casefold() != "system32":
        return None
    if not os.path.isfile(resolved_candidate):
        return None
    return resolved_candidate


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


@dataclass(frozen=True)
class _OwnedMetadataFile:
    path: Path
    device: int
    inode: int
    sha256: str


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
    quarantine: _OwnedMetadataFile | None = None
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
        corrupt_metadata = _corrupt_ash_metadata(pipx_home)
        if corrupt_metadata is None:
            detail = inspection.error or "unknown pipx inspection failure"
            raise InstallError(
                f"Could not inspect the existing pipx installation: {detail}"
            )
        quarantine = _quarantine_pipx_metadata_owned(corrupt_metadata)
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
        if quarantine is not None:
            try:
                _remove_owned_quarantine(quarantine)
            except InstallError as exc:
                print(
                    "Warning: Ash repaired pipx successfully but left the "
                    f"metadata quarantine untouched because ownership changed: {exc}",
                    file=sys.stderr,
                )
    except Exception as exc:
        if not repairing_corrupt_metadata or quarantine is None:
            raise
        detail = str(exc).strip() or type(exc).__name__
        raise InstallError(
            "Corrupt pipx metadata for Ash was detected, but automatic repair "
            "failed. "
            f"The original metadata was preserved at {quarantine.path}. "
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


def _corrupt_ash_metadata(pipx_home: str | None) -> _OwnedMetadataFile | None:
    if not pipx_home:
        return None
    metadata_path = Path(
        os.path.abspath(
            (Path(pipx_home).expanduser() / "venvs" / _PACKAGE_NAME / "pipx_metadata.json")
        )
    )
    try:
        contents, metadata = _read_bounded_file_with_identity(
            metadata_path,
            max_bytes=_MAX_METADATA_BYTES,
        )
        text = contents.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return _OwnedMetadataFile(
            path=metadata_path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            sha256=hashlib.sha256(contents).hexdigest(),
        )
    return None


def _corrupt_ash_metadata_path(pipx_home: str | None) -> Path | None:
    corrupt = _corrupt_ash_metadata(pipx_home)
    return corrupt.path if corrupt is not None else None


def _quarantine_pipx_metadata(metadata_path: Path) -> Path:
    contents, metadata = _read_bounded_file_with_identity(
        metadata_path,
        max_bytes=_MAX_METADATA_BYTES,
    )
    owned = _OwnedMetadataFile(
        path=Path(os.path.abspath(metadata_path.expanduser())),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        sha256=hashlib.sha256(contents).hexdigest(),
    )
    return _quarantine_pipx_metadata_owned(owned).path


def _quarantine_pipx_metadata_owned(
    metadata: _OwnedMetadataFile,
) -> _OwnedMetadataFile:
    if not _installer_supports_dir_fd():
        if os.name == "nt":
            return _quarantine_pipx_metadata_owned_windows(metadata)
        return _quarantine_pipx_metadata_owned_fallback(metadata)

    try:
        parent_descriptor = _open_installer_directory(metadata.path.parent)
    except OSError as exc:
        raise InstallError(
            f"could not safely open pipx metadata directory: {exc}"
        ) from exc
    source_descriptor = -1
    try:
        source_name = metadata.path.name
        source_flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            source_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            source_flags |= os.O_NONBLOCK
        source_descriptor = os.open(
            source_name,
            source_flags,
            dir_fd=parent_descriptor,
        )
        source = os.fstat(source_descriptor)
        if (source.st_dev, source.st_ino) != (metadata.device, metadata.inode):
            raise InstallError("pipx metadata changed before it could be quarantined")
        if not stat.S_ISREG(source.st_mode):
            raise InstallError("pipx metadata is no longer a regular file")
        source_bytes = _read_bounded_descriptor(
            source_descriptor,
            max_bytes=_MAX_METADATA_BYTES,
            path=metadata.path,
        )
        if hashlib.sha256(source_bytes).hexdigest() != metadata.sha256:
            raise InstallError("pipx metadata contents changed before quarantine")

        for _ in range(32):
            quarantine_name = f"{source_name}.corrupt-{uuid.uuid4().hex}"
            quarantine_path = metadata.path.with_name(quarantine_name)
            quarantine_descriptor = -1
            try:
                quarantine_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_CLOEXEC"):
                    quarantine_flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    quarantine_flags |= os.O_NOFOLLOW
                quarantine_descriptor = os.open(
                    quarantine_name,
                    quarantine_flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise InstallError(
                    f"could not safely create pipx metadata quarantine: {exc}"
                ) from exc
            try:
                if hasattr(os, "fchmod") and os.name != "nt":
                    os.fchmod(quarantine_descriptor, 0o600)
                _write_installer_descriptor(quarantine_descriptor, source_bytes)
                os.fsync(quarantine_descriptor)
                quarantined = os.fstat(quarantine_descriptor)
                current = os.stat(
                    source_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (
                    metadata.device,
                    metadata.inode,
                ):
                    raise InstallError(
                        "pipx metadata changed while being quarantined"
                    )
                os.unlink(source_name, dir_fd=parent_descriptor)
                return _OwnedMetadataFile(
                    path=quarantine_path,
                    device=quarantined.st_dev,
                    inode=quarantined.st_ino,
                    sha256=metadata.sha256,
                )
            except Exception:
                if quarantine_descriptor >= 0:
                    try:
                        quarantined = os.fstat(quarantine_descriptor)
                        _unlink_installer_entry_if_same(
                            parent_descriptor,
                            quarantine_name,
                            quarantined,
                        )
                    except OSError:
                        pass
                raise
            finally:
                if quarantine_descriptor >= 0:
                    os.close(quarantine_descriptor)
        raise InstallError("could not allocate a unique pipx metadata quarantine name")
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(
            f"could not safely quarantine corrupt pipx metadata: {exc}"
        ) from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(parent_descriptor)


def _quarantine_pipx_metadata_owned_fallback(
    metadata: _OwnedMetadataFile,
) -> _OwnedMetadataFile:
    raise InstallError(
        "safe automatic quarantine of corrupt pipx metadata is unavailable on "
        f"this platform; metadata was left untouched at {metadata.path}"
    )


def _quarantine_pipx_metadata_owned_windows(
    metadata: _OwnedMetadataFile,
) -> _OwnedMetadataFile:
    parent_handle = _windows_open_installer_directory(metadata.path.parent)
    descriptor = -1
    try:
        descriptor = _windows_open_owned_metadata(
            metadata,
            parent_handle=parent_handle,
        )
        for _ in range(32):
            quarantine_name = f"{metadata.path.name}.corrupt-{uuid.uuid4().hex}"
            quarantine_path = metadata.path.with_name(quarantine_name)
            try:
                _windows_rename_open_file(
                    descriptor,
                    destination_name=quarantine_name,
                )
            except FileExistsError:
                continue
            quarantined = os.fstat(descriptor)
            return _OwnedMetadataFile(
                path=quarantine_path,
                device=quarantined.st_dev,
                inode=quarantined.st_ino,
                sha256=metadata.sha256,
            )
        raise InstallError("could not allocate a unique pipx metadata quarantine name")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _windows_close_handle(parent_handle)


def _remove_owned_quarantine(quarantine: _OwnedMetadataFile) -> None:
    if os.name == "nt" and not _installer_supports_dir_fd():
        _remove_owned_quarantine_windows(quarantine)
        return
    try:
        contents, metadata = _read_bounded_file_with_identity(
            quarantine.path,
            max_bytes=_MAX_METADATA_BYTES,
        )
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise InstallError(
            f"could not safely inspect pipx metadata quarantine: {exc}"
        ) from exc
    if (metadata.st_dev, metadata.st_ino) != (quarantine.device, quarantine.inode):
        raise InstallError("pipx metadata quarantine changed before cleanup")
    if hashlib.sha256(contents).hexdigest() != quarantine.sha256:
        raise InstallError("pipx metadata quarantine contents changed before cleanup")

    try:
        parent_descriptor = _open_installer_directory(quarantine.path.parent)
    except OSError as exc:
        raise InstallError(
            f"could not safely open pipx metadata quarantine directory: {exc}"
        ) from exc
    try:
        current = os.stat(
            quarantine.path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (quarantine.device, quarantine.inode):
            raise InstallError("pipx metadata quarantine changed before cleanup")
        os.unlink(quarantine.path.name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _remove_owned_quarantine_windows(quarantine: _OwnedMetadataFile) -> None:
    parent_handle = _windows_open_installer_directory(quarantine.path.parent)
    try:
        try:
            descriptor = _windows_open_owned_metadata(
                quarantine,
                parent_handle=parent_handle,
            )
        except FileNotFoundError:
            return
        try:
            _windows_delete_open_file(descriptor)
        finally:
            os.close(descriptor)
    finally:
        _windows_close_handle(parent_handle)


def _windows_open_installer_directory(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    file_read_attributes = 0x00000080
    share_read = 0x00000001
    share_write = 0x00000002
    share_delete = 0x00000004
    open_existing = 3
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        file_read_attributes,
        share_read | share_write | share_delete,
        None,
        open_existing,
        flag_backup_semantics | flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        error_number = int(get_last_error())
        raise InstallError(
            "could not safely open pipx metadata directory: "
            f"{format_error(error_number)}"
        )
    try:
        if _windows_handle_is_reparse(handle):
            raise InstallError("pipx metadata directory is a reparse point")
        return int(handle)
    except Exception:
        _windows_close_handle(int(handle))
        raise


def _windows_open_owned_metadata(
    metadata: _OwnedMetadataFile,
    *,
    parent_handle: int,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IoStatusUnion(ctypes.Union):
        _fields_ = [
            ("Status", ctypes.c_long),
            ("Pointer", ctypes.c_void_p),
        ]

    class IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("result",)
        _fields_ = [
            ("result", IoStatusUnion),
            ("Information", ctypes.c_size_t),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
    ntdll: Any = win_dll("ntdll")
    nt_open_file: Any = ntdll.NtOpenFile
    nt_open_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    nt_open_file.restype = ctypes.c_long

    file_read_data = 0x00000001
    file_read_attributes = 0x00000080
    delete_access = 0x00010000
    synchronize = 0x00100000
    share_read = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_non_directory_file = 0x00000040
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    missing_statuses = {0xC0000034, 0xC000003A}

    name_buffer = ctypes.create_unicode_buffer(metadata.path.name)
    name_length = len(metadata.path.name.encode("utf-16-le"))
    name = UnicodeString(
        Length=name_length,
        MaximumLength=name_length + ctypes.sizeof(ctypes.c_wchar),
        Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        Length=ctypes.sizeof(ObjectAttributes),
        RootDirectory=wintypes.HANDLE(parent_handle),
        ObjectName=ctypes.pointer(name),
        Attributes=obj_case_insensitive,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = IoStatusBlock()
    handle = wintypes.HANDLE()
    status = int(
        nt_open_file(
            ctypes.byref(handle),
            file_read_data
            | file_read_attributes
            | delete_access
            | synchronize,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            share_read,
            file_synchronous_io_nonalert
            | file_non_directory_file
            | file_open_reparse_point,
        )
    )
    if status < 0:
        status_code = status & 0xFFFFFFFF
        if status_code in missing_statuses:
            raise FileNotFoundError(
                2,
                "pipx metadata was not found",
                metadata.path,
            )
        raise InstallError(
            "could not safely open pipx metadata relative to its directory: "
            f"NTSTATUS 0x{status_code:08x}"
        )
    handle_value = handle.value
    if handle_value is None:
        raise InstallError("NtOpenFile returned an invalid pipx metadata handle")
    raw_handle = int(handle_value)
    descriptor = -1
    try:
        if _windows_handle_is_reparse(raw_handle):
            raise InstallError("pipx metadata is no longer a regular file")
        descriptor = int(
            open_osfhandle(
                raw_handle,
                os.O_RDONLY
                | int(getattr(os, "O_BINARY", 0))
                | int(getattr(os, "O_NOINHERIT", 0)),
            )
        )
        if descriptor < 0:
            raise OSError("could not adopt the pipx metadata handle")
        raw_handle = -1
        current = os.fstat(descriptor)
        if metadata.inode == 0 or current.st_ino == 0:
            raise InstallError("pipx metadata identity is unavailable")
        if (current.st_dev, current.st_ino) != (metadata.device, metadata.inode):
            raise InstallError("pipx metadata changed before it could be quarantined")
        if not stat.S_ISREG(current.st_mode):
            raise InstallError("pipx metadata is no longer a regular file")
        contents = _read_bounded_descriptor(
            descriptor,
            max_bytes=_MAX_METADATA_BYTES,
            path=metadata.path,
        )
        if hashlib.sha256(contents).hexdigest() != metadata.sha256:
            raise InstallError("pipx metadata contents changed before quarantine")
        return descriptor
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        elif raw_handle >= 0:
            _windows_close_handle(raw_handle)
        raise


def _windows_handle_is_reparse(handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    get_info: Any = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL
    info = FileAttributeTagInfo()
    if not get_info(
        wintypes.HANDLE(handle),
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_number = int(get_last_error())
        raise InstallError(
            "could not inspect pipx metadata handle: "
            f"{format_error(error_number)}"
        )
    return bool(int(info.FileAttributes) & 0x00000400)


def _windows_rename_open_file(
    descriptor: int,
    *,
    destination_name: str,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_wchar * (len(destination_name) + 1)),
        ]

    info = FileRenameInfo()
    info.ReplaceIfExists = 0
    # A simple name renames the already-open file within its current directory.
    # Windows requires RootDirectory to be NULL for that form.
    info.RootDirectory = None
    info.FileNameLength = len(destination_name.encode("utf-16-le"))
    info.FileName = destination_name
    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    set_info: Any = kernel32.SetFileInformationByHandle
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_info.restype = wintypes.BOOL
    handle = int(get_osfhandle(descriptor))
    if not set_info(
        wintypes.HANDLE(handle),
        3,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_number = int(get_last_error())
        if error_number in {80, 183}:
            raise FileExistsError(
                error_number,
                format_error(error_number),
                destination_name,
            )
        raise InstallError(
            "could not safely rename pipx metadata into quarantine: "
            f"{format_error(error_number)}"
        )


def _windows_delete_open_file(descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    info = FileDispositionInfo()
    info.DeleteFile = 1
    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    set_info: Any = kernel32.SetFileInformationByHandle
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_info.restype = wintypes.BOOL
    handle = int(get_osfhandle(descriptor))
    if not set_info(
        wintypes.HANDLE(handle),
        4,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_number = int(get_last_error())
        raise InstallError(
            "could not safely remove pipx metadata quarantine: "
            f"{format_error(error_number)}"
        )


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    close_handle: Any = kernel32.CloseHandle
    close_handle(wintypes.HANDLE(handle))


def _open_installer_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _installer_supports_dir_fd() -> bool:
    supported: set[Any] = set(getattr(os, "supports_dir_fd", ()))
    return all(function in supported for function in (os.open, os.stat, os.unlink))


def _write_installer_descriptor(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while preserving pipx metadata")
        view = view[written:]


def _read_bounded_descriptor(
    descriptor: int,
    *,
    max_bytes: int,
    path: Path,
) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    contents = bytearray()
    while len(contents) <= max_bytes:
        remaining = max_bytes + 1 - len(contents)
        chunk = os.read(descriptor, min(_CAPTURE_CHUNK_BYTES, remaining))
        if not chunk:
            break
        contents.extend(chunk)
    if len(contents) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} bytes: {path}")
    return bytes(contents)


def _unlink_installer_entry_if_same(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
        os.unlink(name, dir_fd=parent_descriptor)


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
    current = _read_uv_state(uv, runner=runner, environment=environment)
    if not current.installed:
        raise InstallError(
            "uv reported a successful install, but Ash is absent from the resulting uv state."
        )
    executable = current.executable
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
        launcher_directory = str(getattr(directory, "stdout", "")).strip()
        if not launcher_directory:
            raise InstallError(
                "Ash was installed, but uv did not report its executable directory."
            )
        executable_name = "ash.exe" if os.name == "nt" else "ash"
        executable = str(Path(launcher_directory) / executable_name)
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
                primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
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
                primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
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
    windows = plan.is_windows if plan is not None else os.name == "nt"
    if windows:
        if plan is None or plan.taskkill_path is None:
            raise InstallError(
                "managed Windows process-tree cleanup requires successful preflight"
            )
        if process.poll() is not None:
            raise InstallError(
                "managed root already exited; descendant cleanup is unconfirmed"
            )
        failure: str | None = None
        try:
            completed = subprocess.run(
                [plan.taskkill_path, "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            failure = "taskkill timed out"
        except OSError as exc:
            failure = f"taskkill could not execute ({type(exc).__name__})"
        else:
            if completed.returncode != 0:
                failure = f"taskkill exited with status {completed.returncode}"
        if failure is not None:
            _best_effort_root_kill(process)
            raise InstallError(
                f"Windows descendant cleanup could not be confirmed: {failure}"
            )
        try:
            process.wait(timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _best_effort_root_kill(process)
            raise InstallError(
                "Windows descendant cleanup could not be confirmed: "
                "managed root did not exit"
            )
        except OSError as exc:
            _best_effort_root_kill(process)
            raise InstallError(
                "Windows descendant cleanup could not be confirmed: "
                "managed root could not be reaped"
            ) from exc
        return

    if process.poll() is not None:
        return
    options = plan.spawn_options if plan is not None else {"start_new_session": True}
    if not options.get("start_new_session") or not _signal_process_group(
        process.pid, signal.SIGTERM
    ):
        process.terminate()
    try:
        process.wait(timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if (
            not options.get("start_new_session")
            or _SIGKILL is None
            or not _signal_process_group(process.pid, _SIGKILL)
        ):
            process.kill()
        try:
            process.wait(timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
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


def _best_effort_root_kill(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=_WINDOWS_TASKKILL_TIMEOUT_SECONDS)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


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
