from __future__ import annotations

import asyncio
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
from ash.coworkers import CoworkerDirectory, CoworkerRuntimeUnavailable
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
    started: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
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
            prompt_text = "\n".join(
                str(
                    message.get("content", "")
                    if isinstance(message, dict)
                    else getattr(message, "content", "")
                )
                for message in messages
            )
            is_readiness_probe = "Synthetic readiness check." in prompt_text
            get_stdout = getattr(sys.stdout, "getvalue", None)
            captured_output = get_stdout() if callable(get_stdout) else ""
            observed.append(
                {
                    "model": self.model_name,
                    "key": self._api_key,
                    "messages": list(messages),
                    "tools": tools,
                    "provider_id": id(self),
                    "is_readiness_probe": is_readiness_probe,
                    "turn_id_reported_before_dispatch": "Turn ID:" in captured_output,
                }
            )
            if started is not None:
                started.set()
            if release is not None:
                await release.wait()
            if self.model_name.endswith("atlas"):
                answer = (
                    "4"
                    if is_readiness_probe
                    else atlas_answer
                    + "atlas-profile-secret \u001b[31mred\u001b[0m\u202e"
                )
            else:
                answer = (
                    "4"
                    if is_readiness_probe
                    else "Scout found an independent research note. scout-profile-secret"
                )
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


