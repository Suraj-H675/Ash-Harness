from __future__ import annotations

import hashlib
import json
import io
import os
import subprocess
import sys
import time
import urllib.parse
from types import SimpleNamespace
from pathlib import Path

import pytest

from ash.installer import (
    InstallError,
    InstallResult,
    _corrupt_ash_metadata,
    _ensure_shell_path,
    _read_bounded_file,
    _read_pipx_state,
    _resolve_install_manager,
    _run_captured,
    install,
    main,
)


def _completed(returncode: int = 0, *, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    "payload",
    [
        '{"venvs":{},"venvs":{"ash-ai":{}}}',
        '{"venvs":{},"metric":NaN}',
    ],
)
def test_pipx_state_rejects_ambiguous_json(payload: str) -> None:
    def runner(command, **kwargs):
        del kwargs
        assert command == ["/usr/bin/pipx", "list", "--json"]
        return _completed(stdout=payload)

    inspection = _read_pipx_state(
        "/usr/bin/pipx",
        runner=runner,
        environment={},
    )

    assert inspection.succeeded is False
    assert inspection.error is not None
    assert "pipx returned invalid JSON" in inspection.error


def test_corrupt_pipx_metadata_rejects_duplicate_fields(tmp_path: Path) -> None:
    metadata_path = tmp_path / "venvs" / "ash-ai" / "pipx_metadata.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        '{"main_package":{},"main_package":{"package_or_url":"ash-ai"}}',
        encoding="utf-8",
    )

    assert _corrupt_ash_metadata(str(tmp_path)) == metadata_path


_TEST_RELEASE_WHEEL = b"verified Ash release wheel payload\n"
_TEST_RELEASE_SHA256 = hashlib.sha256(_TEST_RELEASE_WHEEL).hexdigest()


class _ReleaseResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str):
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url


def _release_opener(
    *,
    ref: str = "ash-v0.2.0",
    immutable: bool = True,
    wheel_name: str = "ash_ai-0.2.0-py3-none-any.whl",
    digest: str | None = None,
    size: int | None = None,
    download_url: str | None = None,
    wheel_bytes: bytes = _TEST_RELEASE_WHEEL,
    final_metadata_url: str | None = None,
    final_download_url: str | None = None,
):
    resolved_download_url = download_url or (
        "https://github.com/Suraj-H675/Ash-Harness/releases/download/"
        f"{ref}/{wheel_name}"
    )
    payload = {
        "tag_name": ref,
        "immutable": immutable,
        "assets": [
            {
                "name": wheel_name,
                "state": "uploaded",
                "size": len(wheel_bytes) if size is None else size,
                "digest": digest
                or f"sha256:{hashlib.sha256(wheel_bytes).hexdigest()}",
                "browser_download_url": resolved_download_url,
            }
        ],
    }

    def open_request(request, timeout: int):
        assert timeout == 30
        if request.full_url.endswith(f"/releases/tags/{ref}"):
            return _ReleaseResponse(
                json.dumps(payload).encode(),
                final_metadata_url or request.full_url,
            )
        if request.full_url == resolved_download_url:
            return _ReleaseResponse(
                wheel_bytes,
                final_download_url or resolved_download_url,
            )
        raise AssertionError(f"unexpected release URL: {request.full_url}")

    return open_request


def _generated_ash_launcher(directory: str) -> str:
    name = "ash.exe" if os.name == "nt" else "ash"
    return str(Path(directory) / name)


