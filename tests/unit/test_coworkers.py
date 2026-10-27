from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _isolated_ash_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    for name in tuple(os.environ):
        if name.startswith("ASH_"):
            monkeypatch.delenv(name, raising=False)
    ash_dir = home / ".ash"
    ash_dir.mkdir()
    return ash_dir


def _profile(ash_dir: Path, name: str, *, settings: str = "") -> Path:
    profile = ash_dir / "profiles" / name
    profile.mkdir(parents=True)
    if settings:
        (profile / "ash.toml").write_text(settings, encoding="utf-8")
    return profile


@pytest.mark.parametrize("existing_api_key", [None, "preexisting-process-key"])
def test_explicit_profile_load_is_concurrent_and_does_not_publish_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_api_key: str | None,
) -> None:
    from ash.config import AshConfig

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    alpha = _profile(ash_dir, "alpha")
    beta = _profile(ash_dir, "beta")
    (alpha / "ash.toml").write_text("model = 'alpha-model'\n", encoding="utf-8")
    (beta / "ash.toml").write_text("model = 'beta-model'\n", encoding="utf-8")
    marker = ash_dir / "active-profile"
    marker.write_text("marker-only\n", encoding="utf-8")
    monkeypatch.setenv("ASH_PROFILE", "environment-only")
    monkeypatch.setenv("ASH_OPENAI_API_KEY", "ambient-api-sentinel")
    monkeypatch.setenv("ASH_MODEL", "openai/ambient-model")
    monkeypatch.setenv("ASH_PROVIDER", "openai")
    if existing_api_key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", existing_api_key)
    (alpha / ".env").write_text(
        "ASH_OPENAI_API_KEY=alpha-sentinel\nANTHROPIC_API_KEY=alpha-runtime-only\n",
        encoding="utf-8",
    )
    (beta / ".env").write_text(
        "ASH_OPENAI_API_KEY=beta-sentinel\nANTHROPIC_API_KEY=beta-runtime-only\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = dict(os.environ)

    with ThreadPoolExecutor(max_workers=2) as executor:
        alpha_future = executor.submit(
            AshConfig._load_for_profile,
            "alpha",
            ash_dir=ash_dir,
            workspace_root=workspace,
        )
        beta_future = executor.submit(
            AshConfig._load_for_profile,
            "beta",
            ash_dir=ash_dir,
            workspace_root=workspace,
        )
        alpha_config = alpha_future.result()
        beta_config = beta_future.result()

    assert alpha_config.db_directory == alpha / "db"
    assert beta_config.db_directory == beta / "db"
    assert alpha_config.openai_api_key == "alpha-sentinel"
    assert beta_config.openai_api_key == "beta-sentinel"
    assert alpha_config.model == "anthropic/alpha-model"
    assert beta_config.model == "anthropic/beta-model"
    assert alpha_config._coworker_runtime_blocked is True
    assert beta_config._coworker_runtime_blocked is True
    assert dict(os.environ) == before
    assert marker.read_text(encoding="utf-8") == "marker-only\n"
    assert os.environ.get("ANTHROPIC_API_KEY") == existing_api_key


@pytest.mark.parametrize(
    "settings,dotenv,environment",
    [
        ("safety_tier = 'auto_approve'\n", "", None),
        ("allow_unsafe_auto_approve = true\n", "", None),
        ("", "ASH_SAFETY_TIER=auto_approve\n", None),
        ("", "", "auto_approve"),
    ],
)
def test_coworker_profile_rejects_inherited_auto_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: str,
    dotenv: str,
    environment: str | None,
) -> None:
    from ash.config import AshConfig

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    profile = _profile(ash_dir, "alpha", settings=settings)
    if dotenv:
        (profile / ".env").write_text(dotenv, encoding="utf-8")
    if environment is not None:
        monkeypatch.setenv("ASH_SAFETY_TIER", environment)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="auto_approve"):
        AshConfig._load_for_profile(
            "alpha",
            ash_dir=ash_dir,
            workspace_root=workspace,
        )