def _install_readiness_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_answer: str = "4",
    probe_stop_reason: str | None = "stop",
    probe_tool_call: bool = False,
) -> tuple[list[dict[str, Any]], list[ProviderABC]]:
    observed: list[dict[str, Any]] = []
    providers: list[ProviderABC] = []

    class ReadinessProvider(ProviderABC):
        provider_family = "openai"

        def __init__(self, model: str, key: str) -> None:
            self._model = model
            self._api_key = key
            self.closed = False

        @property
        def model_name(self) -> str:
            return self._model

        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def configure_max_tokens(self, max_tokens: int) -> None:
            assert 0 < max_tokens <= 512

        async def aclose(self) -> None:
            self.closed = True

        async def stream_chat(
            self,
            messages: Sequence[MessageInput],
            temperature: float = 0.0,
            tools: list[dict[str, object]] | None = None,
        ) -> AsyncGenerator[StreamChunk, None]:
            del temperature
            prompt_text = "\n".join(
                str(
                    message.get("content", "")
                    if isinstance(message, dict)
                    else getattr(message, "content", "")
                )
                for message in messages
            )
            is_probe = "Synthetic readiness check." in prompt_text
            observed.append(
                {
                    "model": self.model_name,
                    "provider_id": id(self),
                    "prompt": prompt_text,
                    "tools": tools,
                    "is_probe": is_probe,
                }
            )
            if is_probe and probe_tool_call:
                yield StreamChunk(tool_call_delta="attempt_tool")
            else:
                answer = probe_answer if is_probe else "real Crew response"
                yield StreamChunk(content=answer)
            yield StreamChunk(
                is_done=True,
                stop_reason=probe_stop_reason if is_probe else "stop",
            )

    registry = ProviderRegistry()

    def build_profile_provider(
        _config: object,
        model: str,
        environment: object,
    ) -> ProviderABC:
        assert isinstance(environment, dict)
        key = next(iter(environment.values()), "")
        assert isinstance(key, str)
        provider = ReadinessProvider(model, key)
        providers.append(provider)
        return provider

    def build_provider(_config: object, model: str) -> ProviderABC:
        return ReadinessProvider(model, "")

    registry.register("openai", build_provider)
    registry._register_profile_factory("openai", build_provider, build_profile_provider)
    registry.register("openrouter", build_provider)
    registry._register_profile_factory("openrouter", build_provider, build_profile_provider)
    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        lambda: registry,
    )
    return observed, providers


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
def test_crew_check_model_sends_one_synthetic_completion_without_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    observed, providers = _install_readiness_registry(monkeypatch)
    directory = CoworkerDirectory.open(ash_dir)
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

    assert main(
        [
            "crew",
            "check-model",
            "--owner",
            "local",
            "--coworker-id",
            identity.coworker_id,
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Synthetic model readiness probe passed for openai/gpt-atlas" in output
    assert "One provider request was attempted" in output
    assert "No Crew run claim or history was created" in output
    assert "does not guarantee the next provider request" in output
    assert len(observed) == 1
    assert observed[0]["is_probe"] is True
    assert observed[0]["tools"] is None
    assert "2 + 2" in observed[0]["prompt"]
    assert "workspace" not in observed[0]["prompt"].casefold()
    assert providers[0].closed is True
    assert handle.list_readonly_turn_history() == ()
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM coworker_turn_dispatches WHERE coworker_id = ?",
            (identity.coworker_id,),
        ).fetchone()[0] == 0

    assert main(
        [
            "crew",
            "check-model",
            "--owner",
            "foreign",
            "--coworker-id",
            identity.coworker_id,
        ]
    ) == 2
    assert "no coworker matches" in capsys.readouterr().err
    with monkeypatch.context() as wrong_uid:
        wrong_uid.setattr("ash.coworkers._current_owner_uid", lambda: os.geteuid() + 1)
        assert main(
            [
                "crew",
                "check-model",
                "--owner",
                "local",
                "--coworker-id",
                identity.coworker_id,
            ]
        ) == 2
        assert "owner identity" in capsys.readouterr().err
    handle.pause()
    assert main(
        [
            "crew",
            "check-model",
            "--owner",
            "local",
            "--coworker-id",
            identity.coworker_id,
        ]
    ) == 2
    assert "coworker is paused" in capsys.readouterr().err
    assert len(observed) == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize(
    ("answer", "stop_reason", "tool_call", "failure_code"),
    [
        ("", "stop", False, "empty_answer"),
        ("4", "length", False, "completion_truncated"),
        ("4", "content_filter", False, "completion_filtered"),
        ("4", "error", False, "completion_error"),
        ("4", "stop", True, "tool_call"),
        ("atlas-profile-secret", "stop", False, "unexpected_answer"),
    ],
)
def test_crew_check_model_rejects_invalid_outputs_without_leaking_or_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    stop_reason: str,
    tool_call: bool,
    failure_code: str,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    observed, providers = _install_readiness_registry(
        monkeypatch,
        probe_answer=answer,
        probe_stop_reason=stop_reason,
        probe_tool_call=tool_call,
    )
    directory = CoworkerDirectory.open(ash_dir)
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

    assert main(
        [
            "crew",
            "check-model",
            "--owner",
            "local",
            "--coworker-id",
            identity.coworker_id,
        ]
    ) == 2
    result = capsys.readouterr()
    assert f"failure_code={failure_code}" in result.err
    assert "A provider request was attempted" in result.err
    assert "no Crew run claim was made" in result.err
    assert "atlas-profile-secret" not in result.out + result.err
    assert len(observed) == 1
    assert observed[0]["is_probe"] is True
    assert providers[0].closed is True
    assert handle.list_readonly_turn_history() == ()
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM coworker_turn_dispatches WHERE coworker_id = ?",
            (identity.coworker_id,),
        ).fetchone()[0] == 0


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize("model", ["openrouter/openrouter/free", "openrouter/openrouter/auto"])
@pytest.mark.parametrize("check_model", [False, True])
def test_crew_rejects_known_dynamic_openrouter_models_before_provider_or_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    model: str,
    check_model: bool,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    profile = ash_dir / "profiles" / "atlas"
    (profile / "ash.toml").write_text(f"model = '{model}'\n", encoding="utf-8")
    observed, providers = _install_readiness_registry(monkeypatch)
    directory = CoworkerDirectory.open(ash_dir)
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
    args = _ask_args(identity.coworker_id, "Explain the synthetic failure.")
    if check_model:
        args.append("--check-model")

    assert main(args) == 2
    result = capsys.readouterr()
    assert "failure_code=dynamic_model" in result.err
    assert "No provider request was sent" in result.err
    assert "no Crew run claim was made" in result.err
    assert not observed
    assert not providers
    assert handle.list_readonly_turn_history() == ()
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM coworker_turn_dispatches WHERE coworker_id = ?",
            (identity.coworker_id,),
        ).fetchone()[0] == 0


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize(
    "model",
    [
        "openrouter/deepseek/deepseek-r1:free",
        "openrouter/vendor/model-alias",
    ],
)
def test_crew_accepts_pinned_openrouter_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    model: str,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    profile = ash_dir / "profiles" / "atlas"
    (profile / "ash.toml").write_text(f"model = '{model}'\n", encoding="utf-8")
    with (profile / ".env").open("a", encoding="utf-8") as stream:
        stream.write("OPENROUTER_API_KEY=atlas-profile-secret\n")
    observed, providers = _install_readiness_registry(monkeypatch)
    identity = CoworkerDirectory.open(ash_dir).create(
        owner_id="local",
        display_name="Atlas",
        profile_name="atlas",
        workspace=workspace,
    )

    args = _ask_args(identity.coworker_id, "Explain the synthetic failure.")
    args.append("--check-model")
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "real Crew response" in output
    assert len(observed) == 2
    assert [item["is_probe"] for item in observed] == [True, False]
    assert [item["model"] for item in observed] == [
        model.split("/", 1)[1],
        model.split("/", 1)[1],
    ]
    assert observed[0]["provider_id"] == observed[1]["provider_id"]
    assert providers[0].closed is True


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_preflight_failure_blocks_ask_before_run_claim_and_success_uses_same_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    observed, providers = _install_readiness_registry(
        monkeypatch,
        probe_answer="not four",
    )
    directory = CoworkerDirectory.open(ash_dir)
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

    args = _ask_args(
        identity.coworker_id,
        "Explain the fixture.",
        turn_id="failed-preflight-does-not-claim",
    )
    args.append("--check-model")
    assert main(args) == 2
    failure = capsys.readouterr()
    assert "failure_code=unexpected_answer" in failure.err
    assert "A provider request was attempted" in failure.err
    assert "no Crew run claim was made" in failure.err
    assert len(observed) == 1
    assert handle.list_readonly_turn_history() == ()
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM coworker_turn_dispatches WHERE coworker_id = ?",
            (identity.coworker_id,),
        ).fetchone()[0] == 0
    assert providers[0].closed is True

    observed, providers = _install_readiness_registry(monkeypatch)
    success_args = _ask_args(
        identity.coworker_id,
        "Explain the fixture.",
        turn_id="success-after-preflight",
    )
    success_args.append("--check-model")
    assert main(success_args) == 0
    output = capsys.readouterr().out
    assert "real Crew response" in output
    assert len(observed) == 2
    assert [item["is_probe"] for item in observed] == [True, False]
    assert observed[0]["provider_id"] == observed[1]["provider_id"]
    assert providers[0].closed is True
    assert len(handle.list_readonly_turn_history()) == 1

    observed, providers = _install_readiness_registry(monkeypatch)
    ordinary_args = _ask_args(
        identity.coworker_id,
        "Explain the fixture.",
        turn_id="ordinary-no-preflight",
    )
    assert main(ordinary_args) == 0
    assert "real Crew response" in capsys.readouterr().out
    assert len(observed) == 1
    assert observed[0]["is_probe"] is False
    assert providers[0].closed is True
    assert len(handle.list_readonly_turn_history()) == 2


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_preflight_admission_failure_reports_attempt_without_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import _probe_coworker_model as real_probe

    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    observed, providers = _install_readiness_registry(monkeypatch)
    directory = CoworkerDirectory.open(ash_dir)
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

    async def probe_then_pause(provider: Any) -> None:
        await real_probe(provider)
        handle.pause()

    monkeypatch.setattr("ash.coworkers._probe_coworker_model", probe_then_pause)
    args = _ask_args(
        identity.coworker_id,
        "Explain the fixture.",
        turn_id="paused-after-preflight",
    )
    args.append("--check-model")
    assert main(args) == 2
    failure = capsys.readouterr().err
    assert "failure_code=admission_rejected" in failure
    assert "A provider request was attempted" in failure
    assert "no Crew run claim was made" in failure
    assert len(observed) == 1
    assert observed[0]["is_probe"] is True
    assert providers[0].closed is True
    assert handle.list_readonly_turn_history() == ()
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM coworker_turn_dispatches WHERE coworker_id = ?",
            (identity.coworker_id,),
        ).fetchone()[0] == 0


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
        "ash.coworker_evidence.snapshot_scoped_file",
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
def test_model_truncation_exposes_safe_category_and_keeps_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)

    class TruncatedProvider(ProviderABC):
        provider_family = "openai"

        @property
        def model_name(self) -> str:
            return "synthetic-truncated"

        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def configure_max_tokens(self, max_tokens: int) -> None:
            assert 0 < max_tokens <= 512

        async def stream_chat(
            self,
            messages: Sequence[MessageInput],
            temperature: float = 0.0,
            tools: list[dict[str, object]] | None = None,
        ) -> AsyncGenerator[StreamChunk, None]:
            del messages, temperature, tools
            yield StreamChunk(content="private-source-should-not-leak")
            yield StreamChunk(is_done=True, stop_reason="length")

    registry = ProviderRegistry()

    def build(_config: object, _model: str) -> ProviderABC:
        return TruncatedProvider()

    def scoped_build(
        _config: object, _model: str, _environment: object
    ) -> ProviderABC:
        return TruncatedProvider()

    registry.register("openai", build)
    registry._register_profile_factory("openai", build, scoped_build)
    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry", lambda: registry
    )
    directory = CoworkerDirectory.open(ash_dir)
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
    responsibility = handle.create_responsibility(
        scope="Synthetic CI failure",
        objective="Summarize this fixture without making changes.",
    )
    with pytest.raises(
        CoworkerRuntimeUnavailable, match="failure_code=completion_truncated"
    ) as caught:
        asyncio.run(
            handle.run_model_responsibility(
                responsibility.responsibility_id,
                event_id="truncated-once",
            )
        )
    assert "private-source-should-not-leak" not in str(caught.value)
    runs = handle.list_responsibility_runs(responsibility.responsibility_id)
    assert len(runs) == 1 and runs[0].status == "claimed"
    assert directory.list_inbox(owner_id="local")[0].status == "claimed"


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
    directory._claim_responsibility_turn(
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
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    pending_inbox = capsys.readouterr().out
    assert f"approval:{approval_id}" in pending_inbox
    assert "Status: Waiting for explicit owner approval." in pending_inbox

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
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    approved_inbox = capsys.readouterr().out
    assert approved_inbox.count(f"approval:{approval_id}") == 1
    assert "Status: Approved; waiting for an explicit local apply." in approved_inbox

    assert run_tty_action(apply_args, "APPLY") == 0
    assert "Status: succeeded." in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    applied_inbox = capsys.readouterr().out
    assert applied_inbox.count(f"approval:{approval_id}") == 1
    assert "Status: Applied." in applied_inbox
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


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_archive_fences_late_approval_and_preserves_unapplied_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.coworkers import CoworkerRuntimeUnavailable

    directory, handle, _, _, target, _, pending, _ = _new_pending_file_approval(
        tmp_path / "late-approval", monkeypatch, capsys
    )
    verify = directory._verify_file_approval_context

    def verify_then_archive(*args: Any, **kwargs: Any) -> Any:
        result = verify(*args, **kwargs)
        handle.archive()
        return result

    monkeypatch.setattr(directory, "_verify_file_approval_context", verify_then_archive)
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is archived"):
        handle._approve_file_approval(pending.approval_id)
    assert target.read_bytes() == b"VALUE = 1\n"
    pending_after_archive, pending_events = handle.inspect_file_approval(
        pending.approval_id
    )
    assert pending_after_archive.status == "pending"
    assert [event.status for event in pending_events] == ["pending"]

    (
        approved_directory,
        approved_handle,
        _,
        _,
        approved_target,
        _,
        approved,
        source_ids,
    ) = _new_pending_file_approval(
        tmp_path / "approved-unapplied", monkeypatch, capsys
    )
    assert approved_handle._approve_file_approval(approved.approval_id).status == "approved"
    approved_handle.archive()
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is archived"):
        approved_handle._apply_file_approval(approved.approval_id)
    retained, events = approved_handle.inspect_file_approval(approved.approval_id)
    assert retained.status == "approved"
    assert [event.status for event in events] == ["pending", "approved"]
    assert approved_target.read_bytes() == b"VALUE = 1\n"
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is archived"):
        approved_handle.prepare_file_approval(
            run_id=source_ids["Run ID"],
            session_id=source_ids["Session ID"],
            request_id=source_ids["Request ID"],
            target_path="src/example.py",
            replacement_file_path="src/example.fixed.py",
        )
    assert len(approved_handle.list_file_approvals()) == 1
    assert CoworkerDirectory.open(approved_directory.ash_dir).open_coworker(
        owner_id="local",
        coworker_id=approved_handle.identity.coworker_id,
    ).inspect_file_approval(approved.approval_id)[0].status == "approved"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_v9_lifecycle_migration_preserves_file_approval_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, handle, identity, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    with sqlite3.connect(directory.db_path) as connection:
        prior_row = connection.execute(
            "SELECT approval_id, status, approval_digest, replacement_sha256 "
            "FROM coworker_file_approvals WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        prior_events = connection.execute(
            "SELECT status, actor_uid, detail FROM coworker_file_approval_events "
            "WHERE approval_id = ? ORDER BY event_id",
            (approval.approval_id,),
        ).fetchall()
        connection.execute("ALTER TABLE coworkers DROP COLUMN lifecycle_status")
        connection.execute("UPDATE coworker_directory_meta SET schema_version = 9")

    reopened_directory = CoworkerDirectory.open(directory.ash_dir)
    reopened = reopened_directory.open_coworker(
        owner_id=identity.owner_id,
        coworker_id=identity.coworker_id,
    )
    assert reopened.identity.lifecycle_status == "active"
    persisted, events = reopened.inspect_file_approval(approval.approval_id)
    assert persisted.status == "pending"
    assert [event.status for event in events] == ["pending"]
    assert target.read_bytes() == b"VALUE = 1\n"
    with sqlite3.connect(directory.db_path) as connection:
        assert connection.execute(
            "SELECT approval_id, status, approval_digest, replacement_sha256 "
            "FROM coworker_file_approvals WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone() == prior_row
        assert connection.execute(
            "SELECT status, actor_uid, detail FROM coworker_file_approval_events "
            "WHERE approval_id = ? ORDER BY event_id",
            (approval.approval_id,),
        ).fetchall() == prior_events
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


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
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    denied_inbox = capsys.readouterr().out
    assert f"approval:{approval.approval_id}" in denied_inbox
    assert "Status: Denied." in denied_inbox
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
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    revoked_inbox = capsys.readouterr().out
    assert f"approval:{revoked.approval_id}" in revoked_inbox
    assert "Status: Revoked." in revoked_inbox
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
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    expired_inbox = capsys.readouterr().out
    assert f"approval:{expired.approval_id}" in expired_inbox
    assert "Status: Expired." in expired_inbox
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


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_file_approval_apply_has_one_multiprocess_claimant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    directory, handle, identity, _, target, _, approval, _ = _new_pending_file_approval(
        tmp_path, monkeypatch, capsys
    )
    handle._approve_file_approval(approval.approval_id)

    # Run separate Python interpreters, not multiprocessing.spawn on a test
    # module: tests/ is not an importable package in installed CI builds.
    child_script = """
import sys
from pathlib import Path
from ash.coworkers import CoworkerDirectory, CoworkerRuntimeUnavailable

directory = CoworkerDirectory.open(Path(sys.argv[1]))
handle = directory.open_coworker(owner_id=sys.argv[2], coworker_id=sys.argv[3])
try:
    status = handle._apply_file_approval(sys.argv[4]).status
except CoworkerRuntimeUnavailable:
    print("rejected")
else:
    print(status)
"""
    command = [
        sys.executable,
        "-c",
        child_script,
        str(directory.ash_dir),
        identity.owner_id,
        identity.coworker_id,
        approval.approval_id,
    ]
    processes = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results: list[str] = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(stdout.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert results.count("succeeded") == 1
    assert results.count("rejected") == 1
    final, events = handle.inspect_file_approval(approval.approval_id)
    assert final.status == "succeeded"
    assert [event.status for event in events].count("dispatching") == 1
    assert target.read_bytes() == b"VALUE = 2\n"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_signed_event_and_due_schedule_use_distinct_profile_model_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import hmac
    import subprocess
    import sys
    from datetime import datetime, timedelta, timezone
    from uuid import UUID, uuid4, uuid5

    import ash.coworkers as coworkers

    ash_dir, atlas_workspace, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(
        monkeypatch,
        atlas_answer="APPROVED: Atlas recommends inspecting this failure. ",
    )
    fixed_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    monkeypatch.setattr("ash.automation.schedules.utc_now", lambda: fixed_now)
    monkeypatch.setattr("ash.crew_events.utc_now", lambda: fixed_now)
    monkeypatch.setattr(coworkers, "_now", lambda: fixed_now.isoformat())

    protected = atlas_workspace / "src" / "protected.py"
    protected.parent.mkdir()
    protected.write_text("VALUE = 1\n", encoding="utf-8")
    (atlas_workspace / "src" / "protected.fixed.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    ci_log = atlas_workspace / "ci-failure.txt"
    ci_log.write_text(
        "pytest failed in test_auth: expected 2 checks, received 1.\n",
        encoding="utf-8",
    )
    ungranted = atlas_workspace / "ungranted.py"
    ungranted.write_text("UNGRANTED_ATLAS_CONTENT\n", encoding="utf-8")
    scout_note = scout_workspace / "scout-note.txt"
    scout_note.write_text("Scout-only research note.\n", encoding="utf-8")
    directory = CoworkerDirectory.open(ash_dir)
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
    atlas_responsibility_id = str(uuid4())
    atlas_manual_responsibility_id = str(uuid4())
    scout_responsibility_id = str(uuid4())

    assert main(
        [
            "crew",
            "responsibility",
            "create",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_responsibility_id,
            "--scope",
            "Atlas CI failure",
            "--objective",
            "Explain the synthetic test failure and propose a minimal correction.",
            "--success-criterion",
            "Identify the failing assertion.",
            "--evidence",
            "ci-failure.txt",
            "--evidence",
            "src/protected.py",
        ]
    ) == 0
    atlas_create_output = capsys.readouterr().out
    assert "Evidence grant: ci-failure.txt" in atlas_create_output
    assert "Evidence grant: src/protected.py" in atlas_create_output
    assert main(
        [
            "crew",
            "responsibility",
            "create",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_manual_responsibility_id,
            "--scope",
            "Atlas owner follow-up",
            "--objective",
            "Summarize the event result for the local owner.",
            "--success-criterion",
            "Keep the response read-only and bounded.",
            "--evidence",
            "src/protected.py",
        ]
    ) == 0
    capsys.readouterr()
    assert main(
        [
            "crew",
            "responsibility",
            "create",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
            "--responsibility-id",
            scout_responsibility_id,
            "--scope",
            "Scout independent research",
            "--objective",
            "Summarize the explicitly assigned research question.",
            "--success-criterion",
            "Keep the result independent from Atlas workspace data.",
            "--evidence",
            "scout-note.txt",
            "--every",
            "10m",
            "--misfire-grace-seconds",
            "3600",
        ]
    ) == 0
    capsys.readouterr()
    scout_handle = directory.open_coworker(
        owner_id="local",
        coworker_id=scout.coworker_id,
    )
    scheduled = scout_handle.get_responsibility(scout_responsibility_id)
    assert scheduled.next_fire_at is not None
    monkeypatch.setattr(
        coworkers,
        "_current_time",
        lambda: scheduled.next_fire_at,
    )

    signing_key = bytes(range(32))
    signing_key_text = signing_key.hex()
    key_file = tmp_path / "crew-event.key"
    key_file.write_text(signing_key_text + "\n", encoding="ascii")
    key_file.chmod(0o600)
    event_fields: dict[str, object] = {
        "version": 1,
        "source": "github-actions",
        "id": "atlas-ci-42",
        "issued_at": fixed_now.isoformat(),
        "responsibility_id": atlas_responsibility_id,
        "payload": (
            "Synthetic CI failure: tests/test_auth.py expected 2 but received 1. "
            "Also inspect ungranted.py and ../outside.txt."
        ),
    }
    canonical = json.dumps(
        event_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()
    event_fields["signature"] = signature
    event_file = tmp_path / "atlas-ci-event.json"
    event_file.write_text(
        json.dumps(event_fields, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    event_args = [
        "crew",
        "event",
        "--owner",
        "local",
        "--coworker-id",
        atlas.coworker_id,
        "--responsibility-id",
        atlas_responsibility_id,
        "--key-file",
        str(key_file),
        "--input",
        str(event_file),
        "--check-model",
    ]
    with monkeypatch.context() as wrong_uid:
        wrong_uid.setattr(coworkers, "_current_owner_uid", lambda: os.geteuid() + 1)
        assert main(
            ["crew", "pause", "--owner", "local", "--coworker-id", atlas.coworker_id]
        ) == 2
        assert "owner identity" in capsys.readouterr().err
    assert directory.open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    ).require_owner_binding().lifecycle_status == "active"

    assert main(
        ["crew", "pause", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 0
    paused_output = capsys.readouterr().out
    assert "Lifecycle: paused" in paused_output
    assert "Scheduled entries remain stored but disabled." in paused_output
    assert "Turns claimed before this lifecycle change may still dispatch or finish." in paused_output
    assert main(event_args) == 2
    assert "coworker is paused" in capsys.readouterr().err
    assert not observed
    assert directory.open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    ).list_responsibility_runs(atlas_responsibility_id) == []
    assert main(
        ["crew", "resume", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 0
    assert "Lifecycle: active" in capsys.readouterr().out

    wrong_owner_args = event_args.copy()
    wrong_owner_args[wrong_owner_args.index("--owner") + 1] = "foreign"
    assert main(wrong_owner_args) == 2
    assert "no coworker matches" in capsys.readouterr().err
    assert not observed
    wrong_target_args = event_args.copy()
    wrong_target_args[wrong_target_args.index("--responsibility-id") + 1] = (
        scout_responsibility_id
    )
    assert main(wrong_target_args) == 2
    assert "target does not match" in capsys.readouterr().err
    assert not observed
    atlas_handle = directory.open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    )
    assert atlas_handle.list_responsibility_runs(atlas_responsibility_id) == []

    event_fields["signature"] = "0" * 64
    event_file.write_text(
        json.dumps(event_fields, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    assert main(event_args) == 2
    assert "authentication failed" in capsys.readouterr().err
    assert not observed
    event_fields["signature"] = signature
    event_file.write_text(
        json.dumps(event_fields, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    assert main(
        [
            "crew",
            "responsibility",
            "pause",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_responsibility_id,
        ]
    ) == 0
    capsys.readouterr()
    assert main(event_args) == 2
    assert "not active" in capsys.readouterr().err
    assert not observed
    assert main(
        [
            "crew",
            "responsibility",
            "resume",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_responsibility_id,
        ]
    ) == 0
    capsys.readouterr()

    with monkeypatch.context() as wrong_uid:
        wrong_uid.setattr(
            coworkers,
            "_current_owner_uid",
            lambda: os.geteuid() + 1,
        )
        assert main(event_args) == 2
        assert "owner identity" in capsys.readouterr().err
    assert not observed
    assert atlas_handle.list_responsibility_runs(atlas_responsibility_id) == []

    assert main(event_args) == 0
    atlas_output = capsys.readouterr().out
    assert "Trigger: event" in atlas_output
    assert "github-actions:atlas-ci-42" in atlas_output
    assert "APPROVED" in atlas_output
    assert "Read-only turn. No files were changed." in atlas_output
    assert f"SHA-256 {hashlib.sha256(ci_log.read_bytes()).hexdigest()}" in atlas_output
    assert signing_key_text not in atlas_output
    assert "atlas-profile-secret" not in atlas_output
    assert protected.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert len(observed) == 2
    assert observed[0]["is_readiness_probe"] is True
    assert observed[1]["is_readiness_probe"] is False
    atlas_message = next(
        str(message["content"])
        for message in observed[1]["messages"]
        if message["role"] == "user"
    )
    assert event_fields["payload"] in atlas_message
    assert signing_key_text not in atlas_message
    assert signature not in atlas_message
    assert observed[0]["tools"] is None
    assert observed[0]["model"] == "gpt-atlas"
    assert observed[1]["tools"] is None
    assert observed[1]["model"] == "gpt-atlas"
    assert observed[0]["provider_id"] == observed[1]["provider_id"]
    atlas_contract = json.loads(atlas_message.rsplit("\n", 1)[1])
    assert [item["path"] for item in atlas_contract["evidence"]] == [
        "ci-failure.txt",
        "src/protected.py",
    ]
    assert atlas_contract["evidence"][0]["sha256"] == hashlib.sha256(
        ci_log.read_bytes()
    ).hexdigest()
    assert atlas_contract["evidence"][0]["size_bytes"] == ci_log.stat().st_size
    assert atlas_contract["evidence"][0]["text"] == ci_log.read_text(encoding="utf-8")
    assert atlas_contract["evidence"][1]["sha256"] == hashlib.sha256(
        protected.read_bytes()
    ).hexdigest()
    assert atlas_contract["evidence"][1]["text"] == protected.read_text(
        encoding="utf-8"
    )
    assert "UNGRANTED_ATLAS_CONTENT" not in atlas_message
    assert "Scout-only research note." not in atlas_message

    atlas_source_ids = {
        label.lower().replace(" ", "_"): next(
            line.split(": ", 1)[1]
            for line in atlas_output.splitlines()
            if line.startswith(f"{label}: ")
        )
        for label in ("Run ID", "Session ID", "Request ID")
    }
    assert main(
        [
            "crew",
            "propose",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--run-id",
            atlas_source_ids["run_id"],
            "--session-id",
            atlas_source_ids["session_id"],
            "--request-id",
            atlas_source_ids["request_id"],
            "--target",
            "src/protected.py",
            "--replacement-file",
            "src/protected.fixed.py",
        ]
    ) == 0
    proposal_output = capsys.readouterr().out
    assert "Status: pending" in proposal_output
    atlas_approval_id = next(
        line.removeprefix("Approval ID: ")
        for line in proposal_output.splitlines()
        if line.startswith("Approval ID: ")
    )
    assert protected.read_text(encoding="utf-8") == "VALUE = 1\n"

    assert main(
        ["crew", "pause", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 0
    capsys.readouterr()
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    paused_inbox = capsys.readouterr().out
    assert f"Coworker: Atlas ({atlas.coworker_id})" in paused_inbox
    assert f"approval:{atlas_approval_id}" in paused_inbox
    assert "waiting for explicit owner approval" in paused_inbox.casefold()
    assert "Lifecycle: paused" in paused_inbox
    assert "VALUE = 1" not in paused_inbox
    assert "VALUE = 2" not in paused_inbox
    assert "APPROVED: Atlas recommends inspecting this failure." not in paused_inbox
    assert "atlas-profile-secret" not in paused_inbox
    assert main(
        ["crew", "resume", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 0
    capsys.readouterr()

    assert main(
        ["crew", "pause", "--owner", "local", "--coworker-id", scout.coworker_id]
    ) == 0
    assert "Lifecycle: paused" in capsys.readouterr().out
    assert main(
        [
            "crew",
            "tick",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
        ]
    ) == 2
    assert "coworker is paused" in capsys.readouterr().err
    assert len(observed) == 2
    assert scout_handle.get_responsibility(
        scout_responsibility_id
    ).next_fire_at == scheduled.next_fire_at
    assert main(
        ["crew", "resume", "--owner", "local", "--coworker-id", scout.coworker_id]
    ) == 0
    assert "Lifecycle: active" in capsys.readouterr().out
    assert main(
        [
            "crew",
            "tick",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
            "--check-model",
        ]
    ) == 0
    scout_output = capsys.readouterr().out
    assert "Trigger: scheduled" in scout_output
    assert (
        f"scheduled:{scheduled.next_fire_at.astimezone(timezone.utc).isoformat()}"
        in scout_output
    )
    assert "Scout found an independent research note." in scout_output
    assert "Evidence used: scout-note.txt" in scout_output
    assert "scout-profile-secret" not in scout_output
    assert len(observed) == 4
    assert observed[2]["is_readiness_probe"] is True
    assert observed[3]["is_readiness_probe"] is False
    assert observed[2]["tools"] is None
    assert observed[2]["model"] == "gpt-scout"
    assert observed[3]["tools"] is None
    assert observed[3]["model"] == "gpt-scout"
    assert observed[2]["provider_id"] == observed[3]["provider_id"]
    scout_message = next(
        str(message["content"])
        for message in observed[3]["messages"]
        if message["role"] == "user"
    )
    scout_contract = json.loads(scout_message.rsplit("\n", 1)[1])
    assert [item["path"] for item in scout_contract["evidence"]] == [
        "scout-note.txt"
    ]
    assert scout_contract["evidence"][0]["text"] == "Scout-only research note.\n"
    assert scout_contract["evidence"][0]["sha256"] == hashlib.sha256(
        scout_note.read_bytes()
    ).hexdigest()
    assert scout_contract["evidence"][0]["size_bytes"] == scout_note.stat().st_size
    assert "UNGRANTED_ATLAS_CONTENT" not in scout_message
    assert "pytest failed in test_auth" not in scout_message
    assert signing_key_text not in scout_message

    assert main(
        [
            "crew",
            "tick",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
            "--check-model",
        ]
    ) == 0
    assert "No due scheduled responsibility." in capsys.readouterr().out
    assert len(observed) == 4

    atlas_event_run_id = atlas_source_ids["run_id"]
    scout_scheduled_run_id = next(
        line.split(": ", 1)[1]
        for line in scout_output.splitlines()
        if line.startswith("Run ID: ")
    )
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    first_inbox = capsys.readouterr().out
    assert "No remote message was sent or delivered." in first_inbox
    assert f"run:{atlas.coworker_id}:{atlas_event_run_id}" in first_inbox
    assert f"approval:{atlas_approval_id}" in first_inbox
    assert f"run:{scout.coworker_id}:{scout_scheduled_run_id}" in first_inbox
    assert "Trigger" not in first_inbox
    assert "github-actions:atlas-ci-42" in first_inbox
    assert "scout-profile-secret" not in first_inbox
    assert "atlas-profile-secret" not in first_inbox
    assert "APPROVED: Atlas recommends inspecting this failure." not in first_inbox
    assert "VALUE = 1" not in first_inbox
    assert "VALUE = 2" not in first_inbox
    assert first_inbox.count(f"approval:{atlas_approval_id}") == 1
    reopened_entries = CoworkerDirectory.open(ash_dir).list_inbox(owner_id="local")
    reopened_entry_ids = [entry.entry_id for entry in reopened_entries]
    assert len(reopened_entry_ids) == len(set(reopened_entry_ids))
    assert f"run:{atlas.coworker_id}:{atlas_event_run_id}" in reopened_entry_ids
    assert f"approval:{atlas_approval_id}" in reopened_entry_ids
    assert f"run:{scout.coworker_id}:{scout_scheduled_run_id}" in reopened_entry_ids
    approval_entry = next(
        entry for entry in reopened_entries if entry.entry_id == f"approval:{atlas_approval_id}"
    )
    assert approval_entry.status == "pending"
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    assert capsys.readouterr().out == first_inbox

    assert main(["crew", "inbox", "--owner", "foreign"]) == 0
    foreign_inbox = capsys.readouterr().out
    assert "No Crew activity found" in foreign_inbox
    assert atlas.coworker_id not in foreign_inbox
    assert scout.coworker_id not in foreign_inbox
    with monkeypatch.context() as wrong_uid:
        wrong_uid.setattr(coworkers, "_current_owner_uid", lambda: os.geteuid() + 1)
        assert main(["crew", "inbox", "--owner", "local"]) == 0
        uid_inbox = capsys.readouterr().out
    assert "No Crew activity found" in uid_inbox
    assert atlas.coworker_id not in uid_inbox
    assert scout.coworker_id not in uid_inbox

    skipped_at = (fixed_now - timedelta(hours=2)).astimezone(timezone.utc)
    skipped_event_key = f"scheduled:{skipped_at.isoformat()}"
    skipped_run_id = str(
        uuid5(
            UUID(scout_responsibility_id),
            f"ash-crew-scheduled-trigger:{skipped_event_key}",
        )
    )
    with sqlite3.connect(directory.db_path) as connection:
        connection.execute(
            "INSERT INTO coworker_turn_dispatches "
            "(owner_id, coworker_id, run_id, session_id, request_id, status, "
            "execution_kind, responsibility_id, trigger_type, event_key, created_at, "
            "completed_at, scheduled_for, skip_reason, workspace_device, workspace_inode) "
            "VALUES (?, ?, ?, NULL, NULL, 'skipped', 'legacy', ?, 'scheduled', ?, ?, "
            "NULL, ?, 'misfire_grace_expired', NULL, NULL)",
            (
                "local",
                scout.coworker_id,
                skipped_run_id,
                scout_responsibility_id,
                skipped_event_key,
                fixed_now.isoformat(),
                skipped_at.isoformat(),
            ),
        )
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    quiet_inbox = capsys.readouterr().out
    assert skipped_run_id not in quiet_inbox
    assert main(
        ["crew", "inbox", "--owner", "local", "--include-skipped"]
    ) == 0
    included_skips = capsys.readouterr().out
    assert f"run:{scout.coworker_id}:{skipped_run_id}" in included_skips
    assert "Skipped because its scheduled start window expired." in included_skips

    assert main(
        [
            "crew",
            "responsibility",
            "run",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_manual_responsibility_id,
            "--event-id",
            "owner-follow-up-1",
            "--check-model",
        ]
    ) == 0
    manual_output = capsys.readouterr().out
    assert "Trigger: manual" in manual_output
    assert "Evidence used: src/protected.py" in manual_output
    assert "Event key: owner-follow-up-1" in manual_output
    assert "github-actions" not in manual_output
    assert len(observed) == 6
    assert observed[4]["is_readiness_probe"] is True
    assert observed[5]["is_readiness_probe"] is False
    assert observed[4]["model"] == "gpt-atlas"
    assert observed[5]["model"] == "gpt-atlas"
    assert observed[4]["tools"] is None
    assert observed[5]["tools"] is None
    assert observed[4]["provider_id"] == observed[5]["provider_id"]
    manual_message = next(
        str(message["content"])
        for message in observed[5]["messages"]
        if message["role"] == "user"
    )
    manual_contract = json.loads(manual_message.rsplit("\n", 1)[1])
    assert [item["path"] for item in manual_contract["evidence"]] == [
        "src/protected.py"
    ]
    assert manual_contract["evidence"][0]["text"] == "VALUE = 1\n"
    assert manual_contract["evidence"][0]["sha256"] == hashlib.sha256(
        protected.read_bytes()
    ).hexdigest()
    monkeypatch.setattr(coworkers, "_current_time", lambda: fixed_now)
    stale_run_id = str(uuid4())
    stale_run_ref = atlas_handle.begin_background_run(stale_run_id)
    directory._claim_responsibility_turn(
        handle=atlas_handle,
        run_id=stale_run_id,
        session_id=stale_run_ref.session_id,
        request_id=str(uuid4()),
    )
    assert main(event_args) == 2
    duplicate = capsys.readouterr()
    assert "already used" in duplicate.err or "already claimed" in duplicate.err
    assert len(observed) == 6
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    duplicate_inbox = capsys.readouterr().out
    assert duplicate_inbox.count(
        f"run:{atlas.coworker_id}:{atlas_event_run_id}"
    ) == 1
    assert f"run:{atlas.coworker_id}:{stale_run_id}" in duplicate_inbox
    assert "Cause: ask" in duplicate_inbox
    assert "Outcome unknown; this claim will not be replayed." in duplicate_inbox
    assert f"approval:{atlas_approval_id}" in duplicate_inbox

    assert main(
        ["crew", "archive", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 0
    archived_output = capsys.readouterr().out
    assert "Lifecycle: archived" in archived_output
    assert "history and decisions remain available for inspection" in archived_output
    assert "Turns claimed before this lifecycle change may still dispatch or finish." in archived_output
    assert main(
        ["crew", "list", "--owner", "local"]
    ) == 0
    listed = capsys.readouterr().out
    assert f"ID: {atlas.coworker_id}" in listed
    assert "Lifecycle: archived" in listed
    assert f"ID: {scout.coworker_id}" in listed
    assert "Lifecycle: active" in listed
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
    assert "Lifecycle was not changed from archived" in capsys.readouterr().out
    assert main(
        ["crew", "resume", "--owner", "local", "--coworker-id", atlas.coworker_id]
    ) == 2
    assert "cannot change coworker lifecycle from archived to active" in capsys.readouterr().err
    assert main(
        _ask_args(atlas.coworker_id, "Try starting another turn.", turn_id="after-archive")
    ) == 2
    assert "coworker is archived" in capsys.readouterr().err
    assert len(observed) == 6
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is archived"):
        atlas_handle.create_responsibility(
            scope="Must remain disabled",
            objective="This responsibility must not be persisted.",
        )

    event_fields["id"] = "atlas-ci-after-archive"
    event_fields.pop("signature")
    canonical = json.dumps(
        event_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    event_fields["signature"] = hmac.new(
        signing_key, canonical, hashlib.sha256
    ).hexdigest()
    event_file.write_text(
        json.dumps(event_fields, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    assert main(event_args) == 2
    assert "coworker is archived" in capsys.readouterr().err
    assert len(observed) == 6

    assert main(
        [
            "crew",
            "responsibility",
            "run",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
            "--responsibility-id",
            scout_responsibility_id,
            "--event-id",
            "scout-after-atlas-archive",
        ]
    ) == 0
    assert "Trigger: manual" in capsys.readouterr().out
    assert len(observed) == 7

    assert main(
        [
            "crew",
            "responsibility",
            "runs",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--responsibility-id",
            atlas_responsibility_id,
        ]
    ) == 0
    archived_runs = capsys.readouterr().out
    assert "Status: completed" in archived_runs
    assert "Atlas recommends inspecting this failure" in archived_runs
    assert main(
        [
            "crew",
            "responsibility",
            "list",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
        ]
    ) == 0
    assert atlas_responsibility_id in capsys.readouterr().out

    reopened = CoworkerDirectory.open(ash_dir)
    atlas_handle = reopened.open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    )
    scout_handle = reopened.open_coworker(
        owner_id="local",
        coworker_id=scout.coworker_id,
    )
    atlas_run, = atlas_handle.list_responsibility_runs(atlas_responsibility_id)
    scout_runs = scout_handle.list_responsibility_runs(scout_responsibility_id)
    assert len(scout_runs) == 3
    scout_run = next(run for run in scout_runs if run.trigger_type == "scheduled")
    scout_after_archive = next(
        run for run in scout_runs if run.event_key == "scout-after-atlas-archive"
    )
    scout_skipped_run = next(run for run in scout_runs if run.run_id == skipped_run_id)
    assert scout_skipped_run.status == "skipped"
    manual_run, = atlas_handle.list_responsibility_runs(atlas_manual_responsibility_id)
    assert atlas_run.execution_kind == scout_run.execution_kind == "model"
    assert atlas_run.trigger_type == "event"
    assert atlas_run.event_key == "github-actions:atlas-ci-42"
    assert manual_run.trigger_type == "manual"
    assert manual_run.execution_kind == "model"
    assert manual_run.event_key == "owner-follow-up-1"
    assert scout_after_archive.execution_kind == "model"
    assert scout_after_archive.trigger_type == "manual"
    assert manual_run.run_id != atlas_run.run_id
    assert scout_run.trigger_type == "scheduled"
    assert scout_run.event_key == f"scheduled:{scheduled.next_fire_at.isoformat()}"
    assert atlas_run.session_id is not None and scout_run.session_id is not None
    assert atlas_run.session_id != scout_run.session_id
    assert manual_run.session_id is not None
    assert manual_run.session_id != atlas_run.session_id
    atlas_session, _ = atlas_handle.load_session(atlas_run.session_id)
    scout_session, _ = scout_handle.load_session(scout_run.session_id)
    assert atlas_session.model == "openai/gpt-atlas"
    assert atlas_handle.require_owner_binding().lifecycle_status == "archived"
    assert scout_session.model == "openai/gpt-scout"
    assert [message.role for message in atlas_session.messages] == ["user", "assistant"]
    assert [message.role for message in scout_session.messages] == ["user", "assistant"]
    assert not atlas_session.tool_calls and not scout_session.tool_calls
    assert scout_handle.get_responsibility(scout_responsibility_id).next_fire_at == (
        scheduled.next_fire_at + timedelta(minutes=10)
    )

    assert main(["crew", "inbox", "--owner", "local"]) == 0
    archived_inbox = capsys.readouterr().out
    assert f"Coworker: Atlas ({atlas.coworker_id})" in archived_inbox
    assert f"approval:{atlas_approval_id}" in archived_inbox
    assert "Lifecycle: archived" in archived_inbox
    assert f"run:{atlas.coworker_id}:{stale_run_id}" in archived_inbox
    assert "Outcome unknown; this claim will not be replayed." in archived_inbox

    child_env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, child_env.get("PYTHONPATH", "")) if part
    )
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.run(
        [sys.executable, "-m", "ash", "crew", "inbox", "--owner", "local"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert f"run:{atlas.coworker_id}:{atlas_event_run_id}" in child.stdout
    assert f"approval:{atlas_approval_id}" in child.stdout
    assert f"run:{scout.coworker_id}:{scout_scheduled_run_id}" in child.stdout
    assert f"run:{atlas.coworker_id}:{stale_run_id}" in child.stdout
    assert skipped_run_id not in child.stdout
    assert "outcome unknown; this claim will not be replayed" in child.stdout.casefold()
    assert "APPROVED: Atlas recommends inspecting this failure." not in child.stdout
    assert "atlas-profile-secret" not in child.stdout
    assert "scout-profile-secret" not in child.stdout
    assert "VALUE = 1" not in child.stdout
    assert "VALUE = 2" not in child.stdout

    assert len(observed) == 7


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_dispatched_provider_turn_finishes_once_after_pause_and_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerTurnRequest

    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    _, observed = _install_fake_registry(
        monkeypatch,
        started=started,
        release=release,
    )
    directory = CoworkerDirectory.open(ash_dir)
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
    workspace_identity = SafetyGuard(workspace).project_root_identity
    assert workspace_identity is not None
    request = CoworkerTurnRequest(
        turn_id="claimed-before-pause",
        question="Finish this already dispatched read-only request.",
        workspace_identity=workspace_identity,
    )

    async def pause_after_dispatch() -> Any:
        task = asyncio.create_task(handle.run_readonly_turn(request))
        await asyncio.wait_for(started.wait(), timeout=10)
        handle.pause()
        handle.archive()
        release.set()
        return await task

    result = asyncio.run(pause_after_dispatch())
    assert result.response.startswith("Atlas found the failed assertion")
    assert len(observed) == 1
    history = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="local",
        coworker_id=identity.coworker_id,
    ).list_readonly_turn_history()
    assert len(history) == 1
    assert history[0].status == "completed"
    assert history[0].session_ref.run_id == result.session_ref.run_id
    assert history[0].response == result.response
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is archived"):
        asyncio.run(
            handle.run_readonly_turn(
                CoworkerTurnRequest(
                    turn_id="must-not-dispatch-after-archive",
                    question="This new request must be rejected.",
                    workspace_identity=workspace_identity,
                )
            )
        )
    assert len(observed) == 1


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_pause_before_durable_claim_blocks_provider_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.coworkers import CoworkerTurnRequest

    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    directory = CoworkerDirectory.open(ash_dir)
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
    workspace_identity = SafetyGuard(workspace).project_root_identity
    assert workspace_identity is not None
    request = CoworkerTurnRequest(
        turn_id="pause-at-claim",
        question="The final claim must recheck coworker lifecycle state.",
        workspace_identity=workspace_identity,
    )
    claim = directory._claim_responsibility_turn

    def pause_then_claim(**kwargs: Any) -> Any:
        handle.pause()
        return claim(**kwargs)

    monkeypatch.setattr(directory, "_claim_responsibility_turn", pause_then_claim)
    with pytest.raises(CoworkerRuntimeUnavailable, match="coworker is paused"):
        asyncio.run(handle.run_readonly_turn(request))
    assert not observed
    assert handle.list_readonly_turn_history() == ()
    assert handle.require_owner_binding().lifecycle_status == "paused"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize(
    "mutation",
    ["missing", "symlink", "oversize", "malformed_json", "traversal", "private_path"],
)
def test_changed_or_forged_responsibility_evidence_blocks_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    selected = workspace / "selected.log"
    selected.write_text("selected at creation", encoding="utf-8")
    directory = CoworkerDirectory.open(ash_dir)
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
    responsibility = handle.create_responsibility(
        scope="CI investigation",
        objective="Use only the selected log.",
        evidence_paths=("selected.log",),
    )

    if mutation == "missing":
        selected.unlink()
    elif mutation == "symlink":
        outside = tmp_path / "outside.log"
        outside.write_text("outside workspace", encoding="utf-8")
        selected.unlink()
        selected.symlink_to(outside)
    elif mutation == "oversize":
        selected.write_bytes(b"x" * (6 * 1024 + 1))
    else:
        grant = {
            "malformed_json": "not-json",
            "traversal": '["../outside.log"]',
            "private_path": '[".ssh/config"]',
        }[mutation]
        with sqlite3.connect(directory.db_path) as connection:
            connection.execute(
                "UPDATE coworker_responsibilities SET evidence_paths_json = ? "
                "WHERE responsibility_id = ?",
                (grant, responsibility.responsibility_id),
            )

    assert main(
        [
            "crew",
            "responsibility",
            "run",
            "--owner",
            "local",
            "--coworker-id",
            identity.coworker_id,
            "--responsibility-id",
            responsibility.responsibility_id,
            "--event-id",
            f"blocked-{mutation}",
            "--check-model",
        ]
    ) == 2
    blocked = capsys.readouterr()
    assert "blocked before provider dispatch" in blocked.err.casefold()
    assert not observed
    assert handle.list_responsibility_runs(responsibility.responsibility_id) == []


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize("evidence_path", ["missing.log", "../outside.log", ".ssh/config"])
def test_responsibility_creation_rejects_invalid_evidence_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_path: str,
) -> None:
    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    directory = CoworkerDirectory.open(ash_dir)
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

    with pytest.raises(ValueError):
        handle.create_responsibility(
            scope="CI investigation",
            objective="Use only the selected log.",
            evidence_paths=(evidence_path,),
        )
    assert handle.list_responsibilities() == []


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
@pytest.mark.parametrize(
    "change",
    ["source", "grant", "timestamp", "pause", "pause_resume_same_timestamp"],
)
def test_responsibility_change_between_inspection_and_claim_blocks_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from ash.coworkers import CoworkerRuntimeUnavailable

    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    selected = workspace / "selected.log"
    selected.write_text("first observed log", encoding="utf-8")
    (workspace / "other.log").write_text("second log", encoding="utf-8")
    directory = CoworkerDirectory.open(ash_dir)
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
    responsibility = handle.create_responsibility(
        scope="CI investigation",
        objective="Use only the selected log.",
        evidence_paths=("selected.log",),
    )
    real_claim = directory._claim_responsibility_turn

    def change_before_claim(**kwargs: Any) -> Any:
        if change == "source":
            selected.write_text("changed before claim", encoding="utf-8")
        elif change == "grant":
            with sqlite3.connect(directory.db_path) as connection:
                connection.execute(
                    "UPDATE coworker_responsibilities SET evidence_paths_json = ? "
                    "WHERE responsibility_id = ?",
                    ('["other.log"]', responsibility.responsibility_id),
                )
        elif change == "timestamp":
            with sqlite3.connect(directory.db_path) as connection:
                connection.execute(
                    "UPDATE coworker_responsibilities SET updated_at = ? "
                    "WHERE responsibility_id = ?",
                    ("2030-01-01T00:00:00+00:00", responsibility.responsibility_id),
                )
        elif change == "pause":
            handle.set_responsibility_status(
                responsibility.responsibility_id,
                "paused",
            )
        else:
            import ash.coworkers as coworkers

            monkeypatch.setattr(
                coworkers,
                "_now",
                lambda: responsibility.updated_at.isoformat(),
            )
            handle.set_responsibility_status(
                responsibility.responsibility_id,
                "paused",
            )
            handle.set_responsibility_status(
                responsibility.responsibility_id,
                "active",
            )
        return real_claim(**kwargs)

    monkeypatch.setattr(directory, "_claim_responsibility_turn", change_before_claim)
    with pytest.raises(CoworkerRuntimeUnavailable, match="evidence|active"):
        import asyncio

        asyncio.run(
            handle.run_model_responsibility(
                responsibility.responsibility_id,
                event_id=f"state-change-{change}",
            )
        )
    assert not observed
    assert handle.list_responsibility_runs(responsibility.responsibility_id) == []


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_combined_event_and_evidence_prompt_limit_blocks_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import hmac
    from datetime import datetime, timezone

    ash_dir, workspace, _ = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(monkeypatch)
    evidence = workspace / "ci.log"
    evidence.write_text("E" * (6 * 1024), encoding="utf-8")
    directory = CoworkerDirectory.open(ash_dir)
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
    responsibility = handle.create_responsibility(
        scope="S" * 1_000,
        objective="O" * 6_000,
        success_criteria=("C" * 500,),
        evidence_paths=("ci.log",),
    )
    signing_key = bytes(range(32))
    key_file = tmp_path / "event.key"
    key_file.write_text(signing_key.hex() + "\n", encoding="ascii")
    key_file.chmod(0o600)
    fields = {
        "version": 1,
        "source": "github-actions",
        "id": "large-event-1",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "responsibility_id": responsibility.responsibility_id,
        "payload": "E" * (8 * 1024),
    }
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = json.dumps(
        {
            **fields,
            "signature": hmac.new(signing_key, canonical, hashlib.sha256).hexdigest(),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(ValueError, match="prompt exceeds"):
        asyncio.run(
            handle.run_signed_event(
                envelope,
                key_file=key_file,
                responsibility_id=responsibility.responsibility_id,
            )
        )
    assert not observed
    assert handle.list_responsibility_runs(responsibility.responsibility_id) == []




@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_atlas_signed_ci_event_flows_into_exact_owner_approved_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio
    import hmac
    from datetime import datetime, timezone

    from ash.coworkers import CoworkerRuntimeUnavailable

    ash_dir, atlas_workspace, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    _, observed = _install_fake_registry(
        monkeypatch,
        atlas_answer="A CI assertion failed; recommend VALUE = 2. APPROVED",
    )
    target = atlas_workspace / "src" / "check.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (atlas_workspace / "src" / "check.fixed.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    ci_log = atlas_workspace / "ci-failure.txt"
    ci_log.write_text(
        "FAILED tests/test_check.py::test_value expected VALUE = 2\n",
        encoding="utf-8",
    )
    directory = CoworkerDirectory.open(ash_dir)
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
    atlas_handle = directory.open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    )
    scout_handle = directory.open_coworker(
        owner_id="local",
        coworker_id=scout.coworker_id,
    )
    responsibility = atlas_handle.create_responsibility(
        scope="Synthetic CI",
        objective="Investigate a failed test and propose a correction.",
        success_criteria=("Explain the assertion mismatch.",),
        evidence_paths=("ci-failure.txt", "src/check.py"),
    )
    signing_key = bytes(range(32))
    key_file = tmp_path / "event.key"
    key_file.write_text(signing_key.hex() + "\n", encoding="ascii")
    key_file.chmod(0o600)
    event_fields = {
        "version": 1,
        "source": "github-actions",
        "id": "failing-run-42",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "responsibility_id": responsibility.responsibility_id,
        "payload": "Synthetic test failed: expected VALUE=2, observed VALUE=1.",
    }
    canonical = json.dumps(
        event_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    envelope = json.dumps(
        {
            **event_fields,
            "signature": hmac.new(signing_key, canonical, hashlib.sha256).hexdigest(),
        }
    ).encode("utf-8")
    completed = asyncio.run(
        atlas_handle.run_signed_event(
            envelope,
            key_file=key_file,
            responsibility_id=responsibility.responsibility_id,
        )
    )
    assert completed.trigger_type == "event"
    assert completed.responsibility_id == responsibility.responsibility_id
    assert completed.response and len(observed) == 1
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    message = next(
        str(item["content"])
        for item in observed[0]["messages"]
        if item["role"] == "user"
    )
    contract = json.loads(message.rsplit("\n", 1)[1])
    assert [item["path"] for item in contract["evidence"]] == [
        "ci-failure.txt",
        "src/check.py",
    ]
    assert contract["evidence"][0]["sha256"] == hashlib.sha256(
        ci_log.read_bytes()
    ).hexdigest()
    assert contract["evidence"][0]["text"] == ci_log.read_text(encoding="utf-8")

    source_ids = {
        "run_id": completed.session_ref.run_id,
        "session_id": completed.session_ref.session_id,
        "request_id": completed.request_id,
    }
    with pytest.raises(CoworkerRuntimeUnavailable):
        scout_handle.prepare_file_approval(
            **source_ids,
            target_path="src/check.py",
            replacement_file_path="src/check.fixed.py",
        )
    # A dispatch claimed as offline or attached to another event is not an
    # eligible model-backed source, even when the transcript still exists.
    for column, changed_value, original in (
        ("execution_kind", "offline", "model"),
        ("event_key", "github-actions:forged-run", "github-actions:failing-run-42"),
    ):
        with sqlite3.connect(directory.db_path) as connection:
            connection.execute(
                f"UPDATE coworker_turn_dispatches SET {column} = ? WHERE run_id = ?",
                (changed_value, source_ids["run_id"]),
            )
        with pytest.raises(CoworkerRuntimeUnavailable):
            atlas_handle.prepare_file_approval(
                **source_ids,
                target_path="src/check.py",
                replacement_file_path="src/check.fixed.py",
            )
        with sqlite3.connect(directory.db_path) as connection:
            connection.execute(
                f"UPDATE coworker_turn_dispatches SET {column} = ? WHERE run_id = ?",
                (original, source_ids["run_id"]),
            )
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert main(
        [
            "crew",
            "propose",
            "--owner",
            "local",
            "--coworker-id",
            atlas.coworker_id,
            "--run-id",
            source_ids["run_id"],
            "--session-id",
            source_ids["session_id"],
            "--request-id",
            source_ids["request_id"],
            "--target",
            "src/check.py",
            "--replacement-file",
            "src/check.fixed.py",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "Status: pending" in output
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    approval_id = next(
        line.removeprefix("Approval ID: ")
        for line in output.splitlines()
        if line.startswith("Approval ID: ")
    )
    approval_digest = next(
        line.removeprefix("Approval digest: ")
        for line in output.splitlines()
        if line.startswith("Approval digest: ")
    )
    reopened = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="local",
        coworker_id=atlas.coworker_id,
    )
    pending, _ = reopened.inspect_file_approval(approval_id)
    assert pending.source_run_id == completed.session_ref.run_id
    assert pending.source_session_id == completed.session_ref.session_id
    assert pending.status == "pending"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    expected_challenges = [
        f"APPROVE {approval_id} {approval_digest}",
        f"APPLY {approval_id} {approval_digest}",
    ]

    def confirm_exact_challenge(challenge: str) -> bool:
        assert challenge == expected_challenges.pop(0)
        return True

    monkeypatch.setattr("ash.commands.crew._require_tty_confirmation", confirm_exact_challenge)
    for action in ("approve", "apply"):
        assert main(
            [
                "crew", action,
                "--owner", "local",
                "--coworker-id", atlas.coworker_id,
                "--approval-id", approval_id,
            ]
        ) == 0
        if action == "approve":
            assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not expected_challenges
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    final, events = reopened.inspect_file_approval(approval_id)
    assert final.status == "succeeded"
    assert [event.status for event in events] == [
        "pending", "approved", "dispatching", "succeeded"
    ]
    with pytest.raises(CoworkerRuntimeUnavailable, match="consumed"):
        reopened._apply_file_approval(approval_id)
    assert main(["crew", "inbox", "--owner", "local"]) == 0
    applied_inbox = capsys.readouterr().out
    assert applied_inbox.count(f"approval:{approval_id}") == 1
    assert "Status: Applied." in applied_inbox
    assert len(observed) == 1

@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Crew requires a local effective UID")
def test_missing_provider_keeps_model_schedule_due_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import datetime, timezone

    import ash.coworkers as coworkers
    from ash.automation.schedules import build_schedule
    from ash.providers.registry import ProviderRegistry

    ash_dir, _, scout_workspace = _prepare_profiles(tmp_path, monkeypatch)
    schedule_evidence = scout_workspace / "research-note.txt"
    schedule_evidence.write_text("Scheduled evidence remains selected.", encoding="utf-8")
    _, observed = _install_fake_registry(monkeypatch)
    fixed_now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    monkeypatch.setattr("ash.automation.schedules.utc_now", lambda: fixed_now)
    monkeypatch.setattr(coworkers, "_now", lambda: fixed_now.isoformat())
    directory = CoworkerDirectory.open(ash_dir)
    scout = directory.create(
        owner_id="local",
        display_name="Scout",
        profile_name="scout",
        workspace=scout_workspace,
    )
    responsibility = directory.open_coworker(
        owner_id="local",
        coworker_id=scout.coworker_id,
    ).create_responsibility(
        responsibility_id="70467850-1d12-45eb-8872-826012b1adac",
        scope="Scheduled research",
        objective="Summarize the assigned research question.",
        success_criteria=("Return one concise finding.",),
        evidence_paths=("research-note.txt",),
        schedule=build_schedule(every="10m", now=fixed_now),
        misfire_grace_seconds=3_600,
    )
    assert responsibility.next_fire_at is not None
    monkeypatch.setattr(coworkers, "_current_time", lambda: responsibility.next_fire_at)
    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        ProviderRegistry,
    )

    assert main(
        [
            "crew",
            "tick",
            "--owner",
            "local",
            "--coworker-id",
            scout.coworker_id,
        ]
    ) == 2
    blocked = capsys.readouterr()
    assert "provider is unavailable" in blocked.err
    assert "scheduled occurrence remains due" in blocked.err
    assert not observed
    reopened = CoworkerDirectory.open(ash_dir).open_coworker(
        owner_id="local",
        coworker_id=scout.coworker_id,
    )
    assert reopened.get_responsibility(responsibility.responsibility_id).next_fire_at == (
        responsibility.next_fire_at
    )
    assert reopened.list_responsibility_runs(responsibility.responsibility_id) == []
