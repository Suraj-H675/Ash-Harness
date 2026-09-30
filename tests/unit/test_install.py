from __future__ import annotations

import hashlib
import json
import shlex
import sys
import urllib.error
import urllib.request

import pytest

from ash.install import (
    INSTALLER_ASSET_NAME,
    LATEST_RELEASE_API,
    _BOOTSTRAP_CODE,
    install_command,
    pipx_install_command,
)


_ASSET_API_URL = (
    "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases/assets/12345"
)


class _Response:
    def __init__(self, payload: bytes, final_url: str | None = None):
        self.payload = payload
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            return self.payload
        return self.payload[:limit]

    def geturl(self) -> str:
        return self.final_url or ""


class _Opener:
    def __init__(
        self,
        responses: dict[str, bytes | BaseException | tuple[bytes, str]],
    ):
        self.responses = responses
        self.requests: list[urllib.request.Request] = []

    def open(self, request, *, timeout: int):
        assert timeout == 30
        self.requests.append(request)
        response = self.responses.get(request.full_url)
        if response is None:
            raise AssertionError(f"unexpected bootstrap URL: {request.full_url}")
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):
            payload, final_url = response
            return _Response(payload, final_url)
        return _Response(response, request.full_url)


def _release_metadata(
    installer: bytes,
    *,
    immutable: bool = True,
    digest: str | None = None,
    size: int | None = None,
    tag: str = "ash-v1.2.3",
) -> bytes:
    return json.dumps(
        {
            "immutable": immutable,
            "tag_name": tag,
            "assets": [
                {
                    "name": INSTALLER_ASSET_NAME,
                    "state": "uploaded",
                    "url": _ASSET_API_URL,
                    "digest": digest
                    or f"sha256:{hashlib.sha256(installer).hexdigest()}",
                    "size": len(installer) if size is None else size,
                }
            ],
        }
    ).encode()


def _run_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    release: bytes,
    installer: bytes,
    arguments: list[str] | None = None,
    release_url: str = LATEST_RELEASE_API,
) -> tuple[_Opener, list[urllib.request.HTTPRedirectHandler]]:
    opener = _Opener(
        {
            release_url: release,
            _ASSET_API_URL: installer,
        }
    )
    handlers: list[urllib.request.HTTPRedirectHandler] = []

    def build_opener(*configured_handlers):
        handlers.extend(configured_handlers)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(
        sys,
        "argv",
        ["-c", release_url, *(arguments or [])],
    )
    exec(_BOOTSTRAP_CODE, {"__name__": "__main__"})
    return opener, handlers


def _installer_prefix() -> str:
    command = install_command()
    argv = shlex.split(command)
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert "curl" not in command
    assert " | " not in command
    assert "\n" not in command
    assert "\r" not in command
    assert "raw.githubusercontent.com" not in command
    assert LATEST_RELEASE_API in command
    return command


def test_install_command_hides_package_manager_repair_details() -> None:
    assert install_command() == _installer_prefix()


def test_install_command_keeps_sorted_capability_extras_and_ref() -> None:
    command = install_command("browser", "server", "browser", ref="ash-v1.2.3")

    assert "/releases/tags/ash-v1.2.3" in command
    assert command.endswith(" --extra browser --extra server --ref ash-v1.2.3")


def test_install_command_rejects_nonrelease_ref() -> None:
    with pytest.raises(ValueError, match="canonical ash-v<version>"):
        install_command(ref="main")


def test_old_pipx_helper_routes_callers_to_public_installer() -> None:
    assert pipx_install_command("browser") == install_command("browser")


def test_bootstrap_fetch_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _Opener({LATEST_RELEASE_API: urllib.error.URLError("offline")})
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    monkeypatch.setattr(sys, "argv", ["-c", LATEST_RELEASE_API])

    with pytest.raises(SystemExit, match="could not fetch"):
        exec(_BOOTSTRAP_CODE, {"__name__": "__main__"})