def _current_runtime_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def test_existing_pipx_install_is_rebuilt_without_exposing_uv_edge_cases() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": (
                                "ash-ai[browser,server] @ "
                                "git+https://github.com/Suraj-H675/Ash-Harness.git"
                            ),
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    install_command, install_kwargs = calls[1]
    assert install_command == [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        _current_runtime_python(),
        "--fetch-python=missing",
        "ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ]
    assert install_kwargs["env"]["UV_VENV_CLEAR"] == "1"
    assert outcome.manager == "pipx"
    assert outcome.executable == "/isolated/bin/ash"
    assert outcome.version == "ash 0.1.0"


def test_installer_scrubs_unrelated_host_secrets_from_manager_environment() -> None:
    observed_environments: list[dict[str, str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        observed_environments.append(dict(kwargs["env"]))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "HOME": "/home/tester",
            "PIPX_BIN_DIR": "/isolated/bin",
            "LC_ALL": "C.UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "OPENAI_API_KEY": "provider-secret",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            "GITHUB_TOKEN": "github-secret",
            "GIT_CONFIG_GLOBAL": "/tmp/attacker.gitconfig",
            "GIT_CONFIG_COUNT": "1",
        },
    )

    assert observed_environments
    for environment in observed_environments:
        assert environment["PATH"].startswith("/isolated/bin")
        assert environment["HOME"] == "/home/tester"
        assert environment["PIPX_BIN_DIR"] == "/isolated/bin"
        assert environment["LC_ALL"] == "C.UTF-8"
        assert environment["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
        assert "OPENAI_API_KEY" not in environment
        assert "AWS_SECRET_ACCESS_KEY" not in environment
        assert "GITHUB_TOKEN" not in environment
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_COUNT" not in environment


def test_manager_resolution_ignores_relative_and_workspace_path_entries(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace_bin = workspace / "bin"
    trusted_bin = tmp_path / "trusted-bin"
    workspace_bin.mkdir(parents=True)
    trusted_bin.mkdir()
    malicious = workspace_bin / "pipx"
    trusted = trusted_bin / "pipx"
    malicious.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    trusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    malicious.chmod(0o755)
    trusted.chmod(0o755)
    monkeypatch.chdir(workspace)

    resolved = _resolve_install_manager(
        "pipx",
        environment={
            "PATH": os.pathsep.join(
                [
                    "bin",
                    str(workspace_bin),
                    str(trusted_bin),
                ]
            ),
            "HOME": str(tmp_path / "home"),
        },
    )

    assert resolved == str(trusted.resolve())


def test_installer_child_path_excludes_workspace_entries(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace_bin = workspace / "bin"
    workspace_bin.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    observed_paths: list[str] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        observed_paths.append(kwargs["env"]["PATH"])
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": os.pathsep.join(
                [
                    "bin",
                    str(workspace_bin),
                    "/isolated/bin",
                    "/usr/bin",
                    "/usr/bin",
                ]
            ),
            "HOME": str(tmp_path / "home"),
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert observed_paths
    for rendered in observed_paths:
        assert rendered == os.pathsep.join(["/isolated/bin", "/usr/bin"])
        assert str(workspace_bin) not in rendered
        assert "bin" != rendered.split(os.pathsep)[0]


def test_release_ref_installs_the_immutable_hashed_wheel() -> None:
    calls: list[list[str]] = []
    list_calls = 0
    installed_artifact: Path | None = None
    installed_metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser]",
                            "app_paths": [
                                {
                                    "__Path__": "/pipx/venvs/ash-ai/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        nonlocal installed_artifact, list_calls
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            list_calls += 1
            return _completed(
                stdout='{"venvs": {}}' if list_calls == 1 else installed_metadata
            )
        if command[1:3] == ["install", "--force"]:
            requirement = command[-1].split(" @ ", 1)[1]
            parsed = urllib.parse.urlsplit(requirement)
            assert parsed.scheme == "file"
            assert parsed.fragment == f"sha256={_TEST_RELEASE_SHA256}"
            installed_artifact = Path(urllib.parse.unquote(parsed.path))
            assert installed_artifact.read_bytes() == _TEST_RELEASE_WHEEL
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.2.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        extras=["browser"],
        ref="ash-v0.2.0",
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
        release_opener=_release_opener(),
    )

    install_call = next(command for command in calls if command[1:3] == ["install", "--force"])
    assert install_call[-1].startswith("ash-ai[browser] @ file://")
    assert install_call[-1].endswith(f"#sha256={_TEST_RELEASE_SHA256}")
    assert "git+" not in install_call[-1]
    assert outcome.version == "ash 0.2.0"
    assert installed_artifact is not None
    assert not installed_artifact.exists()


def test_release_ref_rejects_mutable_wheel_before_manager_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:3] == ["install", "--force"]:
            pytest.fail("mutable release must fail before manager mutation")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="missing, mutable, or does not match"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
            release_opener=_release_opener(immutable=False),
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_release_ref_rejects_wheel_digest_mismatch_before_manager_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:3] == ["install", "--force"]:
            pytest.fail("unverified release wheel must never reach pipx")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="SHA-256 did not match"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
            release_opener=_release_opener(digest="sha256:" + "0" * 64),
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_release_ref_rejects_untrusted_wheel_redirect_before_manager_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:3] == ["install", "--force"]:
            pytest.fail("untrusted wheel redirect must never reach pipx")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="redirected to an untrusted host"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
            release_opener=_release_opener(
                final_download_url="https://evil.example/ash.whl"
            ),
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_release_ref_rejects_release_metadata_redirect_before_manager_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:3] == ["install", "--force"]:
            pytest.fail("redirected release metadata must never reach pipx")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="metadata redirected"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
            release_opener=_release_opener(
                final_metadata_url="https://evil.example/release.json"
            ),
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_release_ref_rejects_http_downgrade_before_manager_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:3] == ["install", "--force"]:
            pytest.fail("downgraded release wheel must never reach pipx")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="redirected to an untrusted URL"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
            release_opener=_release_opener(
                final_download_url=(
                    "http://release-assets.githubusercontent.com/ash.whl"
                )
            ),
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_release_ref_rejects_wrong_installed_version() -> None:
    list_calls = 0
    installed_metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai",
                            "app_paths": [{"__Path__": "/pipx/venvs/ash-ai/bin/ash"}],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        nonlocal list_calls
        if command == ["/usr/bin/pipx", "list", "--json"]:
            list_calls += 1
            return _completed(
                stdout='{"venvs": {}}' if list_calls == 1 else installed_metadata
            )
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="does not match release ash-v0.2.0"):
        install(
            ref="ash-v0.2.0",
            runtime_python="3.14",
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={
                "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
                "PIPX_BIN_DIR": "/isolated/bin",
            },
            release_opener=_release_opener(),
        )


