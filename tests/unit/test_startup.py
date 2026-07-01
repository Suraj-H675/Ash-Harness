from __future__ import annotations

import subprocess
import sys
import time


def test_lightweight_cli_import_does_not_load_runtime_stack() -> None:
    script = """
import sys
import ash.cli
blocked = {'openai', 'anthropic', 'core.loop', 'ui.terminal'} & set(sys.modules)
assert not blocked, blocked
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_version_command_has_bounded_startup_time() -> None:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "ash", "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert result.stdout.startswith("ash ")
    assert elapsed < 1.0, f"version startup took {elapsed:.3f}s"


def test_public_sdk_exports_remain_lazy_and_compatible() -> None:
    script = """
import sys
import ash
assert 'ash.sdk' not in sys.modules
assert ash.AshClient.__name__ == 'AshClient'
assert 'ash.sdk' in sys.modules
assert ash.tools.__name__ == 'tools'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_direct_legacy_submodule_import_remains_compatible() -> None:
    script = """
import ash.tools.command
import tools.command
assert ash.tools.command is tools.command
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