def test_coworker_profile_rejects_ambient_unsafe_auto_approve_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.config import AshConfig

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    profile = _profile(ash_dir, "alpha")
    monkeypatch.setenv("ASH_ALLOW_UNSAFE_AUTO_APPROVE", "true")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="unsafe auto_approve"):
        AshConfig._load_for_profile(
            "alpha",
            ash_dir=ash_dir,
            workspace_root=workspace,
        )
    assert not (profile / "db" / "sessions.db").exists()


def test_coworker_identity_restart_rename_and_session_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.core.session import SessionStorageError, SessionStore
    from ash.coworkers import CoworkerDirectory, CoworkerNotFound

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = CoworkerDirectory.open(ash_dir)
    created = directory.create(
        owner_id="local-operator",
        display_name="Research",
        profile_name="alpha",
        workspace=workspace,
    )
    handle = directory.open_coworker(
        owner_id="local-operator",
        coworker_id=created.coworker_id,
    )
    canonical = handle.ensure_canonical_conversation()
    run_one = handle.begin_background_run("run-one")
    run_one_again = handle.begin_background_run("run-one")
    run_two = handle.begin_background_run("run-two")

    assert canonical.session_id == canonical.canonical_session_id
    assert run_one.session_id != canonical.session_id
    assert run_two.session_id not in {canonical.session_id, run_one.session_id}
    assert run_one_again == run_one

    store = SessionStore(handle.effective_config().db_directory / "sessions.db")
    if os.name != "posix":
        pytest.skip("Ash session runtime leases currently require POSIX flock")
    canonical_lease = store.acquire_session_runtime_lease(canonical.session_id)
    try:
        with pytest.raises(SessionStorageError, match="active in another Ash process"):
            handle.begin_background_run("blocked-by-canonical-turn")
    finally:
        canonical_lease.close()
    run_one_lease = store.acquire_session_runtime_lease(run_one.session_id)
    run_two_lease = store.acquire_session_runtime_lease(run_two.session_id)
    run_one_lease.close()
    run_two_lease.close()
    run_one_lease = store.acquire_session_runtime_lease(run_one.session_id)
    try:
        with pytest.raises(SessionStorageError, match="active in another Ash process"):
            store.acquire_session_runtime_lease(run_one.session_id)
    finally:
        run_one_lease.close()

    renamed = directory.rename(
        owner_id="local-operator",
        coworker_id=created.coworker_id,
        display_name="Release watch",
    )
    reopened = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="local-operator",
        coworker_id=created.coworker_id,
    )
    assert renamed.coworker_id == created.coworker_id
    assert renamed.profile_name == created.profile_name
    assert reopened.identity.coworker_id == created.coworker_id
    assert reopened.identity.owner_id == "local-operator"
    assert reopened.identity.display_name == "Release watch"
    assert reopened.ensure_canonical_conversation() == canonical
    assert reopened.begin_background_run("run-one") == run_one
    with pytest.raises(CoworkerNotFound):
        directory.open_coworker(
            owner_id="another-owner",
            coworker_id=created.coworker_id,
        )


def test_coworker_provisioning_recovers_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerDirectory, CoworkerHandle

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = CoworkerDirectory.open(ash_dir)
    finish_provisioning = CoworkerHandle._finish_provisioning

    def stop_after_directory_commit(self: CoworkerHandle) -> None:
        del self
        raise RuntimeError("simulated process stop after directory commit")

    monkeypatch.setattr(
        CoworkerHandle,
        "_finish_provisioning",
        stop_after_directory_commit,
    )
    with pytest.raises(RuntimeError, match="simulated process stop"):
        directory.create(
            owner_id="owner",
            display_name="Recover me",
            profile_name="alpha",
            workspace=workspace,
        )

    pending = directory._get_by_profile("alpha")
    assert pending is not None
    assert pending.state == "provisioning"

    monkeypatch.setattr(
        CoworkerHandle,
        "_finish_provisioning",
        finish_provisioning,
    )
    reopened = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="owner",
        coworker_id=pending.coworker_id,
    )
    assert reopened.identity.state == "ready"
    assert reopened.ensure_canonical_conversation().session_id == (
        pending.canonical_session_id
    )


