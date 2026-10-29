from __future__ import annotations

import hashlib
import io
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
    *,
    atlas_answer: str = "Atlas found the failed assertion and proposes a fix. ",
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
                answer = atlas_answer
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


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_atlas_file_approval_is_durable_exact_and_single_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, atlas_workspace, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(
        monkeypatch,
        atlas_answer="APPROVED: the fix should change VALUE to 2. ",
    )
    (atlas_workspace / "src").mkdir()
    (scout_workspace / "src").mkdir()
    (atlas_workspace / "ci-failure.txt").write_text(
        "controlled test failed: expected VALUE=2", encoding="utf-8"
    )
    target = atlas_workspace / "src" / "example.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    replacement = atlas_workspace / "src" / "example.fixed.py"
    replacement.write_text("VALUE = 2\n", encoding="utf-8")
    scout_target = scout_workspace / "src" / "example.py"
    scout_target.write_text("SCOUT = True\n", encoding="utf-8")

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
    atlas_id = next(
        line.split(": ", 1)[1]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("ID: ")
    )
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
    scout_id = next(
        line.split(": ", 1)[1]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("ID: ")
    )

    assert main(
        _ask_args(
            atlas_id,
            "Inspect this controlled CI failure and suggest the correction.",
            "ci-failure.txt",
            turn_id="bounded-ci-fix",
        )
    ) == 0
    ask_output = capsys.readouterr().out
    assert "APPROVED" in ask_output
    assert "Proposal only. No files were changed." in ask_output
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    source_ids = {}
    for label in ("Run ID", "Session ID", "Request ID"):
        source_ids[label] = next(
            line.split(": ", 1)[1]
            for line in ask_output.splitlines()
            if line.startswith(f"{label}: ")
        )

    assert main(
        [
            "crew",
            "propose",
            "--owner",
            "local",
            "--coworker-id",
            atlas_id,
            "--run-id",
            source_ids["Run ID"],
            "--session-id",
            source_ids["Session ID"],
            "--request-id",
            source_ids["Request ID"],
            "--target",
            "src/example.py",
            "--replacement-file",
            "src/example.fixed.py",
        ]
    ) == 0
    proposal_output = capsys.readouterr().out
    assert "Status: pending" in proposal_output
    assert "Expected prior SHA-256" in proposal_output
    assert "Proposed diff:" in proposal_output
    assert "VALUE = 2" in proposal_output
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    approval_id = next(
        line.split(": ", 1)[1]
        for line in proposal_output.splitlines()
        if line.startswith("Approval ID: ")
    )
    approval_digest = next(
        line.split(": ", 1)[1]
        for line in proposal_output.splitlines()
        if line.startswith("Approval digest: ")
    )
    assert main(
        [
            "crew",
            "approval",
            "show",
            "--owner",
            "someone-else",
            "--coworker-id",
            atlas_id,
            "--approval-id",
            approval_id,
        ]
    ) == 2
    assert "could not be inspected" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    approve_args = [
        "crew",
        "approve",
        "--owner",
        "local",
        "--coworker-id",
        atlas_id,
        "--approval-id",
        approval_id,
    ]
    assert main(approve_args) == 2
    assert "interactive local TTY" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    class TtyOutput:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return self.stream.write(value)

        def flush(self) -> None:
            self.stream.flush()

    captured_stdout = sys.stdout

    def run_tty_action(args: list[str], action: str) -> int:
        monkeypatch.setattr(sys, "stdin", TtyInput(f"{action} {approval_id} {approval_digest}\n"))
        monkeypatch.setattr(sys, "stdout", TtyOutput(captured_stdout))
        return main(args)

    assert run_tty_action(approve_args, "FAKE-APPROVAL") == 2
    assert "exact challenge" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    apply_args = [
        "crew",
        "apply",
        "--owner",
        "local",
        "--coworker-id",
        atlas_id,
        "--approval-id",
        approval_id,
    ]
    assert run_tty_action(apply_args, "APPLY") == 2
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    assert run_tty_action(approve_args, "APPROVE") == 0
    assert "Status: approved." in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    assert run_tty_action(apply_args, "APPLY") == 0
    assert "Status: succeeded." in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert scout_target.read_text(encoding="utf-8") == "SCOUT = True\n"

    directory = CoworkerDirectory.open()
    reopened = directory.open_coworker(owner_id="local", coworker_id=atlas_id)
    approval, events = reopened.inspect_file_approval(approval_id)
    assert approval.status == "succeeded"
    assert approval.replacement_source_path == "src/example.fixed.py"
    assert approval.replacement_sha256 == hashlib.sha256(b"VALUE = 2\n").hexdigest()
    assert [event.status for event in events] == [
        "pending",
        "approved",
        "dispatching",
        "succeeded",
    ]
    assert main(
        [
            "crew",
            "propose",
            "--owner",
            "local",
            "--coworker-id",
            scout_id,
            "--run-id",
            source_ids["Run ID"],
            "--session-id",
            source_ids["Session ID"],
            "--request-id",
            source_ids["Request ID"],
            "--target",
            "src/example.py",
            "--replacement-file",
            "src/example.fixed.py",
        ]
    ) == 2
    assert "Atlas profile-bound coworker" in capsys.readouterr().err
    assert scout_target.read_text(encoding="utf-8") == "SCOUT = True\n"

    assert run_tty_action(apply_args, "APPLY") == 2
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert len(observed) == 1