def test_bootstrap_preserves_arguments_and_pins_release(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = b"import json,sys; print(json.dumps(sys.argv[1:]))\n"

    opener, _handlers = _run_bootstrap(
        monkeypatch,
        release=_release_metadata(installer),
        installer=installer,
        arguments=["--extra", "browser"],
    )

    assert json.loads(capsys.readouterr().out) == [
        "--extra",
        "browser",
        "--ref",
        "ash-v1.2.3",
    ]
    assert [request.full_url for request in opener.requests] == [
        LATEST_RELEASE_API,
        _ASSET_API_URL,
    ]


def test_bootstrap_preserves_explicit_release_ref(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = b"import json,sys; print(json.dumps(sys.argv[1:]))\n"
    release_url = (
        "https://api.github.com/repos/Suraj-H675/Ash-Harness/"
        "releases/tags/ash-v1.1.0"
    )

    _run_bootstrap(
        monkeypatch,
        release=_release_metadata(installer, tag="ash-v1.1.0"),
        installer=installer,
        arguments=["--ref", "ash-v1.1.0"],
        release_url=release_url,
    )

    assert json.loads(capsys.readouterr().out) == ["--ref", "ash-v1.1.0"]


def test_bootstrap_rejects_mutable_release(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = b"raise AssertionError('must not execute')\n"

    with pytest.raises(SystemExit, match="release is not immutable"):
        _run_bootstrap(
            monkeypatch,
            release=_release_metadata(installer, immutable=False),
            installer=installer,
        )


def test_bootstrap_rejects_non_finite_release_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = b"raise AssertionError('must not execute')\n"
    valid = json.loads(_release_metadata(installer))
    release = json.dumps(valid).removesuffix("}") + ',"metric":NaN}'

    with pytest.raises(SystemExit, match="invalid JSON constant: NaN"):
        _run_bootstrap(
            monkeypatch,
            release=release.encode(),
            installer=installer,
        )


def test_bootstrap_rejects_release_metadata_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = b"raise AssertionError('must not execute')\n"
    opener = _Opener(
        {
            LATEST_RELEASE_API: (
                _release_metadata(installer),
                "https://evil.example/releases/latest",
            )
        }
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    monkeypatch.setattr(sys, "argv", ["-c", LATEST_RELEASE_API])

    with pytest.raises(SystemExit, match="metadata redirected"):
        exec(_BOOTSTRAP_CODE, {"__name__": "__main__"})


def test_bootstrap_rejects_noncanonical_release_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = b"raise AssertionError('must not execute')\n"

    with pytest.raises(SystemExit, match="release tag was missing or invalid"):
        _run_bootstrap(
            monkeypatch,
            release=_release_metadata(installer, tag="ash-vmain"),
            installer=installer,
        )


def test_bootstrap_rejects_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = b"raise AssertionError('must not execute')\n"

    with pytest.raises(SystemExit, match="digest did not match"):
        _run_bootstrap(
            monkeypatch,
            release=_release_metadata(installer, digest="sha256:" + "0" * 64),
            installer=installer,
        )


def test_bootstrap_rejects_oversized_installer_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = b"raise AssertionError('must not execute')\n"

    with pytest.raises(SystemExit, match="asset size is invalid"):
        _run_bootstrap(
            monkeypatch,
            release=_release_metadata(installer, size=1024 * 1024 + 1),
            installer=installer,
        )


def test_bootstrap_rejects_untrusted_redirect_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = b"print('ok')\n"
    _opener, handlers = _run_bootstrap(
        monkeypatch,
        release=_release_metadata(installer),
        installer=installer,
    )
    capsys.readouterr()
    redirect = next(
        handler
        for handler in handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    )

    with pytest.raises(SystemExit, match="untrusted host"):
        redirect.redirect_request(
            urllib.request.Request(_ASSET_API_URL),
            None,
            302,
            "Found",
            {},
            "https://evil.example/install-ash.py",
        )


def test_bootstrap_rejects_http_redirect_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installer = b"print('ok')\n"
    _opener, handlers = _run_bootstrap(
        monkeypatch,
        release=_release_metadata(installer),
        installer=installer,
    )
    capsys.readouterr()
    redirect = next(
        handler
        for handler in handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    )

    with pytest.raises(SystemExit, match="untrusted host"):
        redirect.redirect_request(
            urllib.request.Request(_ASSET_API_URL),
            None,
            302,
            "Found",
            {},
            "http://release-assets.githubusercontent.com/install-ash.py",
        )
