"""Canonical public installation commands used by remediation messages."""

from __future__ import annotations

import base64
import re
import shlex
import sys
import urllib.parse
import zlib


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"
RELEASES_API = "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases"
LATEST_RELEASE_API = f"{RELEASES_API}/latest"
PUBLIC_BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/Suraj-H675/Ash-Harness/main/install.sh"
)
INSTALLER_ASSET_NAME = "install-ash.py"
GITHUB_API_VERSION = "2026-03-10"
_RELEASE_REF_PATTERN = re.compile(r"^ash-v\d+(?:\.\d+)+(?:[0-9A-Za-z._+-]*)$")
_INSTALLER_MAX_BYTES = 1024 * 1024
_RELEASE_METADATA_MAX_BYTES = 1024 * 1024
_BOOTSTRAP_CODE = f'''\
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_URL = sys.argv.pop(1)
MAX_INSTALLER_BYTES = {_INSTALLER_MAX_BYTES}
MAX_METADATA_BYTES = {_RELEASE_METADATA_MAX_BYTES}
ASSET_NAME = {INSTALLER_ASSET_NAME!r}
API_HOST = "api.github.com"
ASSET_HOST = "release-assets.githubusercontent.com"
ASSET_API_PREFIX = "/repos/Suraj-H675/Ash-Harness/releases/assets/"


def fail(message):
    raise SystemExit("Ash installer bootstrap failed: " + message)


def strict_object(pairs):
    result = {{}}
    for key, value in pairs:
        if key in result:
            fail("release metadata contained duplicate JSON keys")
        result[key] = value
    return result


def reject_constant(raw):
    fail("release metadata contained invalid JSON constant: " + raw)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {{API_HOST, ASSET_HOST}}:
            fail("release download redirected to an untrusted host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(request, limit, expected_url=None):
    try:
        with opener.open(request, timeout=30) as response:
            if expected_url is not None:
                geturl = getattr(response, "geturl", None)
                if callable(geturl):
                    final_url = str(geturl())
                    final = urllib.parse.urlsplit(final_url)
                    expected = urllib.parse.urlsplit(expected_url)
                    if (
                        final.scheme != "https"
                        or final.hostname != API_HOST
                        or final.username is not None
                        or final.password is not None
                        or final.port not in (None, 443)
                        or final.path != expected.path
                        or final.query != expected.query
                        or final.fragment
                    ):
                        fail("release metadata redirected to an unexpected endpoint")
            payload = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        fail("could not fetch " + request.full_url + ": " + str(exc))
    if len(payload) > limit:
        fail("download exceeded the configured size limit")
    return payload


api = urllib.parse.urlsplit(API_URL)
if api.scheme != "https" or api.hostname != API_HOST:
    fail("release metadata URL is not the official GitHub API")

opener = urllib.request.build_opener(SafeRedirect())
metadata_request = urllib.request.Request(
    API_URL,
    headers={{
        "Accept": "application/vnd.github+json",
        "User-Agent": "ash-installer-bootstrap",
        "X-GitHub-Api-Version": {GITHUB_API_VERSION!r},
    }},
)
metadata_raw = fetch(metadata_request, MAX_METADATA_BYTES, expected_url=API_URL)
try:
    release = json.loads(
        metadata_raw.decode("utf-8"),
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )
except (UnicodeError, json.JSONDecodeError) as exc:
    fail("release metadata was not valid JSON: " + str(exc))
if not isinstance(release, dict):
    fail("release metadata had an unexpected shape")
if release.get("immutable") is not True:
    fail("latest Ash release is not immutable")
tag = release.get("tag_name")
if not isinstance(tag, str) or not re.fullmatch({_RELEASE_REF_PATTERN.pattern!r}, tag):
    fail("release tag was missing or invalid")
assets = release.get("assets")
if not isinstance(assets, list):
    fail("release assets were missing")
matches = [
    asset
    for asset in assets
    if isinstance(asset, dict)
    and asset.get("name") == ASSET_NAME
    and asset.get("state") == "uploaded"
]
if len(matches) != 1:
    fail("release must contain exactly one uploaded " + ASSET_NAME + " asset")
asset = matches[0]
asset_url = asset.get("url")
asset_parts = urllib.parse.urlsplit(asset_url if isinstance(asset_url, str) else "")
if (
    asset_parts.scheme != "https"
    or asset_parts.hostname != API_HOST
    or not asset_parts.path.startswith(ASSET_API_PREFIX)
):
    fail("installer asset URL was not the expected GitHub API endpoint")
digest = asset.get("digest")
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{{64}}", digest):
    fail("installer asset is missing its SHA-256 digest")
size = asset.get("size")
if not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > MAX_INSTALLER_BYTES:
    fail("installer asset size is invalid")
asset_request = urllib.request.Request(
    asset_url,
    headers={{
        "Accept": "application/octet-stream",
        "User-Agent": "ash-installer-bootstrap",
        "X-GitHub-Api-Version": {GITHUB_API_VERSION!r},
    }},
)
raw = fetch(asset_request, MAX_INSTALLER_BYTES)
if len(raw) != size:
    fail("installer asset size did not match release metadata")
actual_digest = hashlib.sha256(raw).hexdigest()
if actual_digest != digest.removeprefix("sha256:"):
    fail("installer asset SHA-256 digest did not match release metadata")
if not any(arg == "--ref" or arg.startswith("--ref=") for arg in sys.argv[1:]):
    sys.argv.extend(("--ref", tag))
sys.argv[0] = ASSET_NAME
exec(compile(raw, ASSET_NAME, "exec"))
'''
_BOOTSTRAP_PAYLOAD = base64.b64encode(
    zlib.compress(_BOOTSTRAP_CODE.encode("utf-8"), level=9)
).decode("ascii")
_BOOTSTRAP_LAUNCHER = (
    "import base64,zlib;"
    f"exec(compile(zlib.decompress(base64.b64decode({_BOOTSTRAP_PAYLOAD!r})),"
    "'<ash-bootstrap>','exec'))"
)


def _release_api_url(ref: str | None) -> str:
    if not ref:
        return LATEST_RELEASE_API
    if _RELEASE_REF_PATTERN.fullmatch(ref) is None:
        raise ValueError(
            "Ash release refs must use the canonical ash-v<version> form"
        )
    return f"{RELEASES_API}/tags/{urllib.parse.quote(ref, safe='')}"


def install_command(*extras: str, ref: str | None = None) -> str:
    """Return the Ash-owned, release-verified bootstrap command."""

    normalized = sorted({extra.strip() for extra in extras if extra.strip()})
    arguments = [part for extra in normalized for part in ("--extra", extra)]
    if ref is None:
        command = (
            "curl -fsSL --proto '=https' --tlsv1.2 "
            f"{PUBLIC_BOOTSTRAP_URL} | sh"
        )
        if arguments:
            command += " -s -- " + " ".join(shlex.quote(value) for value in arguments)
        return command
    if ref:
        arguments.extend(("--ref", ref))
    suffix = (
        " " + " ".join(shlex.quote(value) for value in arguments) if arguments else ""
    )
    return (
        f"{shlex.quote(sys.executable)} -I -c {shlex.quote(_BOOTSTRAP_LAUNCHER)} "
        f"{shlex.quote(_release_api_url(ref))}{suffix}"
    )


def pipx_install_command(*extras: str, ref: str | None = None) -> str:
    """Compatibility alias for callers that previously exposed raw pipx."""

    return install_command(*extras, ref=ref)
