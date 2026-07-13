"""Canonical installation commands used by runtime remediation messages."""

from __future__ import annotations


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"


def pipx_install_command(*extras: str) -> str:
    """Return the current one-command Git installation for capability extras."""

    normalized = sorted({extra.strip() for extra in extras if extra.strip()})
    suffix = f"[{','.join(normalized)}]" if normalized else ""
    return f"pipx install --force 'ash-ai{suffix} @ git+{REPOSITORY_URL}'"
