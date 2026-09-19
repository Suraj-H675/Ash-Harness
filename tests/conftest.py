from __future__ import annotations

import errno
import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

import pytest


_ORIGINAL_ENV: dict[str, str | None] = {}
_TEST_HOME: Path | None = None
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def _remember_and_set(name: str, value: str) -> None:
    if name not in _ORIGINAL_ENV:
        _ORIGINAL_ENV[name] = os.environ.get(name)
    os.environ[name] = value


def _host_is_local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if not isinstance(host, str):
        return False
    normalized = host.strip().strip("[]").casefold()
    if not normalized or normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any):
    if not _host_is_local(host):
        raise RuntimeError(f"live network disabled in tests: {host!r}")
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_connect(sock: socket.socket, address: object) -> None:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        host = address[0] if isinstance(address, tuple) and address else None
        if not _host_is_local(host):
            raise RuntimeError(f"live network disabled in tests: {host!r}")
    _ORIGINAL_CONNECT(sock, address)  # type: ignore[arg-type]


def _guarded_connect_ex(sock: socket.socket, address: object) -> int:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        host = address[0] if isinstance(address, tuple) and address else None
        if not _host_is_local(host):
            return errno.ENETUNREACH
    return _ORIGINAL_CONNECT_EX(sock, address)  # type: ignore[arg-type]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Establish deterministic home/network isolation before test collection."""

    global _TEST_HOME
    _TEST_HOME = Path(tempfile.mkdtemp(prefix="ash-pytest-home-")).resolve()
    config_home = _TEST_HOME / ".config"
    data_home = _TEST_HOME / ".local" / "share"
    cache_home = _TEST_HOME / ".cache"
    state_home = _TEST_HOME / ".local" / "state"
    appdata = _TEST_HOME / "AppData" / "Roaming"
    local_appdata = _TEST_HOME / "AppData" / "Local"
    for directory in (
        config_home,
        data_home,
        cache_home,
        state_home,
        appdata,
        local_appdata,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    git_config = _TEST_HOME / "gitconfig"
    git_config.write_text("", encoding="utf-8")
    for name, value in {
        "HOME": _TEST_HOME,
        "USERPROFILE": _TEST_HOME,
        "XDG_CONFIG_HOME": config_home,
        "XDG_DATA_HOME": data_home,
        "XDG_CACHE_HOME": cache_home,
        "XDG_STATE_HOME": state_home,
        "APPDATA": appdata,
        "LOCALAPPDATA": local_appdata,
        "GIT_CONFIG_GLOBAL": git_config,
    }.items():
        _remember_and_set(name, str(value))

    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Restore process-global state after the suite completes."""

    socket.getaddrinfo = _ORIGINAL_GETADDRINFO  # type: ignore[assignment]
    socket.socket.connect = _ORIGINAL_CONNECT  # type: ignore[method-assign]
    socket.socket.connect_ex = _ORIGINAL_CONNECT_EX  # type: ignore[method-assign]
    for name, previous in _ORIGINAL_ENV.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
    if _TEST_HOME is not None:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