def test_coworker_refuses_to_adopt_existing_profile_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.config import AshConfig
    from ash.core.session import SessionStorageError, SessionStore
    from ash.coworkers import CoworkerDirectory

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AshConfig._load_for_profile(
        "alpha",
        ash_dir=ash_dir,
        workspace_root=workspace,
    )
    store = SessionStore(config.db_directory / "sessions.db")
    existing = store.create_session(str(workspace))
    directory = CoworkerDirectory.open(ash_dir)

    with pytest.raises(SessionStorageError, match="existing sessions"):
        directory.create(
            owner_id="owner",
            display_name="Do not adopt",
            profile_name="alpha",
            workspace=workspace,
        )

    assert directory._get_by_profile("alpha") is None
    assert store.load_session(existing.session_id).session_id == existing.session_id


def test_coworker_profile_binding_and_session_access_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerDirectory

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    _profile(ash_dir, "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = CoworkerDirectory.open(ash_dir)
    alpha = directory.create(
        owner_id="owner",
        display_name="A",
        profile_name="alpha",
        workspace=workspace,
    )
    beta = directory.create(
        owner_id="owner",
        display_name="B",
        profile_name="beta",
        workspace=workspace,
    )
    alpha_handle = directory.open_coworker(
        owner_id="owner", coworker_id=alpha.coworker_id
    )
    beta_handle = directory.open_coworker(
        owner_id="owner", coworker_id=beta.coworker_id
    )
    alpha_run = alpha_handle.begin_background_run("alpha-run")
    beta_run = beta_handle.begin_background_run("beta-run")

    assert alpha.coworker_id != beta.coworker_id
    assert alpha_run.session_id != beta_run.session_id
    assert (
        alpha_handle.effective_config().db_directory
        != beta_handle.effective_config().db_directory
    )
    with pytest.raises(KeyError, match="coworker session not found"):
        alpha_handle.load_session(beta_run.session_id)
    with pytest.raises(ValueError, match="already bound"):
        directory.create(
            owner_id="different-owner",
            display_name="Impostor",
            profile_name="alpha",
            workspace=workspace,
        )


def test_coworker_rejects_default_missing_and_rogue_profiles_before_db_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerDirectory

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = CoworkerDirectory.open(ash_dir)

    for profile in ("default", "missing"):
        with pytest.raises(ValueError):
            directory.create(
                owner_id="owner",
                display_name="Reject",
                profile_name=profile,
                workspace=workspace,
            )

    outside_db = tmp_path / "outside-db"
    profile_dir = _profile(
        ash_dir,
        "rogue",
        settings=f"db_directory = {str(outside_db)!r}\n",
    )
    with pytest.raises(ValueError, match="db_directory"):
        directory.create(
            owner_id="owner",
            display_name="Reject path",
            profile_name="rogue",
            workspace=workspace,
        )
    assert not outside_db.exists()
    assert not (profile_dir / "db" / "sessions.db").exists()

    ambient_profile = _profile(ash_dir, "ambient")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(outside_db))
    with pytest.raises(ValueError, match="environment db_directory"):
        directory.create(
            owner_id="owner",
            display_name="Reject ambient path",
            profile_name="ambient",
            workspace=workspace,
        )
    assert not outside_db.exists()
    assert not (ambient_profile / "db" / "sessions.db").exists()


def test_coworker_config_cannot_start_runtime_and_headless_ask_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerDirectory, CoworkerRuntimeUnavailable
    from ash.providers.registry import ProviderRegistry
    from ash.runtime import RuntimeCapabilityProfile
    from ash.sdk import AshClient
    from ash.ui.headless import HeadlessUI

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def reject_provider_build(self: ProviderRegistry, config: object) -> None:
        del self, config
        pytest.fail("coworker registration must not build a model provider")

    monkeypatch.setattr(ProviderRegistry, "build", reject_provider_build)
    coworker = CoworkerDirectory.open(ash_dir).create(
        owner_id="owner",
        display_name="No runtime yet",
        profile_name="alpha",
        workspace=workspace,
    )
    handle = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="owner",
        coworker_id=coworker.coworker_id,
    )
    with pytest.raises(CoworkerRuntimeUnavailable, match="runtime is unavailable"):
        handle.start_runtime()
    with pytest.raises(RuntimeError, match="cannot start a model runtime"):
        import asyncio

        asyncio.run(
            AshClient.create(
                config=handle.effective_config(),
                provider=object(),
                runtime_capability_profile=RuntimeCapabilityProfile.TURN_ONLY,
            )
        )
    assert HeadlessUI().request_tool_approval("write_file", {}) is False


