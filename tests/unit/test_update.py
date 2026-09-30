from __future__ import annotations

import base64
import io
import json
import shlex
import sys
import urllib.error

import pytest

from ash.commands.update import apply_update, check_for_update, render_update_status
from ash.installer import InstallError, InstallResult


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class RedirectedResponse(Response):
    def __init__(self, payload: bytes, final_url: str) -> None:
        super().__init__(payload)
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url


def opener(payload: dict):
    def open_request(request, timeout: int):
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert timeout == 5
        if request.full_url.endswith("/releases/latest"):
            return Response(json.dumps({"immutable": True, **payload}).encode())
        if "/contents/pyproject.toml?" in request.full_url:
            tag = str(payload.get("tag_name", ""))
            version = tag.removeprefix("ash-").removeprefix("v")
            project = (
                '[project]\nname = "ash-ai"\n'
                f'version = "{version}"\n'
            ).encode()
            return Response(
                json.dumps(
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(project).decode(),
                    }
                ).encode()
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return open_request


def test_update_check_normalizes_release_tag_and_reports_upgrade() -> None:
    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )
    assert status.update_available is True
    assert status.latest_version == "0.2.0"
    rendered = render_update_status(status)
    upgrade = rendered.split("Upgrade: ", 1)[1]
    assert shlex.split(upgrade)[:3] == [sys.executable, "-I", "-c"]
    assert "--ref ash-v0.2.0" in rendered
    assert "curl" not in rendered
    assert "pipx install" not in rendered
    assert (
        json.loads(render_update_status(status, json_output=True))["update_available"]
        is True
    )


def test_update_check_reports_current_version() -> None:
    status = check_for_update(
        current_version="1.0.0",
        opener=opener(
            {
                "tag_name": "ash-v1.0.0",
                "html_url": "https://example.test/release",
            }
        ),
    )
    assert status.update_available is False
    assert render_update_status(status) == "Ash 1.0.0 is up to date."


def test_update_check_rejects_release_metadata_redirect() -> None:
    payload = json.dumps(
        {
            "tag_name": "ash-v0.2.0",
            "html_url": "https://github.com/example/release",
            "immutable": True,
        }
    ).encode()

    def redirected(request, timeout: int):
        assert timeout == 5
        return RedirectedResponse(payload, "https://evil.example/releases/latest")

    with pytest.raises(ValueError, match="release check redirected"):
        check_for_update(current_version="0.1.0", opener=redirected)


def test_update_check_rejects_package_metadata_redirect() -> None:
    release = json.dumps(
        {
            "tag_name": "ash-v0.2.0",
            "html_url": "https://github.com/example/release",
            "immutable": True,
        }
    ).encode()

    def redirected(request, timeout: int):
        assert timeout == 5
        if request.full_url == "https://api.github.com/repos/Suraj-H675/Ash-Harness/releases/latest":
            return Response(release)
        return RedirectedResponse(
            b"{}",
            "https://evil.example/pyproject.toml",
        )

    with pytest.raises(ValueError, match="package metadata check redirected"):
        check_for_update(current_version="0.1.0", opener=redirected)


def test_update_check_handles_missing_and_invalid_releases() -> None:
    def missing(request, timeout: int):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    with pytest.raises(ValueError, match="No published"):
        check_for_update(current_version="0.1.0", opener=missing)
    with pytest.raises(ValueError, match="invalid release response"):
        check_for_update(current_version="0.1.0", opener=opener({}))
    with pytest.raises(ValueError, match="not a valid version"):
        check_for_update(
            current_version="0.1.0",
            opener=opener(
                {
                    "tag_name": "ash-vlatest",
                    "html_url": "https://example.test/release",
                }
            ),
        )


def test_update_check_rejects_legacy_noncanonical_release_tag() -> None:
    with pytest.raises(ValueError, match="required 'ash-v' prefix"):
        check_for_update(
            current_version="1.0.0",
            opener=opener(
                {"tag_name": "v1.0.1", "html_url": "https://example.test/release"}
            ),
        )


def test_update_check_rejects_duplicate_release_fields() -> None:
    def duplicate(request, timeout: int):
        assert timeout == 5
        return Response(
            b'{"tag_name":"v9.9.9","tag_name":"v0.2.0",'
            b'"html_url":"https://example.test/release"}'
        )

    with pytest.raises(ValueError, match="invalid release response"):
        check_for_update(current_version="0.1.0", opener=duplicate)


def test_update_check_rejects_mutable_release() -> None:
    with pytest.raises(ValueError, match="release is mutable"):
        check_for_update(
            current_version="0.1.0",
            opener=opener(
                {
                    "tag_name": "ash-v0.2.0",
                    "html_url": "https://github.com/example/release",
                    "immutable": False,
                }
            ),
        )