def test_release_ref_uv_path_uses_the_same_hashed_wheel() -> None:
    calls: list[list[str]] = []
    list_calls = 0
    installed_listing = (
        "ash-ai v0.2.0 [required: https://github.com/Suraj-H675/Ash-Harness/] "
        "[extras: server] [CPython 3.14.7] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        nonlocal list_calls
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            list_calls += 1
            return _completed(stdout="" if list_calls == 1 else installed_listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.2.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        extras=["server"],
        ref="ash-v0.2.0",
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/uv" if name == "uv" else None,
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
        release_opener=_release_opener(),
    )

    install_call = next(command for command in calls if command[1:3] == ["tool", "install"])
    assert install_call[-1].startswith("ash-ai[server] @ file://")
    assert install_call[-1].endswith(f"#sha256={_TEST_RELEASE_SHA256}")
    assert "git+" not in install_call[-1]
    assert outcome.manager == "uv"
    assert outcome.version == "ash 0.2.0"


def test_default_manager_resolution_does_not_execute_workspace_binary(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace_bin = workspace / "bin"
    workspace_bin.mkdir(parents=True)
    marker = tmp_path / "executed"
    for name in ("pipx", "uv"):
        executable = workspace_bin / name
        executable.write_text(
            f"#!/bin/sh\nprintf executed > {marker!s}\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    monkeypatch.chdir(workspace)

    with pytest.raises(InstallError, match="Neither pipx nor uv"):
        install(
            runtime_python="3.14",
            environ={
                "PATH": os.pathsep.join(["bin", str(workspace_bin)]),
                "HOME": str(tmp_path / "home"),
            },
        )

    assert not marker.exists()


def test_existing_pypi_style_pipx_spec_preserves_capability_extras() -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser,server]",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        "3.14",
        "--fetch-python=missing",
        "ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_existing_pipx_install_drops_obsolete_extra_but_preserves_supported_one(
    capsys,
) -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser,vector]",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.14",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        "3.14",
        "--fetch-python=missing",
        "ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls
    warning = capsys.readouterr().err
    assert "ignoring obsolete or unsupported Ash capability extras" in warning
    assert "vector" in warning


def test_existing_pipx_install_preserves_supported_runtime_minor() -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "python_version": "Python 3.14.7",
                        "main_package": {
                            "package_or_url": "ash-ai[browser]",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        },
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.12",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        "3.14",
        "--fetch-python=missing",
        "ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_existing_pipx_install_does_not_preserve_unsupported_runtime_minor() -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "python_version": "Python 3.11.14",
                        "main_package": {
                            "package_or_url": "ash-ai",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        },
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.12",
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        "3.12",
        "--fetch-python=missing",
        "ash-ai @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_explicit_pipx_extra_is_additive_to_existing_capability_packs() -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser]",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        extras=["server"],
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/isolated/bin{os.pathsep}/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--python",
        _current_runtime_python(),
        "--fetch-python=missing",
        "ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_uv_is_a_supported_fallback_when_pipx_is_unavailable() -> None:
    calls: list[list[str]] = []
    installed = False
    launcher = _generated_ash_launcher("/isolated/bin")

    def runner(command, **kwargs):
        nonlocal installed
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            return _completed(
                stdout=(
                    "ash-ai v0.1.0 (/isolated/tools/ash-ai)\n"
                    if installed
                    else ""
                )
            )
        if command[1:3] == ["tool", "install"]:
            installed = True
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == [launcher, "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/uv" if name == "uv" else None,
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert calls[1] == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--python",
        _current_runtime_python(),
        "ash-ai @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ]
    assert outcome.manager == "uv"
    assert outcome.executable == launcher


def test_uv_install_rejects_empty_reported_launcher_directory() -> None:
    calls: list[list[str]] = []
    installed = False

    def runner(command, **kwargs):
        nonlocal installed
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            return _completed(
                stdout=(
                    "ash-ai v0.1.0 (/isolated/tools/ash-ai)\n"
                    if installed
                    else ""
                )
            )
        if command[1:3] == ["tool", "install"]:
            installed = True
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="\n")
        if command == ["ash", "--version"]:
            return _completed(stdout="ash 0.0.1\n")
        if command[1:] == ["tool", "update-shell"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="executable directory"):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/uv" if name == "uv" else None,
            environ={"PATH": f"/tmp/older-ash{os.pathsep}/usr/bin"},
        )

    assert ["ash", "--version"] not in calls