def _new_pending_file_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    replacement_content: bytes = b"VALUE = 2\n",
    target_content: bytes | None = b"VALUE = 1\n",
) -> tuple[Any, Any, Any, Path, Path, Path, Any, dict[str, str]]:
    from uuid import uuid4

    tmp_path.mkdir(parents=True, exist_ok=True)
    _, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _install_fake_registry(monkeypatch)
    (workspace / "src").mkdir()
    (workspace / "ci-failure.txt").write_text(
        "controlled CI failure", encoding="utf-8"
    )
    target = workspace / "src" / "example.py"
    if target_content is not None:
        target.write_bytes(target_content)
    replacement = workspace / "src" / "example.fixed.py"
    replacement.write_bytes(replacement_content)
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
    turn_id = f"approval-{uuid4()}"
    assert main(
        _ask_args(
            identity.coworker_id,
            "Review the controlled failure and explain its likely correction.",
            "ci-failure.txt",
            turn_id=turn_id,
        )
    ) == 0
    output = capsys.readouterr().out
    source_ids = {
        label: next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith(f"{label}: ")
        )
        for label in ("Run ID", "Session ID", "Request ID")
    }
    approval = handle.prepare_file_approval(
        run_id=source_ids["Run ID"],
        session_id=source_ids["Session ID"],
        request_id=source_ids["Request ID"],
        target_path="src/example.py",
        replacement_file_path="src/example.fixed.py",
    )
    capsys.readouterr()
    return directory, handle, identity, workspace, target, replacement, approval, source_ids


def test_file_approval_rejects_foreign_run_and_linked_or_traversal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from uuid import UUID, uuid4, uuid5

    from ash.coworkers import CoworkerRuntimeUnavailable
    from ash.safety.guard import SafetyViolation
    from ash.safety.scoped_io import ScopedIOError

    directory, handle, _, workspace, target, replacement, approval, source_ids = (
        _new_pending_file_approval(tmp_path, monkeypatch, capsys)
    )
    with pytest.raises(CoworkerRuntimeUnavailable):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=str(uuid4()),
            target_path="src/example.py",
            replacement_file_path="src/example.fixed.py",
        )
    foreign_run_id = str(uuid4())
    with pytest.raises(CoworkerRuntimeUnavailable, match="source must be"):
        handle.prepare_file_approval(
            run_id=foreign_run_id,
            session_id=source_ids["Session ID"],
            request_id=str(uuid5(UUID(foreign_run_id), "request")),
            target_path="src/example.py",
            replacement_file_path="src/example.fixed.py",
        )
    with pytest.raises(CoworkerRuntimeUnavailable, match="source must be"):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=str(uuid4()),
            request_id=source_ids["Request ID"],
            target_path="src/example.py",
            replacement_file_path="src/example.fixed.py",
        )
    with pytest.raises(ValueError, match="traversal"):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=source_ids["Request ID"],
            target_path="../outside.py",
            replacement_file_path="src/example.fixed.py",
        )

    symlink = workspace / "src" / "linked.py"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises((ScopedIOError, SafetyViolation, OSError)):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=source_ids["Request ID"],
            target_path="src/linked.py",
            replacement_file_path="src/example.fixed.py",
        )
    symlink.unlink()
    hardlink = workspace / "src" / "alias.py"
    try:
        os.link(target, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable")
    with pytest.raises(ScopedIOError, match="hard link"):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=source_ids["Request ID"],
            target_path="src/alias.py",
            replacement_file_path="src/example.fixed.py",
        )
    hardlink.unlink()
    replacement_alias = workspace / "src" / "replacement-alias.py"
    os.link(replacement, replacement_alias)
    with pytest.raises((ScopedIOError, CoworkerRuntimeUnavailable), match="hard link"):
        handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=source_ids["Request ID"],
            target_path="src/example.py",
            replacement_file_path="src/replacement-alias.py",
        )
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM coworker_file_approvals"
        ).fetchone() == (1,)
    assert target.read_bytes() == b"VALUE = 1\n"
    assert approval.status == "pending"


