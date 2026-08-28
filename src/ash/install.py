"""Canonical public installation commands used by remediation messages."""

from __future__ import annotations

import os
import shlex


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"
INSTALLER_URL = (
    "https://raw.githubusercontent.com/Suraj-H675/Ash-Harness/main/src/ash/installer.py"
)


def install_command(*extras: str, ref: str | None = None) -> str:
    """Return the Ash-owned cross-platform bootstrap command.

    Package-manager selection and repair details live in the downloaded
    installer, keeping generated user guidance stable as pipx and uv evolve.
    """

    normalized = sorted({extra.strip() for extra in extras if extra.strip()})
    arguments = [part for extra in normalized for part in ("--extra", extra)]
    if ref:
        arguments.extend(("--ref", ref))
    suffix = (
        " " + " ".join(shlex.quote(value) for value in arguments) if arguments else ""
    )
    if os.name == "nt":
        return f"irm {INSTALLER_URL} | py -{suffix}"
    return f"curl -fsSL {INSTALLER_URL} | python3 -{suffix}"


def pipx_install_command(*extras: str, ref: str | None = None) -> str:
    """Compatibility alias for callers that previously exposed raw pipx."""

    return install_command(*extras, ref=ref)