def test_public_installer_turns_manager_failures_into_one_clear_message() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    def fail_install(**kwargs):
        raise InstallError("Neither pipx nor uv is installed.")

    assert (
        main(
            ["--ref", "ash-v0.1.0"],
            installer=fail_install,
            stdout=stdout,
            stderr=stderr,
        )
        == 1
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "Ash installation could not continue: Neither pipx nor uv is installed.\n"
        "Install pipx or uv, then run this installer again.\n"
    )


def test_programmatic_installer_rejects_unknown_extra_before_tool_discovery() -> None:
    with pytest.raises(InstallError, match="Unsupported Ash capability extra"):
        install(
            extras=["not-real"],
            which=lambda _name: pytest.fail("tool discovery must not run"),
            runner=lambda *args, **kwargs: pytest.fail("runner must not execute"),
        )


def test_broken_pipx_metadata_recovers_executable_from_pipx_bin_directory() -> None:
    launcher = _generated_ash_launcher("/isolated/bin")

    def runner(command, **kwargs):
        if command[1:] == ["list", "--json"]:
            return _completed(
                stdout=json.dumps(
                    {
                        "venvs": {
                            "ash-ai": {
                                "metadata": {
                                    "main_package": {
                                        "package_or_url": (
                                            "ash-ai @ git+https://example.invalid/ash"
                                        ),
                                        "app_paths": [],
                                    }
                                }
                            }
                        }
                    }
                )
            )
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command[1:] == ["environment", "--value", "PIPX_BIN_DIR"]:
            return _completed(stdout="/isolated/bin\n")
        if command == [launcher, "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert outcome.executable == launcher


def test_public_installer_rejects_unsupported_python_before_running_tools() -> None:
    stderr = io.StringIO()

    def unexpected_install(**kwargs):
        raise AssertionError("installer must not run")

    assert (
        main(
            [],
            installer=unexpected_install,
            stdout=io.StringIO(),
            stderr=stderr,
            python_version=(3, 9),
        )
        == 1
    )
    assert stderr.getvalue() == (
        "Ash's installer requires Python 3.10 or newer; this interpreter is Python 3.9.\n"
    )


def test_public_installer_rejects_native_windows_before_running_tools() -> None:
    stderr = io.StringIO()

    def unexpected_install(**kwargs):
        raise AssertionError("installer must not run")

    assert (
        main(
            [],
            installer=unexpected_install,
            stdout=io.StringIO(),
            stderr=stderr,
            host_platform="win32",
        )
        == 1
    )
    rendered = stderr.getvalue()
    assert "Native Windows is not currently supported by Ash" in rendered
    assert "WSL2" in rendered


def test_programmatic_installer_rejects_native_windows_before_running_tools() -> None:
    with pytest.raises(InstallError, match="WSL2"):
        install(
            host_platform="win32",
            runner=lambda *args, **kwargs: pytest.fail("runner must not execute"),
            which=lambda _name: pytest.fail("tool discovery must not execute"),
        )


def test_public_installer_bridges_old_bootstrap_to_supported_runtime() -> None:
    calls: list[dict[str, object]] = []

    def fake_install(**kwargs):
        calls.append(dict(kwargs))
        return InstallResult("uv", "/isolated/bin/ash", "ash 0.1.0")

    stdout = io.StringIO()
    assert (
        main(
            ["--ref", "ash-v0.1.0"],
            installer=fake_install,
            stdout=stdout,
            stderr=io.StringIO(),
            python_version=(3, 10),
        )
        == 0
    )
    assert calls == [
        {"extras": [], "ref": "ash-v0.1.0", "runtime_python": "3.14"}
    ]
    assert "Ash is ready" in stdout.getvalue()


def test_public_installer_pins_supported_bootstrap_runtime() -> None:
    calls: list[dict[str, object]] = []

    def fake_install(**kwargs):
        calls.append(dict(kwargs))
        return InstallResult("pipx", "/isolated/bin/ash", "ash 0.1.0")

    assert (
        main(
            ["--ref", "ash-v0.1.0"],
            installer=fake_install,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            python_version=(3, 12),
        )
        == 0
    )
    assert calls == [
        {"extras": [], "ref": "ash-v0.1.0", "runtime_python": "3.12"}
    ]


def test_existing_uv_install_keeps_manager_and_capability_extras() -> None:
    calls: list[list[str]] = []
    uv_listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: acp, browser] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
            "--show-python",
        ]:
            return _completed(stdout=uv_listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runtime_python="3.14",
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--python",
        "3.14",
        "ash-ai[acp,browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls
    assert outcome.manager == "uv"


def test_installer_rejects_ambiguous_pipx_and_uv_ownership() -> None:
    calls: list[list[str]] = []
    pipx_metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser]",
                            "app_paths": [
                                {
                                    "__Path__": "/pipx/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )
    uv_listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: server] [CPython 3.14.7] (/uv/tools/ash-ai)\n"
        "- ash (/uv/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=pipx_metadata)
        if command[0] == "/usr/bin/uv" and command[1:3] == ["tool", "list"]:
            return _completed(stdout=uv_listing)
        if command[1:3] == ["install", "--force"] or command[1:3] == [
            "tool",
            "install",
        ]:
            pytest.fail("ambiguous ownership must be resolved before mutation")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="both pipx and uv"):
        install(
            runtime_python="3.14",
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "uv": "/usr/bin/uv",
            }.get(name),
            environ={"PATH": "/usr/bin"},
        )

    assert not any(
        command[1:3] in (["install", "--force"], ["tool", "install"])
        for command in calls
    )


