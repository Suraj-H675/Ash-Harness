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
