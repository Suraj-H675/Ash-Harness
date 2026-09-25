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
    real_open = profiles_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state and not swapped:
            swapped = True
            state.rename(home / ".ash-real")
            try:
                state.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        profiles_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

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
    real_open = profiles_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state and not swapped:
            swapped = True
            state.rename(home / ".ash-real")
            try:
                state.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        profiles_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profiles_module.set_active_profile("work", ash_dir=state)
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_active_profile_read_rejects_plain_state_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.profiles as profiles_module

    home = tmp_path / "home"
    state = home / ".ash"
    saved = home / ".ash-original"
    replacement = tmp_path / "replacement"
    state.mkdir(parents=True)
    replacement.mkdir()
    (state / "active-profile").write_text("default\n", encoding="utf-8")
    (replacement / "active-profile").write_text("work\n", encoding="utf-8")
    real_open = profiles_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state and not swapped:
            swapped = True
            state.rename(saved)
            replacement.rename(state)
        return directory

    monkeypatch.setattr(
        profiles_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profiles_module.active_profile_name(environ={}, ash_dir=state)

    assert swapped is True


def test_active_profile_write_rejects_plain_state_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.profiles as profiles_module

    home = tmp_path / "home"
    state = home / ".ash"
    saved = home / ".ash-original"
    replacement = tmp_path / "replacement"
    state.mkdir(parents=True)
    replacement.mkdir()
    victim = replacement / "active-profile"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    real_open = profiles_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state and not swapped:
            swapped = True
            state.rename(saved)
            replacement.rename(state)
        return directory

    monkeypatch.setattr(
        profiles_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profiles_module.set_active_profile("work", ash_dir=state)

    assert swapped is True
    assert (state / "active-profile").read_text(encoding="utf-8") == "do-not-touch\n"
    assert not (saved / "active-profile").exists()


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


def test_removing_active_profile_resets_marker_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    from ash.commands.profile import add_profile, remove_profile, use_profile
    from ash.profiles import active_profile_name, profile_exists

    assert add_profile("work") == "work"
    assert use_profile("work") == "work"
    assert active_profile_name(environ={}) == "work"

    assert remove_profile("work", confirmed=True) == "work"

    assert active_profile_name(environ={}) == "default"
    assert profile_exists("work") is False


def test_remove_missing_profile_reports_missing_before_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from ash.commands.profile import remove_profile

    with pytest.raises(ValueError, match="profile does not exist: work"):
        remove_profile("work", confirmed=False)


def test_active_profile_marker_write_failure_prevents_profile_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    import ash.commands.profile as profile_module
    from ash.commands.profile import add_profile, use_profile
    from ash.profiles import active_profile_name, profile_exists

    add_profile("work")
    use_profile("work")

    def fail_marker_write(*args, **kwargs):
        raise OSError("marker write failed")

    monkeypatch.setattr(
        profile_module,
        "_write_active_profile_to_directory",
        fail_marker_write,
    )

    with pytest.raises(OSError, match="marker write failed"):
        profile_module.remove_profile("work", confirmed=True)

    assert profile_exists("work") is True
    assert active_profile_name(environ={}) == "work"


def test_active_profile_delete_failure_leaves_marker_at_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    import ash.commands.profile as profile_module
    from ash.commands.profile import add_profile, use_profile
    from ash.profiles import active_profile_name, profile_exists

    add_profile("work")
    use_profile("work")
    real_remove_tree = profile_module.AnchoredDirectory.remove_tree

    def fail_profile_delete(self, name, **kwargs):
        if name == "work":
            raise profile_module.AnchoredFilesystemError("delete failed")
        return real_remove_tree(self, name, **kwargs)

    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "remove_tree",
        fail_profile_delete,
    )

    with pytest.raises(ValueError, match="cannot remove profile work: delete failed"):
        profile_module.remove_profile("work", confirmed=True)

    assert active_profile_name(environ={}) == "default"
    assert profile_exists("work") is True


def test_removing_inactive_profile_keeps_active_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ASH_PROFILE", raising=False)
    from ash.commands.profile import add_profile, remove_profile, use_profile
    from ash.profiles import active_profile_name, profile_exists

    add_profile("work")
    add_profile("personal")
    use_profile("personal")

    assert remove_profile("work", confirmed=True) == "work"

    assert active_profile_name(environ={}) == "personal"
    assert profile_exists("work") is False
    assert profile_exists("personal") is True


def test_remove_profile_preserves_ash_profile_override_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from ash.commands.profile import add_profile, remove_profile
    from ash.profiles import active_profile_name, profile_exists

    add_profile("work")
    monkeypatch.setenv("ASH_PROFILE", "work")

    assert remove_profile("work", confirmed=True) == "work"

    assert active_profile_name(environ={}) == "default"
    assert active_profile_name() == "work"
    assert profile_exists("work") is False


def test_profile_remove_rejects_named_profile_replacement_after_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.profile as profile_module

    home = tmp_path / "home"
    base = home / ".ash"
    profiles = base / "profiles"
    original = profiles / "work"
    moved = profiles / "work-original"
    replacement = tmp_path / "replacement-work"
    original.mkdir(parents=True)
    original.joinpath("local.txt").write_text("local\n", encoding="utf-8")
    replacement.mkdir()
    sentinel = replacement / "sentinel.txt"
    sentinel.write_text("DO NOT DELETE\n", encoding="utf-8")
    real_child = profile_module.AnchoredDirectory.child
    swapped = False

    def child_then_swap(self, name, **kwargs):
        nonlocal swapped
        child = real_child(self, name, **kwargs)
        if self.path == profiles and name == "work" and not swapped:
            swapped = True
            original.rename(moved)
            replacement.rename(original)
        return child

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "child",
        child_then_swap,
    )

    with pytest.raises(ValueError, match="profile changed before removal"):
        profile_module.remove_profile("work", confirmed=True)

    assert swapped is True
    assert (original / "sentinel.txt").read_text(encoding="utf-8") == (
        "DO NOT DELETE\n"
    )
    assert (moved / "local.txt").read_text(encoding="utf-8") == "local\n"


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
    real_open = profile_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == profiles and not swapped:
            swapped = True
            profiles.rename(base / "profiles-real")
            try:
                profiles.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profile_module.add_profile("work")
    assert swapped is True
    assert not (outside / "work").exists()


