from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

import pytest

from ash.cli import main
from ash.coworkers import CoworkerDirectory
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.messages import MessageInput
from ash.providers.registry import ProviderRegistry
from ash.safety.guard import SafetyGuard


def _prepare_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
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
    profiles = {
        "atlas": "atlas-profile-secret",
        "scout": "scout-profile-secret",
    }
    for name, secret in profiles.items():
        profile = ash_dir / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "ash.toml").write_text(
            f"model = 'openai/gpt-{name}'\nopenai_auth_mode = 'api_key'\n",
            encoding="utf-8",
        )
        (profile / ".env").write_text(
            f"OPENAI_API_KEY={secret}\n",
            encoding="utf-8",
        )
    atlas_workspace = tmp_path / "atlas-workspace"
    scout_workspace = tmp_path / "scout-workspace"
    atlas_workspace.mkdir()
    scout_workspace.mkdir()
    return ash_dir, atlas_workspace, scout_workspace


def _install_fake_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ProviderRegistry, list[dict[str, Any]]]:
    observed: list[dict[str, Any]] = []

    class FakeProvider(ProviderABC):
        provider_family = "openai"

        def __init__(self, model: str, key: str) -> None:
            self._model = model
            self._api_key = key

        @property
        def model_name(self) -> str:
            return self._model

        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def configure_max_tokens(self, max_tokens: int) -> None:
            assert max_tokens <= 512

        async def stream_chat(
            self,
            messages: Sequence[MessageInput],
            temperature: float = 0.0,
            tools: list[dict[str, object]] | None = None,
        ) -> AsyncGenerator[StreamChunk, None]:
            del temperature
            get_stdout = getattr(sys.stdout, "getvalue", None)
            captured_output = get_stdout() if callable(get_stdout) else ""
            observed.append(
                {
                    "model": self.model_name,
                    "key": self._api_key,
                    "messages": list(messages),
                    "tools": tools,
                    "turn_id_reported_before_dispatch": "Turn ID:" in captured_output,
                }
            )
            if self.model_name.endswith("atlas"):
                answer = "Atlas found the failed assertion and proposes a fix. "
                answer += "atlas-profile-secret \u001b[31mred\u001b[0m\u202e"
            else:
                answer = "Scout found an independent research note. scout-profile-secret"
            yield StreamChunk(content=answer)
            yield StreamChunk(is_done=True)

    registry = ProviderRegistry()

    def build_provider(_config: object, model: str) -> ProviderABC:
        return FakeProvider(model, "")

    def build_profile_provider(
        _config: object,
        model: str,
        environment: object,
    ) -> ProviderABC:
        assert isinstance(environment, dict)
        key = environment.get("OPENAI_API_KEY")
        assert isinstance(key, str)
        return FakeProvider(model, key)

    registry.register("openai", build_provider)
    registry._register_profile_factory(
        "openai",
        build_provider,
        build_profile_provider,
    )
    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        lambda: registry,
    )
    return registry, observed


