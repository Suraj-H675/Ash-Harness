from __future__ import annotations

import os
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
    from ash.sdk import AshClient
    from ash.ui.headless import HeadlessUI

    ash_dir = _isolated_ash_home(tmp_path, monkeypatch)
    _profile(ash_dir, "alpha")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
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

        asyncio.run(AshClient.create(config=handle.effective_config()))
    assert HeadlessUI().request_tool_approval("write_file", {}) is False
