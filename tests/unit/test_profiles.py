from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_profile_names_reject_path_traversal_and_normalize_case() -> None:
    from ash.profiles import validate_profile_name

    assert validate_profile_name(" Work_Profile ") == "work_profile"
    assert validate_profile_name("default") == "default"
    with pytest.raises(ValueError, match="profile name"):
        validate_profile_name("../secrets")
    with pytest.raises(ValueError, match="profile name"):
        validate_profile_name("profile/name")


def test_profile_selection_is_atomic_and_lists_default_first(tmp_path: Path) -> None:
    from ash.profiles import (
        active_profile_name,
        list_profile_names,
        profile_directory,
        set_active_profile,
    )

    (tmp_path / "profiles" / "work").mkdir(parents=True)
    (tmp_path / "profiles" / "bad name").mkdir(parents=True)

    assert list_profile_names(ash_dir=tmp_path) == ("default", "work")
    assert active_profile_name(environ={}, ash_dir=tmp_path) == "default"
    set_active_profile("WORK", ash_dir=tmp_path)
    assert active_profile_name(environ={}, ash_dir=tmp_path) == "work"
    assert (tmp_path / "active-profile").read_text() == "work\n"
    assert profile_directory("work", ash_dir=tmp_path) == tmp_path / "profiles" / "work"


def test_profile_inventory_has_a_bounded_directory_scan(tmp_path: Path, monkeypatch) -> None:
    import ash.profiles as profiles_module

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    for name in ("one", "two", "three"):
        (profiles / name).mkdir()
    monkeypatch.setattr(profiles_module, "MAX_PROFILE_ENTRIES", 2)

    with pytest.raises(ValueError, match="exceeds 2 entries"):
        profiles_module.list_profile_names(ash_dir=tmp_path)


def test_active_profile_marker_symlink_is_not_followed(tmp_path: Path) -> None:
    from ash.profiles import active_profile_name

    target = tmp_path / "outside-marker"
    target.write_text("work\n", encoding="utf-8")
    marker = tmp_path / "active-profile"
    try:
        marker.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlinked active Ash profile marker"):
        active_profile_name(environ={}, ash_dir=tmp_path)