def test_installer_fails_closed_when_secondary_uv_ownership_is_unknown() -> None:
    calls: list[list[str]] = []
    pipx_metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser]",
                            "app_paths": [
                                {
                                    "__Path__": "/pipx/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=pipx_metadata)
        if command[0] == "/usr/bin/uv" and command[1:3] == ["tool", "list"]:
            return _completed(returncode=2, stderr="uv state unavailable")
        if command[1:3] == ["install", "--force"]:
            pytest.fail("pipx must not mutate while uv ownership is unknown")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Could not inspect.*uv"):
        install(
            runtime_python="3.14",
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "uv": "/usr/bin/uv",
            }.get(name),
            environ={"PATH": "/usr/bin"},
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_existing_uv_install_preserves_supported_runtime_minor() -> None:
    calls: list[list[str]] = []
    uv_listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: browser] [CPython 3.14.7] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
            "--show-python",
        ]:
            return _completed(stdout=uv_listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runtime_python="3.12",
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--python",
        "3.14",
        "ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_uv_state_scopes_extras_and_executable_to_ash_tool() -> None:
    calls: list[list[str]] = []
    uv_listing = (
        "other-tool v9.0.0 [extras: server] (/isolated/tools/other-tool)\n"
        "- ash (/tmp/unowned/bin/ash)\n"
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: browser] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
            "--show-python",
        ]:
            return _completed(stdout=uv_listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/tmp/unowned/bin/ash", "--version"]:
            return _completed(stdout="ash 9.0.0\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["tool", "update-shell"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--python",
        _current_runtime_python(),
        "ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls
    assert outcome.executable == "/isolated/bin/ash"
    assert ["/tmp/unowned/bin/ash", "--version"] not in calls


def test_uv_install_rejects_success_without_resulting_ash_state() -> None:
    calls: list[list[str]] = []
    list_calls = 0
    previous_listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: browser] (/isolated/tools/ash-ai)\n"
        "- ash (/tmp/older-ash/bin/ash)\n"
    )

    def runner(command, **kwargs):
        nonlocal list_calls
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
            "--show-python",
        ]:
            list_calls += 1
            return _completed(stdout=previous_listing if list_calls == 1 else "")
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command == ["/tmp/older-ash/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["tool", "update-shell"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="absent from the resulting uv state"):
        install(
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "uv": "/usr/bin/uv",
            }.get(name),
            environ={"PATH": f"/tmp/older-ash/bin{os.pathsep}/usr/bin"},
        )

    assert list_calls == 2
    assert ["/tmp/older-ash/bin/ash", "--version"] not in calls


