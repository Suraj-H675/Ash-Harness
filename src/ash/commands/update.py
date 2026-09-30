"""Explicit, telemetry-free release update checks."""

from __future__ import annotations

import base64
import importlib.metadata
import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

from packaging.version import InvalidVersion, Version

from ash.install import GITHUB_API_VERSION, pipx_install_command
from ash.installer import InstallError, InstallResult, install as install_ash
from ash.safe_io import strict_json_loads


LATEST_RELEASE_API = (
    "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases/latest"
)
PROJECT_METADATA_API = (
    "https://api.github.com/repos/Suraj-H675/Ash-Harness/contents/pyproject.toml"
)
MAX_RESPONSE_BYTES = 1_000_000


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
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    try:
        with opener(request, timeout=5) as response:
            _require_expected_github_api_response(
                response,
                expected_url=request.full_url,
                label="release check",
            )
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
        payload = strict_json_loads(raw)
        tag = payload["tag_name"]
        release_url = payload["html_url"]
        immutable = payload["immutable"]
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("GitHub returned an invalid release response") from exc
    if (
        not isinstance(tag, str)
        or not isinstance(release_url, str)
        or not isinstance(immutable, bool)
    ):
        raise ValueError("GitHub returned an invalid release response")
    if not immutable:
        raise ValueError(
            "Latest Ash release is mutable; refusing to update from an unlocked tag"
        )

    latest = _version_from_tag(tag)
    try:
        installed = Version(current)
    except InvalidVersion as exc:
        raise ValueError(f"Installed Ash version is invalid: {current!r}") from exc
    published = _published_package_version(
        tag,
        current_version=current,
        opener=opener,
    )
    if published != latest:
        raise ValueError(
            "Latest Ash release tag version "
            f"{latest} does not match packaged project version {published}; "
            "refusing to trust the release"
        )
    return UpdateStatus(
        current_version=current,
        latest_version=str(latest),
        release_tag=tag,
        update_available=latest > installed,
        release_url=release_url,
    )


def _published_package_version(
    tag: str,
    *,
    current_version: str,
    opener: Callable[..., Any],
) -> Version:
    query = urllib.parse.urlencode({"ref": tag})
    request = urllib.request.Request(
        f"{PROJECT_METADATA_API}?{query}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ash/{current_version}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    try:
        with opener(request, timeout=5) as response:
            _require_expected_github_api_response(
                response,
                expected_url=request.full_url,
                label="package metadata check",
            )
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise ValueError(
            f"Could not verify Ash release package metadata: {exc}"
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ash release package metadata response exceeded 1 MB")
    try:
        payload = strict_json_loads(raw)
        encoding = payload["encoding"]
        content = payload["content"]
        if encoding != "base64" or not isinstance(content, str):
            raise ValueError("unexpected package metadata encoding")
        decoded = base64.b64decode("".join(content.split()), validate=True)
        project = tomllib.loads(decoded.decode("utf-8"))["project"]
        name = project["name"]
        version = project["version"]
    except (
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
    ) as exc:
        raise ValueError("GitHub returned invalid Ash package metadata") from exc
    if name != "ash-ai" or not isinstance(version, str):
        raise ValueError("GitHub returned invalid Ash package metadata")
    try:
        return Version(version)
    except InvalidVersion as exc:
        raise ValueError(
            f"Published Ash package version is invalid: {version!r}"
        ) from exc


def _require_expected_github_api_response(
    response: Any,
    *,
    expected_url: str,
    label: str,
) -> None:
    """Reject silent urllib redirects away from the exact GitHub API request."""

    geturl = getattr(response, "geturl", None)
    if not callable(geturl):
        return
    final_url = str(geturl())
    expected = urllib.parse.urlsplit(expected_url)
    final = urllib.parse.urlsplit(final_url)
    if (
        final.scheme != "https"
        or final.hostname != "api.github.com"
        or final.username is not None
        or final.password is not None
        or final.port not in {None, 443}
        or final.path != expected.path
        or final.query != expected.query
        or final.fragment
    ):
        raise ValueError(
            f"GitHub {label} redirected to an unexpected endpoint"
        )


def apply_update(
    status: UpdateStatus,
    *,
    installer: Callable[..., InstallResult] = install_ash,
    json_output: bool = False,
) -> int:
    """Install the exact published release selected by ``check_for_update``."""

    if not status.update_available:
        if json_output:
            print(json.dumps({**status.as_dict(), "updated": False}, sort_keys=True))
        else:
            print(render_update_status(status))
        return 0

    try:
        result = installer(ref=status.release_tag)
    except InstallError as exc:
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        raise ValueError(f"Ash installer could not start: {exc}") from exc
    installed_version = _version_from_installer_result(result.version)
    expected_version = Version(status.latest_version)
    if installed_version != expected_version:
        raise ValueError(
            "Ash installer completed but the installed version "
            f"{installed_version} does not match expected {expected_version}"
        )
    if json_output:
        print(
            json.dumps(
                {
                    **status.as_dict(),
                    "updated": True,
                    "manager": result.manager,
                    "executable": result.executable,
                    "version": result.version,
                    "shell_restart_required": result.shell_restart_required,
                },
                sort_keys=True,
            )
        )
        return 0
    print(
        f"Ash updated successfully ({result.version}) via {result.manager}. "
        "Run `ash doctor --connect` to verify provider connectivity."
    )
    return 0


def render_update_status(status: UpdateStatus, *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(status.as_dict(), sort_keys=True)
    if status.update_available:
        return "\n".join(
            (
                f"Ash {status.latest_version} is available (installed: {status.current_version}).",
                f"Release: {status.release_url}",
                "Upgrade: " + pipx_install_command(ref=status.release_tag),
            )
        )
    return f"Ash {status.current_version} is up to date."


def _installed_version() -> str:
    try:
        return importlib.metadata.version("ash-ai")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _version_from_installer_result(value: str) -> Version:
    rendered = value.strip()
    prefix = "ash "
    if not rendered.casefold().startswith(prefix):
        raise ValueError(
            f"Ash installer returned an invalid version response: {value!r}"
        )
    candidate = rendered[len(prefix) :].strip()
    try:
        return Version(candidate)
    except InvalidVersion as exc:
        raise ValueError(
            f"Ash installer returned an invalid version response: {value!r}"
        ) from exc


def _version_from_tag(tag: str) -> Version:
    value = tag.strip()
    prefix = "ash-v"
    if not value.startswith(prefix):
        raise ValueError(
            "Latest Ash release tag does not use the required "
            f"{prefix!r} prefix: {tag!r}"
        )
    value = value[len(prefix) :]
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise ValueError(
            f"Latest Ash release tag is not a valid version: {tag!r}"
        ) from exc