def test_coworker_process_environments_do_not_inherit_host_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerDirectory, CoworkerProcessEnvironment

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    _profile(ash_dir, "beta")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = CoworkerDirectory.open(ash_dir)
    identities = [
        directory.create(
            owner_id="owner",
            display_name=name,
            profile_name=name,
            workspace=workspace,
        )
        for name in ("alpha", "beta")
    ]
    handles = [
        directory.open_coworker(owner_id="owner", coworker_id=item.coworker_id)
        for item in identities
    ]

    ambient = {
        "ASH_PROFILE": "ambient-profile",
        "ASH_OPENAI_API_KEY": "ambient-provider-sentinel",
        "ASH_MODEL": "ambient-provider-model",
        "ANTHROPIC_API_KEY": "ambient-provider-sentinel",
        "OPENAI_API_KEY": "ambient-provider-sentinel",
        "AWS_SECRET_ACCESS_KEY": "ambient-cloud-sentinel",
        "AWS_SHARED_CREDENTIALS_FILE": "/ambient/aws/credentials",
        "CLOUDSDK_CONFIG": "/ambient/gcloud",
        "GOOGLE_APPLICATION_CREDENTIALS": "/ambient/cloud/credentials.json",
        "AZURE_CLIENT_SECRET": "ambient-cloud-sentinel",
        "AZURE_CONFIG_DIR": "/ambient/azure",
        "HTTP_PROXY": "http://ambient-proxy.invalid",
        "PYTHONPATH": "/ambient/python/path",
        "PATH": "/ambient/executable/path",
        "XDG_CONFIG_HOME": "/ambient/config",
        "XDG_DATA_HOME": "/ambient/data",
        "XDG_CACHE_HOME": "/ambient/cache",
        "TMPDIR": "/ambient/tmp",
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    probe = (
        "import json, os, tempfile; from pathlib import Path; "
        "print(json.dumps({'env': dict(os.environ), 'home': str(Path.home()), "
        "'cwd': os.getcwd(), 'tmp': tempfile.gettempdir(), "
        "'ash_exists': (Path.home() / '.ash').exists()}))"
    )

    with (
        handles[0].worker_process_environment() as alpha_environment,
        handles[1].worker_process_environment() as beta_environment,
    ):
        assert alpha_environment.root != beta_environment.root
        if os.name != "nt":
            assert stat.S_IMODE(alpha_environment.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(beta_environment.root.stat().st_mode) == 0o700

        def run_probe(
            environment: CoworkerProcessEnvironment,
        ) -> dict[str, object]:
            process_environment = environment.as_environ()
            completed = subprocess.run(
                [sys.executable, "-I", "-c", probe],
                cwd=environment.working_directory,
                env=process_environment,
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            return json.loads(completed.stdout)

        with ThreadPoolExecutor(max_workers=2) as executor:
            alpha_future = executor.submit(run_probe, alpha_environment)
            beta_future = executor.submit(run_probe, beta_environment)
            alpha_result = alpha_future.result()
            beta_result = beta_future.result()

        for environment, result in (
            (alpha_environment, alpha_result),
            (beta_environment, beta_result),
        ):
            child_environment = result["env"]
            assert isinstance(child_environment, dict)
            expected_environment = environment.as_environ()
            assert child_environment.keys() >= expected_environment.keys()
            os_injected_names = {"LC_CTYPE"}
            if sys.platform == "darwin":
                os_injected_names.add("__CF_USER_TEXT_ENCODING")
            assert child_environment.keys() - expected_environment.keys() <= (
                os_injected_names
            )
            assert all(
                child_environment[name] == value
                for name, value in expected_environment.items()
            )
            assert result["home"] == str(environment.home)
            assert result["cwd"] == str(environment.working_directory)
            assert result["tmp"] == str(environment.temporary_directory)
            assert result["ash_exists"] is False
            assert all(
                child_environment.get(name) != value for name, value in ambient.items()
            )

    assert not alpha_environment.root.exists()
    assert not beta_environment.root.exists()
    assert dict(os.environ) == before


def test_coworker_process_environment_refuses_workspace_storage(
    tmp_path: Path,
) -> None:
    from ash.coworkers import CoworkerDirectory, CoworkerDirectoryError

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ash_dir = workspace / ".ash"
    ash_dir.mkdir()
    _profile(ash_dir, "alpha")
    handle = CoworkerDirectory.open(ash_dir).create(
        owner_id="owner",
        display_name="Workspace state",
        profile_name="alpha",
        workspace=workspace,
    )
    handle = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="owner",
        coworker_id=handle.coworker_id,
    )

    with pytest.raises(CoworkerDirectoryError, match="outside the workspace"):
        with handle.worker_process_environment():
            pytest.fail("workspace storage must not back a worker process")


@pytest.mark.skipif(sys.platform != "linux", reason="staging is Linux-only")
def test_coworker_python_syntax_check_stages_only_permitted_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from ash.coworkers import CoworkerDirectory
    from ash.sandbox.manager import SandboxResult

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "selected.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / "not-selected.py").write_text("def broken(:\n", encoding="utf-8")
    directory = CoworkerDirectory.open(ash_dir)
    identity = directory.create(
        owner_id="owner-label",
        display_name="Python checker",
        profile_name="alpha",
        workspace=workspace,
    )
    handle = directory.open_coworker(
        owner_id="owner-label",
        coworker_id=identity.coworker_id,
    )
    observed: dict[str, object] = {}

    class FakeSandboxManager:
        def __init__(self, **settings: object) -> None:
            observed["settings"] = settings
            self.backend_name = "docker"

        def has_aggregate_resource_limits(self) -> bool:
            return True

        async def run_supervised_process(
            self,
            command: Sequence[str],
            *,
            cwd: Path,
            timeout: int,
        ) -> SandboxResult:
            observed["command"] = command
            observed["timeout"] = timeout
            observed["staged_names"] = sorted(path.name for path in cwd.iterdir())
            assert (cwd / "selected.py").read_text(encoding="utf-8") == "answer = 42\n"
            return SandboxResult(
                exit_code=0,
                stdout='{"files":[{"name":"selected.py","ok":true,"line":null,"column":null}]}',
                stderr="",
                tier=3,
                backend_name="docker",
            )

    monkeypatch.setattr("ash.coworkers.SandboxManager", FakeSandboxManager)

    report = asyncio.run(
        handle.check_python_syntax(
            run_id="syntax-run",
            permitted_files=("selected.py",),
        )
    )

    assert identity.owner_uid == os.geteuid()
    assert report.session_ref.run_id == "syntax-run"
    assert report.session_ref.session_id != report.session_ref.canonical_session_id
    assert report.files[0].name == "selected.py"
    assert report.files[0].valid is True
    assert observed["staged_names"] == ["selected.py"]
    assert observed["command"][0] == "/usr/local/bin/python"
    assert "not-selected.py" not in observed["command"]
    assert observed["settings"]["network"] is False
    assert observed["settings"]["workspace_read_only"] is True
    assert observed["settings"]["allow_scoped_fallback"] is False


@pytest.mark.skipif(sys.platform != "linux", reason="staging requires Linux mountinfo")
@pytest.mark.parametrize(
    "adversary",
    [
        "owner",
        "symlink",
        "hardlink",
        "fifo",
        "socket",
        "mountpoint",
        "secret",
        "credential",
        "traversal",
        "unknown",
    ],
)
def test_coworker_python_syntax_check_rejects_unverified_or_unsafe_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adversary: str,
) -> None:
    import asyncio

    from ash.coworkers import (
        CoworkerDirectory,
        CoworkerDirectoryError,
        CoworkerRuntimeUnavailable,
    )
    from ash.sandbox.manager import SandboxResult

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selected = workspace / "selected.py"
    selected.write_text("value = 1\n", encoding="utf-8")
    if adversary == "symlink":
        selected.unlink()
        selected.symlink_to(workspace / "other.py")
        (workspace / "other.py").write_text("value = 1\n", encoding="utf-8")
    elif adversary == "hardlink":
        selected.unlink()
        (workspace / "other.py").write_text("value = 1\n", encoding="utf-8")
        os.link(workspace / "other.py", selected)
    elif adversary == "fifo":
        selected.unlink()
        os.mkfifo(selected)
    elif adversary == "socket":
        import errno
        import socket

        selected.unlink()
        with socket.socket(socket.AF_UNIX) as listener:
            prior_directory = Path.cwd()
            try:
                os.chdir(workspace)
                try:
                    listener.bind(selected.name)
                except PermissionError as exc:
                    if exc.errno != errno.EPERM:
                        raise
                    pytest.skip("host sandbox forbids Unix-domain socket creation")
            finally:
                os.chdir(prior_directory)
    elif adversary == "secret":
        selected.write_text(
            'aws_secret_access_key = "' + "x" * 32 + '"\n',
            encoding="utf-8",
        )
    elif adversary == "credential":
        selected.write_text(
            'credential = "' + "x" * 32 + '"\n',
            encoding="utf-8",
        )

    directory = CoworkerDirectory.open(ash_dir)
    identity = directory.create(
        owner_id="owner-label",
        display_name="Python checker",
        profile_name="alpha",
        workspace=workspace,
    )
    handle = directory.open_coworker(
        owner_id="owner-label",
        coworker_id=identity.coworker_id,
    )
    dispatched = False

    class FakeSandboxManager:
        def __init__(self, **_settings: object) -> None:
            self.backend_name = "docker"

        def has_aggregate_resource_limits(self) -> bool:
            return True

        async def run_supervised_process(
            self,
            _command: Sequence[str],
            *,
            cwd: Path,
            timeout: int,
        ) -> SandboxResult:
            nonlocal dispatched
            del cwd, timeout
            dispatched = True
            return SandboxResult(
                0,
                "{}",
                "",
                3,
                "docker",
                outcome_unknown=adversary == "unknown",
            )

    monkeypatch.setattr("ash.coworkers.SandboxManager", FakeSandboxManager)
    if adversary == "owner":
        monkeypatch.setattr("ash.coworkers._current_owner_uid", lambda: os.geteuid() + 1)
        expected_error = CoworkerRuntimeUnavailable
    elif adversary == "mountpoint":
        monkeypatch.setattr(
            "ash.coworkers._linux_mountpoints",
            lambda: frozenset({selected}),
        )
        expected_error = CoworkerDirectoryError
    elif adversary == "traversal":
        expected_error = ValueError
    elif adversary == "unknown":
        expected_error = CoworkerRuntimeUnavailable
    else:
        expected_error = CoworkerDirectoryError

    with pytest.raises(expected_error):
        asyncio.run(
            handle.check_python_syntax(
                run_id=f"syntax-{adversary}",
                permitted_files=("../selected.py",)
                if adversary == "traversal"
                else ("selected.py",),
            )
        )
    assert dispatched is (adversary == "unknown")


def test_coworker_directory_migration_does_not_infer_owner_authority(
    tmp_path: Path,
) -> None:
    import sqlite3
    from uuid import uuid4

    from ash.coworkers import (
        CoworkerDirectory,
        CoworkerHandle,
        CoworkerRuntimeUnavailable,
    )

    ash_dir = tmp_path / "ash"
    ash_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = ash_dir / "coworkers.sqlite3"
    coworker_id = str(uuid4())
    canonical_session_id = str(uuid4())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE coworker_directory_meta (schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO coworker_directory_meta(schema_version) VALUES (1)"
        )
        connection.execute(
            """
            CREATE TABLE coworkers (
                coworker_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                profile_name TEXT NOT NULL UNIQUE,
                workspace_path TEXT NOT NULL,
                canonical_session_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('provisioning', 'ready')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO coworkers (
                coworker_id, owner_id, display_name, profile_name, workspace_path,
                canonical_session_id, state, created_at, updated_at
            ) VALUES (?, 'owner-label', 'Old coworker', 'alpha', ?, ?, 'ready', 'now', 'now')
            """,
            (coworker_id, str(workspace), canonical_session_id),
        )

    directory = CoworkerDirectory.open(ash_dir)
    identity = directory._get_by_profile("alpha")
    assert identity is not None
    assert identity.owner_uid is None
    with pytest.raises(CoworkerRuntimeUnavailable):
        directory._require_local_coworker_owner(CoworkerHandle(directory, identity))

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM coworker_directory_meta"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT owner_uid FROM coworkers WHERE coworker_id = ?",
            (coworker_id,),
        ).fetchone() == (None,)