def test_active_profile_read_rejects_state_root_swapped_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.profiles as profiles_module

    home = tmp_path / "home"
    state = home / ".ash"
    state.mkdir(parents=True)
    (state / "active-profile").write_text("default\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "active-profile").write_text("work\n", encoding="utf-8")
    real_state_root = profiles_module._state_root
    swapped = False

    def state_root_then_swap(ash_dir=None):
        nonlocal swapped
        root = real_state_root(ash_dir)
        if not swapped:
            swapped = True
            state.rename(home / ".ash-real")
            try:
                state.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return root

    monkeypatch.setattr(profiles_module, "_state_root", state_root_then_swap)

    with pytest.raises((OSError, ValueError)):
        profiles_module.active_profile_name(environ={}, ash_dir=state)
    assert swapped is True


def test_active_profile_write_rejects_state_root_swapped_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.profiles as profiles_module

    home = tmp_path / "home"
    state = home / ".ash"
    state.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "active-profile"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    real_state_root = profiles_module._state_root
    swapped = False

    def state_root_then_swap(ash_dir=None):
        nonlocal swapped
        root = real_state_root(ash_dir)
        if not swapped:
            swapped = True
            state.rename(home / ".ash-real")
            try:
                state.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return root

    monkeypatch.setattr(profiles_module, "_state_root", state_root_then_swap)

    with pytest.raises((OSError, ValueError)):
        profiles_module.set_active_profile("work", ash_dir=state)
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_profile_commands_keep_credentials_out_of_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from ash.commands.profile import add_profile, render_profile_list
    from ash.profiles import profile_directory, set_active_profile

    assert add_profile("work") == "work"
    directory = profile_directory("work")
    directory.joinpath(".env").write_text(
        "ASH_MODEL=ollama/private-model\nOPENAI_API_KEY=secret-value\n",
        encoding="utf-8",
    )
    set_active_profile("work")

    rendered = render_profile_list(json_output=True)
    payload = json.loads(rendered)

    assert payload["active"] == "work"
    assert payload["profiles"][1]["model"] == "ollama/private-model"
    assert "secret-value" not in rendered


def test_profile_human_rendering_sanitizes_model_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from ash.commands.profile import add_profile, render_profile_list, render_profile_show
    from ash.profiles import profile_directory

    assert add_profile("work") == "work"
    profile_directory("work").joinpath(".env").write_text(
        "ASH_MODEL=openrouter/model\x1b[2J\u202ehidden\u202c\n",
        encoding="utf-8",
    )

    rendered = render_profile_list()
    shown = render_profile_show("work")

    for output in (rendered, shown):
        assert "openrouter/model\\x1b[2J\\u202ehidden\\u202c" in output
        assert "\x1b[2J" not in output
        assert "\u202e" not in output


def test_profile_state_is_used_by_config_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    monkeypatch.delenv("ASH_MODEL", raising=False)
    from ash.commands.profile import add_profile, use_profile
    from ash.config import AshConfig
    from ash.profiles import profile_directory

    add_profile("work")
    use_profile("work")
    profile_directory("work").joinpath(".env").write_text(
        "ASH_MODEL=ollama/profile-model\n", encoding="utf-8"
    )

    config = AshConfig.load()

    assert config.model == "ollama/profile-model"
    assert config.config_source("model") == (
        "dotenv",
        f"ASH_MODEL in {profile_directory('work') / '.env'}",
    )


def test_missing_profile_fails_before_loading_default_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_PROFILE", "missing")
    from ash.config import AshConfig

    with pytest.raises(ValueError, match="profile does not exist"):
        AshConfig.load()


def test_symlinked_profile_cannot_redirect_config_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    profiles = home / ".ash" / "profiles"
    outside = tmp_path / "outside-profile"
    profiles.mkdir(parents=True)
    outside.mkdir()
    outside.joinpath(".env").write_text(
        "ASH_MODEL=ollama/outside-model\n",
        encoding="utf-8",
    )
    try:
        profiles.joinpath("work").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASH_PROFILE", "work")
    monkeypatch.delenv("ASH_MODEL", raising=False)

    from ash.config import AshConfig
    from ash.profiles import profile_exists

    assert profile_exists("work") is False
    with pytest.raises(ValueError, match="profile does not exist"):
        AshConfig.load()


def test_symlinked_profiles_root_cannot_redirect_profile_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    ash_dir = home / ".ash"
    outside = tmp_path / "outside"
    ash_dir.mkdir(parents=True)
    outside.mkdir()
    try:
        (ash_dir / "profiles").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))

    from ash.commands.profile import add_profile, remove_profile

    with pytest.raises(ValueError, match="symlinked profiles directory"):
        add_profile("work")
    assert not (outside / "work").exists()

    victim = outside / "work"
    victim.mkdir()
    victim.joinpath("sentinel.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="symlinked profiles directory"):
        remove_profile("work", confirmed=True)
    assert victim.joinpath("sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_profile_add_rejects_profiles_root_swapped_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.profile as profile_module

    home = tmp_path / "home"
    base = home / ".ash"
    profiles = base / "profiles"
    profiles.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_profile_directory = profile_module.profile_directory
    swapped = False

    def directory_then_swap(name, *, ash_dir=None):
        nonlocal swapped
        directory = real_profile_directory(name, ash_dir=ash_dir)
        if not swapped:
            swapped = True
            profiles.rename(base / "profiles-real")
            try:
                profiles.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(profile_module, "profile_directory", directory_then_swap)

    with pytest.raises((OSError, ValueError)):
        profile_module.add_profile("work")
    assert swapped is True
    assert not (outside / "work").exists()


def test_profile_remove_rejects_profiles_root_swapped_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.profile as profile_module

    home = tmp_path / "home"
    base = home / ".ash"
    profiles = base / "profiles"
    local_work = profiles / "work"
    local_work.mkdir(parents=True)
    (local_work / "local.txt").write_text("local\n", encoding="utf-8")
    outside = tmp_path / "outside"
    victim = outside / "work"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("DO NOT DELETE\n", encoding="utf-8")
    real_active = profile_module.active_profile_name
    swapped = False

    def active_then_swap(*args, **kwargs):
        nonlocal swapped
        active = real_active(*args, **kwargs)
        if not swapped:
            swapped = True
            profiles.rename(base / "profiles-real")
            try:
                profiles.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return active

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(profile_module, "active_profile_name", active_then_swap)

    with pytest.raises((OSError, ValueError)):
        profile_module.remove_profile("work", confirmed=True)
    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "DO NOT DELETE\n"


def test_symlinked_user_state_root_cannot_redirect_profile_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    outside.joinpath(".env").write_text(
        "ASH_MODEL=ollama/outside-model\n", encoding="utf-8"
    )
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    monkeypatch.delenv("ASH_MODEL", raising=False)

    from ash.config import AshConfig

    with pytest.raises(ValueError, match="symlinked Ash state directory"):
        AshConfig.load()