def test_uv_state_query_failure_does_not_reinstall_as_if_ash_were_absent() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            return _completed(returncode=2, stderr="uv state unavailable")
        if command[1:3] == ["tool", "install"]:
            pytest.fail("uv install must not run after state inspection failure")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Could not inspect.*uv"):
        install(
            runtime_python="3.12",
            runner=runner,
            which=lambda name: "/usr/bin/uv" if name == "uv" else None,
            environ={"PATH": "/usr/bin"},
        )

    assert not any(command[1:3] == ["tool", "install"] for command in calls)


def test_uv_state_query_falls_back_when_show_python_is_unsupported() -> None:
    calls: list[list[str]] = []
    listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: browser] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            if "--show-python" in command:
                return _completed(
                    returncode=2,
                    stderr="unexpected argument '--show-python'",
                )
            return _completed(stdout=listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runtime_python="3.12",
        runner=runner,
        which=lambda name: "/usr/bin/uv" if name == "uv" else None,
        environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
    )

    assert [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--python",
        "3.12",
        "ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls
    assert outcome.manager == "uv"


def test_public_installer_contains_process_start_errors() -> None:
    stderr = io.StringIO()

    def fail_install(**kwargs):
        raise OSError("permission denied")

    assert (
        main(
            ["--ref", "ash-v0.1.0"],
            installer=fail_install,
            stdout=io.StringIO(),
            stderr=stderr,
        )
        == 1
    )
    assert stderr.getvalue() == (
        "Ash installation could not continue: could not start the installer "
        "backend (permission denied).\n"
    )


def test_public_installer_refuses_missing_or_nonrelease_ref() -> None:
    calls = 0

    def unexpected_install(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid release refs must fail before installation")

    for argv in ([], ["--ref", "main"], ["--ref", "ash-vmain"]):
        stderr = io.StringIO()
        assert (
            main(
                argv,
                installer=unexpected_install,
                stdout=io.StringIO(),
                stderr=stderr,
            )
            == 1
        )
        rendered = stderr.getvalue()
        assert "--ref must name a canonical Ash release tag" in rendered
        assert "ash-v0.1.0" in rendered

    assert calls == 0


def test_unusable_pipx_binary_fails_closed_before_uv_mutation() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/broken/pipx", "list", "--json"]:
            raise OSError("bad interpreter")
        if command[0] == "/usr/bin/uv":
            pytest.fail("uv must not run while pipx ownership is unknown")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="cannot safely determine.*ownership"):
        install(
            runner=runner,
            which=lambda name: {
                "pipx": "/broken/pipx",
                "uv": "/usr/bin/uv",
            }.get(name),
            environ={"PATH": f"/isolated/bin{os.pathsep}/usr/bin"},
        )

    assert calls == [["/broken/pipx", "list", "--json"]]


def test_unowned_existing_ash_blocks_new_uv_install() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            return _completed(stdout="")
        if command[1:3] == ["tool", "install"]:
            pytest.fail("uv must not install over an unowned Ash executable")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="existing Ash executable.*uv does not report owning"):
        install(
            runner=runner,
            which=lambda name: {
                "uv": "/usr/bin/uv",
                "ash": "/opt/legacy/bin/ash",
            }.get(name),
            environ={"PATH": "/usr/bin"},
        )

    assert not any(command[1:3] == ["tool", "install"] for command in calls)


def test_unowned_existing_ash_blocks_new_pipx_install() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=json.dumps({"venvs": {}}))
        if command[1:3] == ["install", "--force"]:
            pytest.fail("pipx must not install over an unowned Ash executable")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="existing Ash executable.*neither pipx nor uv"):
        install(
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "ash": "/opt/legacy/bin/ash",
            }.get(name),
            environ={"PATH": "/usr/bin"},
        )

    assert not any(command[1:3] == ["install", "--force"] for command in calls)


def test_installer_updates_shell_path_when_executable_directory_is_missing() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                            "app_paths": [{"__Path__": "/isolated/bin/ash"}],
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["ensurepath"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
    )

    assert ["/usr/bin/pipx", "ensurepath"] in calls
    assert outcome.shell_restart_required is True