def test_update_check_rejects_release_package_version_mismatch() -> None:
    release = {
        "tag_name": "ash-v0.2.0",
        "html_url": "https://github.com/example/release",
    }

    def mismatched(request, timeout: int):
        assert timeout == 5
        if request.full_url.endswith("/releases/latest"):
            return Response(json.dumps({"immutable": True, **release}).encode())
        if "/contents/pyproject.toml?" in request.full_url:
            project = b'[project]\nname = "ash-ai"\nversion = "0.1.0"\n'
            return Response(
                json.dumps(
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(project).decode(),
                    }
                ).encode()
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")

    with pytest.raises(ValueError, match="does not match packaged project version"):
        check_for_update(current_version="0.1.0", opener=mismatched)


def test_update_check_verifies_package_version_even_when_already_current() -> None:
    release = {
        "tag_name": "ash-v0.2.0",
        "html_url": "https://github.com/example/release",
    }

    def mismatched(request, timeout: int):
        assert timeout == 5
        if request.full_url.endswith("/releases/latest"):
            return Response(json.dumps({"immutable": True, **release}).encode())
        if "/contents/pyproject.toml?" in request.full_url:
            project = b'[project]\nname = "ash-ai"\nversion = "0.1.0"\n'
            return Response(
                json.dumps(
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(project).decode(),
                    }
                ).encode()
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")

    with pytest.raises(ValueError, match="does not match packaged project version"):
        check_for_update(current_version="0.2.0", opener=mismatched)


def test_apply_update_uses_the_same_verified_installer_as_first_run(capsys) -> None:
    calls: list[str | None] = []

    def installer(*, ref: str | None = None):
        calls.append(ref)
        return InstallResult("pipx", "/isolated/bin/ash", "ash 0.2.0")

    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    assert apply_update(status, installer=installer) == 0
    assert calls == ["ash-v0.2.0"]
    assert "ash 0.2.0" in capsys.readouterr().out


def test_apply_update_rejects_successful_install_with_wrong_reported_version(
    capsys,
) -> None:
    def installer(*, ref: str | None = None):
        assert ref == "ash-v0.2.0"
        return InstallResult("pipx", "/isolated/bin/ash", "ash 0.1.0")

    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    with pytest.raises(ValueError, match="installed version 0.1.0.*expected 0.2.0"):
        apply_update(status, installer=installer)

    assert capsys.readouterr().out == ""


def test_apply_update_does_not_reinstall_current_release(capsys) -> None:
    calls: list[str | None] = []

    def installer(*, ref: str | None = None):
        calls.append(ref)
        raise AssertionError("current releases must not be reinstalled")

    status = check_for_update(
        current_version="0.2.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    assert apply_update(status, installer=installer) == 0
    assert calls == []
    assert capsys.readouterr().out == "Ash 0.2.0 is up to date.\n"


def test_apply_update_explains_installer_failure() -> None:
    def installer(*, ref: str | None = None):
        raise InstallError("Neither pipx nor uv is installed.")

    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    with pytest.raises(ValueError, match="Neither pipx nor uv"):
        apply_update(status, installer=installer)


def test_apply_update_translates_backend_start_failure() -> None:
    def installer(*, ref: str | None = None):
        raise OSError("permission denied")

    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    with pytest.raises(ValueError, match="could not start.*permission denied"):
        apply_update(status, installer=installer)


def test_apply_update_json_success_is_machine_readable(capsys) -> None:
    def installer(*, ref: str | None = None):
        assert ref == "ash-v0.2.0"
        return InstallResult("uv", "/isolated/bin/ash", "ash 0.2.0")

    status = check_for_update(
        current_version="0.1.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    assert apply_update(status, installer=installer, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "current_version": "0.1.0",
        "executable": "/isolated/bin/ash",
        "latest_version": "0.2.0",
        "manager": "uv",
        "release_tag": "ash-v0.2.0",
        "release_url": "https://github.com/example/release",
        "shell_restart_required": False,
        "update_available": True,
        "updated": True,
        "version": "ash 0.2.0",
    }


def test_apply_update_json_noop_stays_machine_readable(capsys) -> None:
    status = check_for_update(
        current_version="0.2.0",
        opener=opener(
            {
                "tag_name": "ash-v0.2.0",
                "html_url": "https://github.com/example/release",
            }
        ),
    )

    assert apply_update(status, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["updated"] is False
    assert payload["update_available"] is False
    assert payload["latest_version"] == "0.2.0"
