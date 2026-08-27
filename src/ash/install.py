"""Canonical installation commands used by runtime remediation messages."""

from __future__ import annotations

import os
import shlex


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"


def pipx_install_command(*extras: str, ref: str | None = None) -> str:
    """Return a self-healing pipx install command.

    Newer pipx releases may use uv as their virtual-environment backend.  In
    that mode ``pipx install --force`` can fail when the target environment
    already exists unless uv is told to clear it.  The setting is harmless for
    fresh installs and pip-backed environments, so keep it in every generated
    remediation command.
    """

    normalized = sorted({extra.strip() for extra in extras if extra.strip()})
    suffix = f"[{','.join(normalized)}]" if normalized else ""
    revision = f"@{ref}" if ref else ""
    package_spec = f"ash-ai{suffix} @ git+{REPOSITORY_URL}{revision}"
    quoted_spec = shlex.quote(package_spec)
    if os.name == "nt":
        return f"$env:UV_VENV_CLEAR='1'; pipx install --force {quoted_spec}"
    return f"UV_VENV_CLEAR=1 pipx install --force {quoted_spec}"