@pytest.mark.parametrize(
    "path",
    [
        ".ssh/config",
        ".aws/config",
        ".kube/config",
        ".docker/config.json",
        ".gnupg/gpg.conf",
        ".config/app/settings",
        ".azure/settings",
        ".gcloud/configurations/config_default",
    ],
)
def test_file_approval_rejects_workspace_credential_directories(path: str) -> None:
    from ash.coworkers import _validate_file_approval_path

    with pytest.raises(ValueError, match="protected or sensitive"):
        _validate_file_approval_path(path, label="target path")


@pytest.mark.parametrize(
    "replacement_content",
    [b"atlas-profile-secret", b"\xff", b"x" * 16_385],
)
def test_file_approval_does_not_persist_secret_binary_or_oversize_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replacement_content: bytes,
) -> None:
    with pytest.raises(ValueError):
        _new_pending_file_approval(
            tmp_path,
            monkeypatch,
            capsys,
            replacement_content=replacement_content,
        )
    output = capsys.readouterr()
    assert "atlas-profile-secret" not in output.out
    ash_dir = tmp_path / "home" / ".ash"
    with sqlite3.connect(ash_dir / "coworkers.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM coworker_file_approvals"
        ).fetchone() == (0,)


def test_file_approval_detects_altered_persisted_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import CoworkerDirectoryError

    directory, handle, _, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    with sqlite3.connect(directory.db_path) as connection:
        connection.execute(
            "UPDATE coworker_file_approvals SET replacement_content = ? "
            "WHERE approval_id = ?",
            (b"VALUE = 999\n", approval.approval_id),
        )
    with pytest.raises(CoworkerDirectoryError, match="digest changed"):
        handle.inspect_file_approval(approval.approval_id)
    assert target.read_bytes() == b"VALUE = 1\n"


def test_file_approval_expiry_denial_and_stale_target_are_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import datetime, timedelta, timezone

    from ash.coworkers import CoworkerRuntimeUnavailable

    directory, handle, _, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    handle._deny_file_approval(approval.approval_id)
    with pytest.raises(CoworkerRuntimeUnavailable, match="not approved"):
        handle._apply_file_approval(approval.approval_id)
    denied = handle.inspect_file_approval(approval.approval_id)[0]
    assert denied.status == "denied"
    assert target.read_bytes() == b"VALUE = 1\n"

    _, revoked_handle, _, _, revoked_target, _, revoked, _ = (
        _new_pending_file_approval(tmp_path / "revoked", monkeypatch, capsys)
    )
    revoked_handle._approve_file_approval(revoked.approval_id)
    assert revoked_handle._revoke_file_approval(revoked.approval_id).status == "revoked"
    with pytest.raises(CoworkerRuntimeUnavailable, match="not approved"):
        revoked_handle._apply_file_approval(revoked.approval_id)
    assert [
        event.status
        for event in revoked_handle.inspect_file_approval(revoked.approval_id)[1]
    ] == ["pending", "approved", "revoked"]
    assert revoked_target.read_bytes() == b"VALUE = 1\n"

    _, expired_handle, _, _, expired_target, _, expired, _ = _new_pending_file_approval(
        tmp_path / "expired", monkeypatch, capsys
    )
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with monkeypatch.context() as patcher:
        patcher.setattr("ash.coworkers._now", lambda: future)
        with pytest.raises(CoworkerRuntimeUnavailable, match="no longer pending"):
            expired_handle._approve_file_approval(expired.approval_id)
    assert expired_handle.inspect_file_approval(expired.approval_id)[0].status == "expired"
    assert expired_target.read_bytes() == b"VALUE = 1\n"

    _, stale_handle, _, _, stale_target, _, stale, _ = _new_pending_file_approval(
        tmp_path / "stale", monkeypatch, capsys
    )
    stale_target.write_bytes(b"VALUE = 3\n")
    with pytest.raises(CoworkerRuntimeUnavailable, match="revoked"):
        stale_handle._approve_file_approval(stale.approval_id)
    assert stale_handle.inspect_file_approval(stale.approval_id)[0].status == "revoked"
    assert stale_target.read_bytes() == b"VALUE = 3\n"