def test_profile_add_rejects_plain_profiles_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.profile as profile_module

    home = tmp_path / "home"
    base = home / ".ash"
    profiles = base / "profiles"
    saved = base / "profiles-original"
    replacement = tmp_path / "replacement"
    profiles.mkdir(parents=True)
    replacement.mkdir()
    real_open = profile_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == profiles and not swapped:
            swapped = True
            profiles.rename(saved)
            replacement.rename(profiles)
        return directory

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profile_module.add_profile("work")

    assert swapped is True
    assert not (profiles / "work").exists()
    assert not (saved / "work").exists()


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
    real_open = profile_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == profiles and not swapped:
            swapped = True
            profiles.rename(base / "profiles-real")
            try:
                profiles.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profile_module.remove_profile("work", confirmed=True)
    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "DO NOT DELETE\n"


def test_profile_remove_rejects_plain_profiles_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.profile as profile_module

    home = tmp_path / "home"
    base = home / ".ash"
    profiles = base / "profiles"
    saved = base / "profiles-original"
    local_work = profiles / "work"
    local_work.mkdir(parents=True)
    (local_work / "local.txt").write_text("local\n", encoding="utf-8")
    replacement = tmp_path / "replacement"
    victim = replacement / "work"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("DO NOT DELETE\n", encoding="utf-8")
    real_open = profile_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == profiles and not swapped:
            swapped = True
            profiles.rename(saved)
            replacement.rename(profiles)
        return directory

    monkeypatch.setattr(profile_module, "_base_ash_directory", lambda: base)
    monkeypatch.setattr(
        profile_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises((OSError, ValueError)):
        profile_module.remove_profile("work", confirmed=True)

    assert swapped is True
    assert (profiles / "work" / "sentinel.txt").read_text(encoding="utf-8") == (
        "DO NOT DELETE\n"
    )
    assert (saved / "work" / "local.txt").read_text(encoding="utf-8") == "local\n"


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
