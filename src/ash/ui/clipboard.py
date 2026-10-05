"""Explicit, bounded host clipboard integration for terminal commands."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ash.safety.environment import build_scrubbed_environment, resolve_host_executable


MAX_CLIPBOARD_BYTES = 1_000_000
_CLIPBOARD_ENV_ALLOWLIST = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
)


class ClipboardUnavailable(RuntimeError):
    """Raised when no safe host clipboard backend can accept the payload."""


def copy_to_clipboard(text: str, *, workspace_root: str | Path) -> str:
    """Copy bounded text through a known host clipboard helper."""

    if not isinstance(text, str):
        raise TypeError("clipboard text must be a string")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("clipboard text must be valid UTF-8") from exc
    if size > MAX_CLIPBOARD_BYTES:
        raise ValueError(
            f"clipboard text exceeds {MAX_CLIPBOARD_BYTES} UTF-8 bytes; use /export instead"
        )

    workspace = Path(workspace_root).expanduser().resolve()
    environment = build_scrubbed_environment(_CLIPBOARD_ENV_ALLOWLIST)
    backends: tuple[tuple[str, tuple[str, ...]], ...]
    if sys.platform == "darwin":
        backends = (("pbcopy", ()),)
    else:
        backends = (
            ("wl-copy", ()),
            ("xclip", ("-selection", "clipboard")),
            ("xsel", ("--clipboard", "--input")),
        )

    for command, arguments in backends:
        executable = resolve_host_executable(
            command,
            workspace_root=workspace,
            cwd=workspace,
            search_path=environment.get("PATH"),
        )
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, *arguments],
                input=text,
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
                env=environment,
                cwd=workspace,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return command

    raise ClipboardUnavailable(
        "no usable host clipboard helper was found; install wl-clipboard on Wayland, "
        "xclip/xsel on X11, or use /export"
    )
