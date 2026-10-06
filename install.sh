#!/bin/sh
set -eu

uv_version="0.12.17"
uv_installer="https://astral.sh/uv/$uv_version/install.sh"

case "$(uname -s 2>/dev/null || true)" in
  Linux|Darwin) ;;
  *)
    printf '%s\n' "Ash supports Linux and macOS. On Windows, use WSL2." >&2
    exit 1
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' "Ash installation requires curl." >&2
  exit 1
fi

if [ -z "${HOME:-}" ]; then
  printf '%s\n' "Ash installation requires HOME to be set." >&2
  exit 1
fi

python_cmd=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c '
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
' >/dev/null 2>&1; then
    python_cmd="$(command -v "$candidate")"
    break
  fi
done

uv_cmd="$(command -v uv 2>/dev/null || true)"
pipx_cmd="$(command -v pipx 2>/dev/null || true)"

if [ -z "$python_cmd" ] || { [ -z "$uv_cmd" ] && [ -z "$pipx_cmd" ]; }; then
  printf '%s\n' "Preparing Ash's runtime..."
  curl --proto '=https' --tlsv1.2 -LsSf "$uv_installer" | UV_INSTALL_DIR="$HOME/.local/bin" sh
  uv_cmd="$(command -v uv 2>/dev/null || true)"
  if [ -z "$uv_cmd" ] && [ -x "${HOME:-}/.local/bin/uv" ]; then
    uv_cmd="${HOME}/.local/bin/uv"
  fi
  if [ -z "$uv_cmd" ]; then
    printf '%s\n' "Ash could not bootstrap uv." >&2
    exit 1
  fi
  uv_dir=$(dirname "$uv_cmd")
  PATH="$uv_dir:$PATH"
  export PATH
fi

run_bootstrap() {
  if [ -n "$python_cmd" ]; then
    "$python_cmd" -I - "$@"
  else
    "$uv_cmd" run --python 3.12 --no-project python -I - "$@"
  fi
}

run_bootstrap "$@" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn


REPOSITORY = "Suraj-H675/Ash-Harness"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
API_VERSION = "2026-03-10"
MAX_METADATA_BYTES = 1024 * 1024
MAX_INSTALLER_BYTES = 4 * 1024 * 1024
TAG_PATTERN = re.compile(r"^ash-v\d+(?:\.\d+)+(?:[0-9A-Za-z._+-]*)$")


def strict_json_loads(raw: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def fail(message: str) -> NoReturn:
    raise SystemExit(f"Ash installation could not continue: {message}")


headers = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "ash-bootstrap",
    "X-GitHub-Api-Version": API_VERSION,
}
request = urllib.request.Request(RELEASE_API, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlsplit(str(response.geturl()))
        expected = urllib.parse.urlsplit(RELEASE_API)
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
            fail("GitHub release metadata redirected to an unexpected endpoint.")
        raw_metadata = response.read(MAX_METADATA_BYTES + 1)
except urllib.error.HTTPError as exc:
    fail(f"GitHub returned HTTP {exc.code} while resolving the latest release.")
except (OSError, TimeoutError, urllib.error.URLError) as exc:
    fail(f"could not resolve the latest Ash release ({exc}).")

if len(raw_metadata) > MAX_METADATA_BYTES:
    fail("GitHub release metadata exceeded 1 MiB.")
try:
    payload = strict_json_loads(raw_metadata)
except (UnicodeError, json.JSONDecodeError, ValueError):
    fail("GitHub returned invalid Ash release metadata.")
if not isinstance(payload, dict):
    fail("GitHub returned invalid Ash release metadata.")

tag = payload.get("tag_name")
if not isinstance(tag, str) or TAG_PATTERN.fullmatch(tag) is None:
    fail("the latest GitHub release has an invalid Ash tag.")
if payload.get("immutable") is not True:
    fail(f"Ash release {tag} is not immutable.")
assets = payload.get("assets")
if not isinstance(assets, list):
    fail(f"Ash release {tag} has no release assets.")

matches = [
    asset
    for asset in assets
    if isinstance(asset, dict)
    and asset.get("state") == "uploaded"
    and asset.get("name") == "install-ash.py"
]
if len(matches) != 1:
    fail(f"Ash release {tag} must contain exactly one install-ash.py asset.")
asset = matches[0]
url = asset.get("browser_download_url")
digest = asset.get("digest")
size = asset.get("size")
if (
    not isinstance(url, str)
    or not isinstance(digest, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    or not isinstance(size, int)
    or isinstance(size, bool)
    or size < 1
    or size > MAX_INSTALLER_BYTES
):
    fail(f"Ash release {tag} has invalid installer metadata.")

parsed = urllib.parse.urlsplit(url)
parts = parsed.path.split("/")
if (
    parsed.scheme != "https"
    or parsed.hostname != "github.com"
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or len(parts) != 7
    or parts[1:5] != ["Suraj-H675", "Ash-Harness", "releases", "download"]
    or urllib.parse.unquote(parts[5]) != tag
    or urllib.parse.unquote(parts[6]) != "install-ash.py"
):
    fail(f"Ash release {tag} has an untrusted installer URL.")

with tempfile.TemporaryDirectory(prefix="ash-bootstrap-") as raw_directory:
    target = Path(raw_directory) / "install-ash.py"
    installer_request = urllib.request.Request(
        url,
        headers={"User-Agent": "ash-bootstrap"},
    )
    sha256 = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(installer_request, timeout=30) as response:
            final = urllib.parse.urlsplit(str(response.geturl()))
            if (
                final.scheme != "https"
                or final.hostname not in {
                    "github.com",
                    "release-assets.githubusercontent.com",
                }
                or final.username is not None
                or final.password is not None
            ):
                fail(f"Ash release {tag} installer redirected to an untrusted host.")
            with target.open("xb") as handle:
                if os.name != "nt":
                    os.chmod(target, 0o600)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size or total > MAX_INSTALLER_BYTES:
                        fail(f"Ash release {tag} installer exceeded its declared size.")
                    sha256.update(chunk)
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        fail(f"GitHub returned HTTP {exc.code} while downloading the installer.")
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        fail(f"could not download the Ash installer ({exc}).")

    if total != size:
        fail(f"Ash release {tag} installer size did not match release metadata.")
    if sha256.hexdigest() != digest.removeprefix("sha256:"):
        fail(f"Ash release {tag} installer SHA-256 did not match release metadata.")

    command = [sys.executable, "-I", str(target), "--ref", tag, *sys.argv[1:]]
    raise SystemExit(subprocess.run(command, check=False).returncode)
PY