def _ask_args(
    coworker_id: str,
    question: str,
    *evidence: str,
    turn_id: str = "",
    owner: str = "local",
) -> list[str]:
    args = [
        "crew",
        "ask",
        "--owner",
        owner,
        "--coworker-id",
        coworker_id,
        "--question",
        question,
    ]
    if turn_id:
        args.extend(["--turn-id", turn_id])
    for path in evidence:
        args.extend(["--evidence", path])
    return args


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_cli_runs_atlas_and_scout_with_isolated_profiles_evidence_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, atlas_workspace, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    ci_log = atlas_workspace / "ci-failure.txt"
    source = atlas_workspace / "job.py"
    research = scout_workspace / "research-note.txt"
    ci_log.write_text("pytest failed: expected 2 tests, got 1", encoding="utf-8")
    source.write_text("assert test_count == 2\n", encoding="utf-8")
    research.write_text("Synthetic note: Atlas has no access to this file.", encoding="utf-8")

    assert main(
        [
            "crew",
            "create",
            "--owner",
            "local",
            "--name",
            "Atlas",
            "--profile",
            "atlas",
            "--workspace",
            str(atlas_workspace),
        ]
    ) == 0
    atlas_created = capsys.readouterr().out
    atlas_id = next(line.removeprefix("ID: ") for line in atlas_created.splitlines() if line.startswith("ID: "))
    assert main(
        [
            "crew",
            "create",
            "--owner",
            "local",
            "--name",
            "Scout",
            "--profile",
            "scout",
            "--workspace",
            str(scout_workspace),
        ]
    ) == 0
    scout_created = capsys.readouterr().out
    scout_id = next(line.removeprefix("ID: ") for line in scout_created.splitlines() if line.startswith("ID: "))

    assert main(["crew", "list", "--owner", "local"]) == 0
    listed = capsys.readouterr().out
    assert atlas_id in listed
    assert scout_id in listed
    assert "gpt-atlas" not in listed
    assert "profile-secret" not in listed

    monkeypatch.setenv("ASH_PROFILE", "scout")
    assert main(
        _ask_args(
            atlas_id,
            "Explain this synthetic CI failure and propose a fix.",
            "ci-failure.txt",
            "job.py",
            turn_id="atlas-ci-demo-1",
        )
    ) == 0
    atlas_output = capsys.readouterr().out
    assert "Turn ID: atlas-ci-demo-1" in atlas_output
    assert f"SHA-256 {hashlib.sha256(ci_log.read_bytes()).hexdigest()}" in atlas_output
    assert "Proposal only. No files were changed." in atlas_output
    assert "Atlas found the failed assertion" in atlas_output
    assert "atlas-profile-secret" not in atlas_output
    assert "\u001b" not in atlas_output
    assert "\u202e" not in atlas_output

    monkeypatch.setenv("ASH_PROFILE", "atlas")
    assert main(
        _ask_args(
            scout_id,
            "Answer from the listed research note.",
            "research-note.txt",
            turn_id="scout-research-demo-1",
        )
    ) == 0
    scout_output = capsys.readouterr().out
    assert "Scout found an independent research note." in scout_output
    assert "atlas-profile-secret" not in scout_output
    assert "scout-profile-secret" not in scout_output

    assert [item["key"] for item in observed] == [
        "atlas-profile-secret",
        "scout-profile-secret",
    ]
    assert [item["tools"] for item in observed] == [None, None]
    assert all(item["turn_id_reported_before_dispatch"] for item in observed)
    assert len(observed) == 2

    atlas_message = next(
        item["content"]
        for item in observed[0]["messages"]
        if item["role"] == "user"
    )
    scout_message = next(
        item["content"]
        for item in observed[1]["messages"]
        if item["role"] == "user"
    )
    atlas_payload = json.loads(atlas_message.split("Untrusted request data:\n", 1)[1])
    scout_payload = json.loads(scout_message.split("Untrusted request data:\n", 1)[1])
    atlas_evidence = json.loads(atlas_payload["evidence"])
    scout_evidence = json.loads(scout_payload["evidence"])
    assert [item["path"] for item in atlas_evidence] == ["ci-failure.txt", "job.py"]
    assert atlas_evidence[0]["sha256"] == hashlib.sha256(ci_log.read_bytes()).hexdigest()
    assert atlas_evidence[0]["size_bytes"] == ci_log.stat().st_size
    assert "Synthetic note" not in atlas_payload["evidence"]
    assert "expected 2 tests" in atlas_payload["evidence"]
    assert [item["path"] for item in scout_evidence] == ["research-note.txt"]
    assert "expected 2 tests" not in scout_payload["evidence"]
    assert "Synthetic note" in scout_payload["evidence"]

    atlas_handle = CoworkerDirectory.open().open_coworker(
        owner_id="local",
        coworker_id=atlas_id,
    )
    scout_handle = CoworkerDirectory.open().open_coworker(
        owner_id="local",
        coworker_id=scout_id,
    )
    atlas_history = atlas_handle.list_readonly_turn_history()
    scout_history = scout_handle.list_readonly_turn_history()
    assert len(atlas_history) == len(scout_history) == 1
    assert atlas_history[0].session_ref.coworker_id == atlas_id
    assert scout_history[0].session_ref.coworker_id == scout_id
    assert atlas_history[0].session_ref.session_id != scout_history[0].session_ref.session_id

    assert main(["crew", "history", "--owner", "local", "--coworker-id", atlas_id]) == 0
    history = capsys.readouterr().out
    assert "Status: completed" in history
    assert atlas_history[0].session_ref.run_id in history
    assert "Atlas found the failed assertion" in history

    assert main(
        _ask_args(
            atlas_id,
            "Retry must be refused.",
            "ci-failure.txt",
            turn_id="atlas-ci-demo-1",
        )
    ) == 2
    duplicate = capsys.readouterr()
    assert "Turn ID: atlas-ci-demo-1" in duplicate.out
    assert "non-replayable" in duplicate.err
    assert len(observed) == 2

    assert main(_ask_args(atlas_id, "Wrong owner must fail.", owner="foreign")) == 2
    assert "no coworker matches" in capsys.readouterr().err
    assert len(observed) == 2

    with pytest.raises(SystemExit):
        main(["--profile", "scout", "crew", "list", "--owner", "local"])
    assert len(observed) == 2


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_rejected_evidence_never_dispatches_fake_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    valid = workspace / "valid.txt"
    valid.write_text("safe evidence", encoding="utf-8")
    identity = CoworkerDirectory.open().create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=workspace,
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "large.txt").write_bytes(b"x" * (6 * 1024 + 1))
    (workspace / "combined-a.txt").write_bytes(b"a" * (5 * 1024))
    (workspace / "combined-b.txt").write_bytes(b"b" * (5 * 1024))
    (workspace / "binary.bin").write_bytes(b"\x00\xff")
    (workspace / "secret-note.txt").write_text(
        "api_key='abcdefghijklmnop123456789'", encoding="utf-8"
    )
    (workspace / "ordinary-note.txt").write_text(
        "Documented config: api_key='abcdefghijklmnop123456789'",
        encoding="utf-8",
    )
    (workspace / ".aws").mkdir()
    (workspace / ".aws" / "credentials").write_text("private", encoding="utf-8")
    (workspace / "id_rsa").write_text("private", encoding="utf-8")
    directory = workspace / "not-a-file"
    directory.mkdir()
    symlink = workspace / "symlink.txt"
    try:
        symlink.symlink_to(valid)
    except OSError:
        symlink = None
    hardlink = workspace / "hardlink.txt"
    hardlink_source = workspace / "hardlink-source.txt"
    hardlink_source.write_text("independent hardlink evidence", encoding="utf-8")
    try:
        os.link(hardlink_source, hardlink)
    except (OSError, NotImplementedError):
        hardlink = None

    rejected_paths = [
        str(valid),
        "../outside.txt",
        "C:\\repo\\file.txt",
        "\\\\server\\share\\file.txt",
        ".aws/credentials",
        "id_rsa",
        "large.txt",
        "binary.bin",
        "secret-note.txt",
        "ordinary-note.txt",
        "not-a-file",
        "missing.txt",
        "valid.txt",
    ]
    if symlink is not None:
        rejected_paths.append("symlink.txt")
    if hardlink is not None:
        rejected_paths.append("hardlink.txt")

    for index, path in enumerate(rejected_paths):
        if path == "valid.txt":
            from ash.safety import scoped_io

            if scoped_io._supports_anchored_io():
                real_read_all = scoped_io._read_all

                def mutate_after_read(
                    fd: int,
                    *,
                    max_bytes: int | None = None,
                ) -> bytes:
                    content = real_read_all(fd, max_bytes=max_bytes)
                    valid.write_text(
                        "changed while the snapshot was read",
                        encoding="utf-8",
                    )
                    return content

                monkeypatch.setattr(scoped_io, "_read_all", mutate_after_read)
            else:
                real_fallback_read = scoped_io._fallback_read

                def mutate_after_fallback_read(
                    target: Path,
                    guard: SafetyGuard,
                    *,
                    max_bytes: int | None = None,
                    require_single_link: bool = False,
                ) -> bytes:
                    content = real_fallback_read(
                        target,
                        guard,
                        max_bytes=max_bytes,
                        require_single_link=require_single_link,
                    )
                    valid.write_text(
                        "changed while the snapshot was read",
                        encoding="utf-8",
                    )
                    return content

                monkeypatch.setattr(
                    scoped_io,
                    "_fallback_read",
                    mutate_after_fallback_read,
                )
        assert main(
            _ask_args(
                identity.coworker_id,
                "Inspect the supplied evidence.",
                path,
                turn_id=f"rejected-evidence-{index}",
            )
        ) == 2
        result = capsys.readouterr()
        assert f"Turn ID: rejected-evidence-{index}" in result.out
        assert "Blocked before model dispatch" in result.err
        assert "changed while the snapshot was read" not in result.err
        assert len(observed) == 0

    assert main(
        _ask_args(
            identity.coworker_id,
            "Combined evidence exceeds the bounded evidence budget.",
            "combined-a.txt",
            "combined-b.txt",
            turn_id="combined-evidence-limit",
        )
    ) == 2
    combined = capsys.readouterr()
    assert "combined-evidence-limit" in combined.out
    assert "combined evidence exceeds" in combined.err
    assert len(observed) == 0


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_crew_commands_work_without_default_model_and_report_missing_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    evidence = workspace / "failure.log"
    evidence.write_text("synthetic failure", encoding="utf-8")
    identity = CoworkerDirectory.open().create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=workspace,
    )

    assert not (Path.home() / ".ash" / "ash.toml").exists()
    with pytest.raises(SystemExit) as help_exit:
        main(["crew", "--help"])
    assert help_exit.value.code == 0
    help_output = capsys.readouterr().out
    for action in ("create", "list", "ask", "history"):
        assert action in help_output
    assert main(["crew", "list", "--owner", "local"]) == 0
    capsys.readouterr()
    assert main(
        _ask_args(identity.coworker_id, "Explain the failure.", "failure.log")
    ) == 0
    generated_turn = capsys.readouterr().out
    assert "Turn ID: " in generated_turn
    assert len(observed) == 1
    assert observed[0]["turn_id_reported_before_dispatch"] is True

    from ash.providers.registry import ProviderRegistry

    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        ProviderRegistry,
    )
    assert main(
        _ask_args(
            identity.coworker_id,
            "No configured provider must block.",
            "failure.log",
            turn_id="missing-provider-turn",
        )
    ) == 2
    blocked = capsys.readouterr()
    assert "Turn ID: missing-provider-turn" in blocked.out
    assert "Blocked." in blocked.err
    assert "provider is unavailable" in blocked.err
    assert "No provider request was sent" in blocked.err
    assert len(observed) == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_changed_profile_binding_blocks_before_evidence_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    evidence = workspace / "failure.log"
    evidence.write_text("synthetic failure", encoding="utf-8")
    directory = CoworkerDirectory.open()
    identity = directory.create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=workspace,
    )
    handle = directory.open_coworker(
        owner_id="local",
        coworker_id=identity.coworker_id,
    )
    with sqlite3.connect(directory.db_path) as connection:
        connection.execute(
            "UPDATE coworkers SET profile_name = 'scout' WHERE coworker_id = ?",
            (identity.coworker_id,),
        )

    monkeypatch.setattr(
        CoworkerDirectory,
        "open_coworker",
        lambda _directory, **_kwargs: handle,
    )

    def evidence_read_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("evidence read happened before profile binding validation")

    monkeypatch.setattr(
        "ash.commands.crew.snapshot_scoped_file",
        evidence_read_must_not_run,
    )
    assert main(
        _ask_args(
            identity.coworker_id,
            "The stale profile binding must be refused.",
            "failure.log",
            turn_id="stale-profile-binding",
        )
    ) == 2
    result = capsys.readouterr()
    assert "Turn ID: stale-profile-binding" in result.out
    assert "process binding changed" in result.err
    assert not observed


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_crew_model_history_excludes_unrelated_offline_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    _, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    directory = CoworkerDirectory.open()
    identity = directory.create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=workspace,
    )
    handle = directory.open_coworker(
        owner_id="local",
        coworker_id=identity.coworker_id,
    )
    offline_run_id = "synthetic-offline-worker-claim"
    ref = handle.begin_background_run(offline_run_id)
    directory._claim_offline_turn(
        handle=handle,
        run_id=offline_run_id,
        session_id=ref.session_id,
        request_id=str(uuid4()),
    )
    assert handle.list_readonly_turn_history() == ()


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_list_hides_records_bound_to_another_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, atlas_workspace, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    directory = CoworkerDirectory.open()
    atlas = directory.create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=atlas_workspace,
    )
    scout = directory.create(
        owner_id="local",
        display_name="Scout",
        profile_name="scout",
        workspace=scout_workspace,
    )
    with sqlite3.connect(directory.db_path) as connection:
        connection.execute(
            "UPDATE coworkers SET owner_uid = ? WHERE coworker_id = ?",
            (os.geteuid() + 1, scout.coworker_id),
        )

    assert main(["crew", "list", "--owner", "local"]) == 0
    listed = capsys.readouterr().out
    assert atlas.coworker_id in listed
    assert scout.coworker_id not in listed