def test_file_approval_revalidates_policy_after_dispatch_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import CoworkerRuntimeUnavailable

    directory, handle, identity, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    handle._approve_file_approval(approval.approval_id)
    original_transition = directory._transition_file_approval
    profile_config = (
        Path.home() / ".ash" / "profiles" / identity.profile_name / "ash.toml"
    )

    def change_policy_after_intent(*args: Any, **kwargs: Any) -> Any:
        result = original_transition(*args, **kwargs)
        if kwargs.get("status") == "dispatching":
            with profile_config.open("a", encoding="utf-8") as stream:
                stream.write("safety_tier = 'plan'\n")
        return result

    monkeypatch.setattr(
        directory,
        "_transition_file_approval",
        change_policy_after_intent,
    )
    with pytest.raises(CoworkerRuntimeUnavailable, match="blocked"):
        handle._apply_file_approval(approval.approval_id)
    observed, events = handle.inspect_file_approval(approval.approval_id)
    assert observed.status == "blocked"
    assert [event.status for event in events][-1] == "blocked"
    assert target.read_bytes() == b"VALUE = 1\n"


def test_file_approval_creates_once_and_absence_cas_preserves_contender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import CoworkerRuntimeUnavailable

    _, handle, _, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path,
        monkeypatch,
        capsys,
        target_content=None,
    )
    assert approval.action == "create"
    assert approval.expected_sha256 == "missing"
    assert approval.expected_content == b""
    handle._approve_file_approval(approval.approval_id)
    assert handle._apply_file_approval(approval.approval_id).status == "succeeded"
    assert target.read_bytes() == b"VALUE = 2\n"

    _, race_handle, _, _, race_target, _, race_approval, _ = _new_pending_file_approval(
        tmp_path / "race",
        monkeypatch,
        capsys,
        target_content=None,
    )
    race_handle._approve_file_approval(race_approval.approval_id)
    import ash.coworkers as coworkers_module

    real_write = coworkers_module.atomic_write_scoped_bytes

    def appear_at_effect_boundary(*args: Any, **kwargs: Any) -> Any:
        race_target.write_bytes(b"another owner-created file\n")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        coworkers_module,
        "atomic_write_scoped_bytes",
        appear_at_effect_boundary,
    )
    with pytest.raises(CoworkerRuntimeUnavailable, match="target changed"):
        race_handle._apply_file_approval(race_approval.approval_id)
    failed = race_handle.inspect_file_approval(race_approval.approval_id)[0]
    assert failed.status == "failed"
    assert race_target.read_bytes() == b"another owner-created file\n"


