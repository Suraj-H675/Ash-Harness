"""Dependency-free compatibility smoke for the standalone Python 3.10 installer."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import patch


def _load_installer():
    installer_path = Path(__file__).parents[2] / "src" / "ash" / "installer.py"
    spec = importlib.util.spec_from_file_location(
        "ash_standalone_installer_py310_smoke",
        installer_path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load the standalone installer module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    installer = _load_installer()

    class FakeProcess:
        def wait(self, timeout=None):
            raise KeyboardInterrupt("cancelled")

        def poll(self):
            return None

    with patch.object(installer.subprocess, "Popen", return_value=FakeProcess()), patch.object(
        installer,
        "_terminate_process",
        side_effect=installer.InstallError("cleanup failed"),
    ):
        try:
            installer._run_streaming(
                ["fake-installer"],
                runner=subprocess.run,
                environment={},
                timeout=1,
                description="installer command",
                failure_message="install failed",
            )
        except BaseException as exc:
            if not isinstance(exc, KeyboardInterrupt):
                raise AssertionError(
                    f"cleanup masked cancellation with {type(exc).__name__}"
                ) from exc
            if not isinstance(exc.__cause__, installer.InstallError):
                raise AssertionError("cleanup failure was not preserved") from exc
            if str(exc.__cause__) != "cleanup failed":
                raise AssertionError("unexpected cleanup failure detail") from exc
        else:
            raise AssertionError("expected cancellation from fake installer process")

    wheel_bytes = b"python-3.10-release-wheel-smoke\n"
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    ref = "ash-v0.2.0"
    wheel_name = "ash_ai-0.2.0-py3-none-any.whl"
    download_url = (
        "https://github.com/Suraj-H675/Ash-Harness/releases/download/"
        f"{ref}/{wheel_name}"
    )
    release_payload = json.dumps(
        {
            "tag_name": ref,
            "immutable": True,
            "assets": [
                {
                    "name": wheel_name,
                    "state": "uploaded",
                    "size": len(wheel_bytes),
                    "digest": f"sha256:{digest}",
                    "browser_download_url": download_url,
                }
            ],
        }
    ).encode()

    class Response(io.BytesIO):
        def __init__(self, payload: bytes, url: str):
            super().__init__(payload)
            self._url = url

        def geturl(self) -> str:
            return self._url

    def opener(request, timeout: int):
        if timeout != 30:
            raise AssertionError(f"unexpected timeout: {timeout}")
        if request.full_url.endswith(f"/releases/tags/{ref}"):
            return Response(release_payload, request.full_url)
        if request.full_url == download_url:
            return Response(wheel_bytes, download_url)
        raise AssertionError(f"unexpected release URL: {request.full_url}")

    artifact: Path | None = None
    with installer._prepared_package_spec(
        ["browser"],
        ref=ref,
        release_opener=opener,
    ) as package_spec:
        requirement = package_spec.split(" @ ", 1)[1]
        parsed = urllib.parse.urlsplit(requirement)
        if parsed.scheme != "file":
            raise AssertionError("release installer did not prepare a local wheel")
        if parsed.fragment != f"sha256={digest}":
            raise AssertionError("release installer lost the wheel digest")
        artifact = Path(urllib.parse.unquote(parsed.path))
        if artifact.read_bytes() != wheel_bytes:
            raise AssertionError("release installer changed verified wheel bytes")
    if artifact is None or artifact.exists():
        raise AssertionError("verified temporary wheel was not cleaned up")

    print("standalone installer Python 3.10 compatibility smoke passed")


if __name__ == "__main__":
    main()