def test_installer_uses_exposed_pipx_launcher_directory_for_path_check() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                            "app_paths": [
                                {
                                    "__Path__": "/tmp/ash-pipx-home/venvs/ash-ai/bin/ash",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/tmp/ash-user-bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["ensurepath"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"/tmp/ash-user-bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            "PIPX_BIN_DIR": "/tmp/ash-user-bin",
        },
    )

    assert outcome.shell_restart_required is False
    assert outcome.executable == "/tmp/ash-user-bin/ash"
    assert ["/tmp/ash-pipx-home/venvs/ash-ai/bin/ash", "--version"] not in calls
    assert ["/usr/bin/pipx", "ensurepath"] not in calls


def test_installer_prefers_its_exposed_launcher_over_global_ash() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []
    launcher = _generated_ash_launcher("/tmp/ash-user-bin")

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == [launcher, "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "ash": "/tmp/older-ash/bin/ash",
        }.get(name),
        environ={
            "PATH": f"/tmp/ash-user-bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            "PIPX_BIN_DIR": "/tmp/ash-user-bin",
        },
    )

    assert outcome.executable == launcher
    assert [launcher, "--version"] in calls
    assert ["/tmp/older-ash/bin/ash", "--version"] not in calls


def test_pipx_install_does_not_verify_unowned_global_ash() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                            "app_paths": [],
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command[1:] == ["environment", "--value", "PIPX_BIN_DIR"]:
            return _completed(stdout="\n")
        if command == ["/tmp/older-ash/bin/ash", "--version"]:
            return _completed(stdout="ash 0.0.1\n")
        if command[1:] == ["ensurepath"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="did not report its executable directory"):
        install(
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "ash": "/tmp/older-ash/bin/ash",
            }.get(name),
            environ={"PATH": f"/tmp/older-ash/bin{os.pathsep}/usr/bin"},
        )

    assert ["/tmp/older-ash/bin/ash", "--version"] not in calls


def test_ensure_shell_path_normalizes_symlink_equivalent_directories(tmp_path) -> None:
    actual = tmp_path / "actual-bin"
    actual.mkdir()
    exposed = tmp_path / "exposed-bin"
    try:
        exposed.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _completed()

    restart_required = _ensure_shell_path(
        "/usr/bin/pipx",
        manager="pipx",
        launcher_directory=str(exposed),
        runner=runner,
        environment={"PATH": str(actual)},
    )

    assert restart_required is False
    assert calls == []


def test_ensure_shell_path_rejects_failed_manager_configuration() -> None:
    def runner(command, **kwargs):
        assert command == ["/usr/bin/pipx", "ensurepath"]
        return _completed(returncode=1, stderr="permission denied")

    with pytest.raises(InstallError, match="could not configure.*PATH"):
        _ensure_shell_path(
            "/usr/bin/pipx",
            manager="pipx",
            launcher_directory="/isolated/bin",
            runner=runner,
            environment={"PATH": "/usr/bin"},
        )


def test_installer_fails_closed_on_corrupt_pipx_metadata_without_mutation(
    tmp_path,
) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    original = b"{"
    metadata_path.write_bytes(original)
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(returncode=1, stderr="JSONDecodeError")
        if command[1:3] == ["install", "--force"]:
            pytest.fail("corrupt metadata must be resolved before pipx mutation")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError) as excinfo:
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={
                "PATH": "/usr/bin",
                "PIPX_HOME": str(pipx_home),
            },
        )

    message = str(excinfo.value)
    assert "Corrupt pipx metadata" in message
    assert str(metadata_path) in message
    assert "will not move or delete this file automatically" in message
    assert "move the file aside manually" in message
    assert metadata_path.read_bytes() == original
    assert list(metadata_directory.iterdir()) == [metadata_path]
    assert calls == [["/usr/bin/pipx", "list", "--json"]]


def test_corrupt_pipx_metadata_does_not_fall_back_to_uv(tmp_path) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    original = b"{"
    metadata_path.write_bytes(original)
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(returncode=1, stderr="JSONDecodeError")
        if command[0] == "/usr/bin/uv":
            pytest.fail("uv must not bypass unresolved pipx metadata corruption")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Corrupt pipx metadata"):
        install(
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "uv": "/usr/bin/uv",
            }.get(name),
            runner=runner,
            environ={
                "PATH": "/usr/bin",
                "PIPX_HOME": str(pipx_home),
            },
        )

    assert metadata_path.read_bytes() == original
    assert not any(call[0] == "/usr/bin/uv" for call in calls)


def test_installer_rejects_successful_pipx_install_without_resulting_ash(
    tmp_path,
) -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=json.dumps({"venvs": {}}))
        if command[1:3] == ["install", "--force"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Ash is absent"):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin"},
        )