def test_file_approval_restart_reconciles_dispatch_crashes_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import CoworkerDirectory, CoworkerRuntimeUnavailable

    directory, handle, identity, workspace, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    handle._approve_file_approval(approval.approval_id)
    original_verify = directory._verify_file_approval_context

    def crash_before_intent(*args: Any, **kwargs: Any) -> Any:
        raise SystemExit(90)

    monkeypatch.setattr(directory, "_verify_file_approval_context", crash_before_intent)
    with pytest.raises(SystemExit, match="90"):
        handle._apply_file_approval(approval.approval_id)
    assert target.read_bytes() == b"VALUE = 1\n"
    before_dispatch = CoworkerDirectory.open().open_coworker(
        owner_id=identity.owner_id,
        coworker_id=identity.coworker_id,
    )
    assert before_dispatch.inspect_file_approval(approval.approval_id)[0].status == "approved"
    monkeypatch.setattr(directory, "_verify_file_approval_context", original_verify)
    assert before_dispatch._apply_file_approval(approval.approval_id).status == "succeeded"
    assert target.read_bytes() == b"VALUE = 2\n"

    directory, handle, identity, workspace, target, _, approval, _ = _new_pending_file_approval(
        tmp_path / "after-intent", monkeypatch, capsys
    )
    handle._approve_file_approval(approval.approval_id)
    original_verify = directory._verify_file_approval_context
    verify_calls = 0

    def crash_after_intent(*args: Any, **kwargs: Any) -> Any:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 2:
            raise SystemExit(91)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(directory, "_verify_file_approval_context", crash_after_intent)
    with pytest.raises(SystemExit, match="91"):
        handle._apply_file_approval(approval.approval_id)
    assert target.read_bytes() == b"VALUE = 1\n"
    reopened = CoworkerDirectory.open().open_coworker(
        owner_id=identity.owner_id,
        coworker_id=identity.coworker_id,
    )
    ambiguous, _ = reopened.inspect_file_approval(approval.approval_id)
    assert ambiguous.status == "ambiguous"
    with pytest.raises(CoworkerRuntimeUnavailable, match="consumed"):
        reopened._apply_file_approval(approval.approval_id)
    assert target.read_bytes() == b"VALUE = 1\n"

    second_dir, second_handle, second_identity, _, second_target, _, second, _ = (
        _new_pending_file_approval(tmp_path / "after-write", monkeypatch, capsys)
    )
    second_handle._approve_file_approval(second.approval_id)
    real_write = __import__("ash.coworkers", fromlist=["atomic_write_scoped_bytes"]).atomic_write_scoped_bytes

    def crash_after_write(*args: Any, **kwargs: Any) -> Any:
        real_write(*args, **kwargs)
        raise SystemExit(92)

    monkeypatch.setattr("ash.coworkers.atomic_write_scoped_bytes", crash_after_write)
    with pytest.raises(SystemExit, match="92"):
        second_handle._apply_file_approval(second.approval_id)
    assert second_target.read_bytes() == b"VALUE = 2\n"
    after_restart = CoworkerDirectory.open().open_coworker(
        owner_id=second_identity.owner_id,
        coworker_id=second_identity.coworker_id,
    )
    reconciled, events = after_restart.inspect_file_approval(second.approval_id)
    assert reconciled.status == "succeeded"
    assert reconciled.observed_sha256 == hashlib.sha256(b"VALUE = 2\n").hexdigest()
    assert [event.status for event in events][-1] == "succeeded"
    with pytest.raises(CoworkerRuntimeUnavailable, match="consumed"):
        after_restart._apply_file_approval(second.approval_id)
    assert second_target.read_bytes() == b"VALUE = 2\n"


def _apply_approval_in_process(
    ash_dir: str,
    owner_id: str,
    coworker_id: str,
    approval_id: str,
    result_queue: Any,
) -> None:
    from pathlib import Path

    from ash.coworkers import CoworkerDirectory, CoworkerRuntimeUnavailable

    try:
        handle = CoworkerDirectory.open(Path(ash_dir)).open_coworker(
            owner_id=owner_id,
            coworker_id=coworker_id,
        )
        result_queue.put(("ok", handle._apply_file_approval(approval_id).status))
    except CoworkerRuntimeUnavailable:
        result_queue.put(("rejected", "CoworkerRuntimeUnavailable"))


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_file_approval_apply_has_one_multiprocess_claimant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import multiprocessing

    directory, handle, identity, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    handle._approve_file_approval(approval.approval_id)
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_apply_approval_in_process,
            args=(
                str(directory.ash_dir),
                identity.owner_id,
                identity.coworker_id,
                approval.approval_id,
                result_queue,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("file approval process did not finish")
        assert process.exitcode == 0
    results = [result_queue.get(timeout=2) for _ in processes]
    assert sum(result == ("ok", "succeeded") for result in results) == 1
    assert results.count(("rejected", "CoworkerRuntimeUnavailable")) == 1
    final, events = handle.inspect_file_approval(approval.approval_id)
    assert final.status == "succeeded"
    assert [event.status for event in events].count("dispatching") == 1
    assert target.read_bytes() == b"VALUE = 2\n"
