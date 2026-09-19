from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

import pytest


def test_suite_uses_synthetic_home() -> None:
    home = Path(os.environ["HOME"]).resolve()

    assert home.name.startswith("ash-pytest-home-")
    assert Path.home().resolve() == home
    assert Path(os.environ["XDG_CONFIG_HOME"]).is_relative_to(home)
    assert Path(os.environ["XDG_CACHE_HOME"]).is_relative_to(home)
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]).is_relative_to(home)


def test_suite_blocks_external_dns_but_allows_loopback() -> None:
    with pytest.raises(RuntimeError, match="live network disabled"):
        socket.getaddrinfo("example.com", 443)

    resolved = socket.getaddrinfo("127.0.0.1", 0, type=socket.SOCK_STREAM)
    assert resolved


def test_suite_blocks_external_connect_ex() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        assert probe.connect_ex(("203.0.113.1", 9)) == errno.ENETUNREACH


def test_suite_blocks_external_connect() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        with pytest.raises(RuntimeError, match="live network disabled"):
            probe.connect(("203.0.113.1", 9))
