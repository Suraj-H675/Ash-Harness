"""Managed subprocess boundary for Playwright setup and diagnostics."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, overload

from ash.safety.environment import build_scrubbed_environment
from ash.sandbox.process_utils import (
    ProcessTreeError,
    prepare_process_tree,
    terminate_process_tree_sync,
)


_BROWSER_ENV_ALLOWLIST = (
    "PLAYWRIGHT_BROWSERS_PATH",
    "PLAYWRIGHT_NODEJS_PATH",
    "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
    "PLAYWRIGHT_DOWNLOAD_HOST",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)


def browser_subprocess_environment() -> dict[str, str]:
    """Return only operational environment needed by Playwright child tools."""

    return build_scrubbed_environment(_BROWSER_ENV_ALLOWLIST)


@overload
def run_browser_subprocess(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
    capture_output: bool = False,
    text: Literal[True],
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_browser_subprocess(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
    capture_output: bool = False,
    text: Literal[False] = False,
) -> subprocess.CompletedProcess[bytes]: ...


def run_browser_subprocess(
    command: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a browser helper with scrubbed env and descendant-tree cleanup."""

    plan = prepare_process_tree(workspace_root=Path.cwd())
    popen_kwargs: dict[str, object] = {
        "env": browser_subprocess_environment(),
        **plan.spawn_options,
    }
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    if text:
        popen_kwargs["text"] = True

    process: subprocess.Popen[Any] = subprocess.Popen(
        list(command), **popen_kwargs  # type: ignore[call-overload]
    )
    try:
        if capture_output:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            process.wait(timeout=timeout)
            stdout = None
            stderr = None
    except subprocess.TimeoutExpired as exc:
        try:
            terminate_process_tree_sync(process, plan=plan, timeout_seconds=1.0)
        except ProcessTreeError as cleanup_error:
            exc.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        raise
    except BaseException as primary:
        try:
            terminate_process_tree_sync(process, plan=plan, timeout_seconds=1.0)
        except ProcessTreeError as cleanup_error:
            primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        raise

    completed = subprocess.CompletedProcess(
        list(command),
        process.returncode if process.returncode is not None else -1,
        stdout,
        stderr,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            list(command),
            output=stdout,
            stderr=stderr,
        )
    return completed