def test_installer_does_not_mutate_metadata_for_unrelated_pipx_failure(
    tmp_path,
) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    metadata_path.write_text(json.dumps({"valid": True}), encoding="utf-8")
    original_metadata = metadata_path.read_bytes()

    def runner(command, **kwargs):
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(returncode=1, stderr="permission denied")
        if command[1:3] == ["install", "--force"]:
            raise AssertionError("pipx install must not run after unrelated failure")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={
                "PATH": "/usr/bin",
                "PIPX_HOME": str(pipx_home),
                "PIPX_BIN_DIR": str(tmp_path / "bin"),
            },
        )

    assert metadata_path.read_bytes() == original_metadata
    assert list(metadata_directory.glob("pipx_metadata.json.corrupt-*")) == []


def test_installer_translates_backend_timeout_to_actionable_error() -> None:
    def runner(command, **kwargs):
        assert kwargs["timeout"] > 0
        if command == ["/usr/bin/pipx", "list", "--json"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Pipx state query timed out"):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin"},
        )


def test_standalone_installer_help_does_not_import_ash_package() -> None:
    installer = Path(__file__).parents[2] / "src" / "ash" / "installer.py"
    result = subprocess.run(
        [sys.executable, "-I", str(installer), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_installer_rejects_oversized_manager_state_before_json_parsing() -> None:
    oversized_state = "x" * (1024 * 1024 + 1)

    def runner(command, **kwargs):
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=oversized_state)
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Pipx state query returned more"):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={"PATH": "/usr/bin"},
        )


def test_real_installer_capture_terminates_noisy_child() -> None:
    with pytest.raises(InstallError, match="returned more than 16384 bytes"):
        _run_captured(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 100000000)",
            ],
            runner=subprocess.run,
            environment={},
            timeout=10,
            max_bytes=16 * 1024,
            description="test command",
        )


def test_real_installer_capture_terminates_hung_child() -> None:
    with pytest.raises(InstallError, match="timed out after 0.2 seconds"):
        _run_captured(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            runner=subprocess.run,
            environment={},
            timeout=0.2,
            max_bytes=16 * 1024,
            description="test command",
        )


def test_streaming_cleanup_failure_preserves_primary_on_python_310_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.installer as installer_module

    class LegacyInterrupt(BaseException):
        add_note = None

    class FakeProcess:
        def wait(self, timeout=None):
            raise LegacyInterrupt("cancelled")

    monkeypatch.setattr(
        installer_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        installer_module,
        "_terminate_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(InstallError("cleanup failed")),
    )

    with pytest.raises(LegacyInterrupt, match="cancelled") as captured:
        installer_module._run_streaming(
            ["fake-installer"],
            runner=subprocess.run,
            environment={},
            timeout=1,
            description="test installation",
            failure_message="install failed",
        )

    assert isinstance(captured.value.__cause__, InstallError)
    assert str(captured.value.__cause__) == "cleanup failed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_real_streaming_installer_timeout_terminates_child_processes(tmp_path) -> None:
    sentinel = tmp_path / "child-finished"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.35); Path({str(sentinel)!r}).write_text('done')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    )

    with pytest.raises(InstallError, match="timed out after 0.1 seconds"):
        from ash.installer import _run_streaming

        _run_streaming(
            [sys.executable, "-c", parent],
            runner=subprocess.run,
            environment={},
            timeout=0.1,
            description="test installation",
            failure_message="install failed",
        )

    time.sleep(0.5)
    assert not sentinel.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_installer_cleanup_terminates_descendants_after_root_exits(tmp_path) -> None:
    import ash.installer as installer_module

    sentinel = tmp_path / "orphan-finished"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.35); Path({str(sentinel)!r}).write_text('done')"
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}])",
        ],
        start_new_session=True,
    )
    assert parent.wait(timeout=5) == 0

    installer_module._terminate_process(
        parent,
        plan=installer_module._InstallerProcessTreePlan(
            spawn_options={"start_new_session": True}
        ),
    )

    time.sleep(0.5)
    assert not sentinel.exists()


def test_installer_bounded_file_reader_rejects_oversized_metadata(tmp_path) -> None:
    metadata_path = tmp_path / "pipx_metadata.json"
    metadata_path.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="exceeds 1048576 bytes"):
        _read_bounded_file(metadata_path, max_bytes=1024 * 1024)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_installer_bounded_file_reader_rejects_fifo_without_blocking(tmp_path) -> None:
    metadata_path = tmp_path / "pipx_metadata.json"
    os.mkfifo(metadata_path)

    with pytest.raises(ValueError, match="non-regular file"):
        _read_bounded_file(metadata_path, max_bytes=1024)
