"""Explicit, telemetry-free release update checks."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

from packaging.version import InvalidVersion, Version

from ash.install import REPOSITORY_URL, pipx_install_command


LATEST_RELEASE_API = (
    "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases/latest"
)
MAX_RESPONSE_BYTES = 1_000_000
PACKAGE_SPEC = f"ash-ai @ git+{REPOSITORY_URL}"


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    release_tag: str
    update_available: bool
    release_url: str

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)


def check_for_update(
    *,
    current_version: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> UpdateStatus:
    """Query Ash's latest published GitHub release and compare versions."""

    current = current_version or _installed_version()
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ash/{current}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("No published Ash release is available yet") from exc
        raise ValueError(f"GitHub release check failed with HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise ValueError(f"Could not check for updates: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("GitHub release response exceeded 1 MB")
    try:
        payload = json.loads(raw)
        tag = payload["tag_name"]
        release_url = payload["html_url"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("GitHub returned an invalid release response") from exc
    if not isinstance(tag, str) or not isinstance(release_url, str):
        raise ValueError("GitHub returned an invalid release response")

    latest = _version_from_tag(tag)
    try:
        installed = Version(current)
    except InvalidVersion as exc:
        raise ValueError(f"Installed Ash version is invalid: {current!r}") from exc
    return UpdateStatus(
        current_version=current,
        latest_version=str(latest),
        release_tag=tag,
        update_available=latest > installed,
        release_url=release_url,
    )


def apply_update(*, runner: Callable[..., Any] = subprocess.run) -> int:
    """Update an installed Ash checkout through pipx without shell parsing.

    This path is useful when Ash is already installed but its pipx environment
    needs to be rebuilt.  It intentionally installs the repository default
    branch rather than depending on a published release existing yet.
    """

    pipx = shutil.which("pipx")
    if pipx is None:
        raise ValueError(
            "pipx is not installed. Install pipx first, then run `ash update --apply`."
        )

    environment = os.environ.copy()
    environment["UV_VENV_CLEAR"] = "1"
    command = [pipx, "install", "--force", PACKAGE_SPEC]
    try:
        result = runner(command, env=environment, check=False)
    except OSError as exc:
        raise ValueError(f"Could not start pipx: {exc}") from exc

    return_code = int(getattr(result, "returncode", 1))
    if return_code != 0:
        raise ValueError(
            "Ash update failed. Retry with: "
            f"`{pipx_install_command()}`"
        )
    print("Ash updated successfully. Run `ash doctor --connect` to verify it.")
    return 0


def render_update_status(status: UpdateStatus, *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(status.as_dict(), sort_keys=True)
    if status.update_available:
        return "\n".join(
            (
                f"Ash {status.latest_version} is available (installed: {status.current_version}).",
                f"Release: {status.release_url}",
                "Upgrade: "
                + pipx_install_command(ref=status.release_tag),
            )
        )
    return f"Ash {status.current_version} is up to date."


def _installed_version() -> str:
    try:
        return importlib.metadata.version("ash-ai")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _version_from_tag(tag: str) -> Version:
    value = tag.strip()
    if value.casefold().startswith("ash-"):
        value = value[4:]
    if value.casefold().startswith("v"):
        value = value[1:]
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise ValueError(
            f"Latest Ash release tag is not a valid version: {tag!r}"
        ) from exc
