"""Observable recovery behavior when provider replay state cannot be bound."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.exceptions import ErrorCategory, classify_exception
from ash.hooks.registry import HookRegistry, LifecycleHook
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.replay_affinity import (
    ProviderReplayAffinityError,
    verify_provider_replay_identity,
)
from ash.providers.replay_state import ProviderReplayCredentialIdentity
from ash.safety.guard import SafetyGuard
from ash.tools.filesystem import WriteFileTool
from ash.ui.terminal import TerminalUI


class CapturingUI(TerminalUI):
    def __init__(self) -> None:
        super().__init__(safety_tier="auto_approve")
        self.events: list[dict[str, Any]] = []

    def emit_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)

    def print_token(self, _text: str) -> None:
        return None


class OpaqueStateProvider(ProviderABC):
    provider_family = "replaytest"
    model_name = "unbound-test"
    _ash_declared_capabilities = ProviderCapabilities(
        native_tools=True,
        reasoning=True,
    )

    def __init__(self, *, identity_available: bool = True) -> None:
        self.calls = 0
        self.identity_available = identity_available

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def replay_credential_identity(
        self,
    ) -> ProviderReplayCredentialIdentity | None:
        if not self.identity_available:
            return None
        return ProviderReplayCredentialIdentity.from_secret("synthetic-test-only")

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ):
        del messages, temperature, tools
        verify_provider_replay_identity(
            self.replay_credential_identity(),
            provider_family=self.provider_family,
            model_name=self.model_name,
        )
        self.calls += 1
        yield StreamChunk(
            content="VISIBLE_SUCCESS",
            provider_state=[
                {
                    "type": "reasoning",
                    "id": "rs-public-test",
                    "summary": [],
                    "encrypted_content": "synthetic-opaque-state",
                }
            ],
            is_done=True,
            stop_reason="stop",
        )


class EffectThenOpaqueStateProvider(OpaqueStateProvider):
    def __init__(self) -> None:
        super().__init__()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ):
        del messages, temperature, tools
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "approved-write-1",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "approved-effect.txt",
                            "content": "executed once",
                        },
                    }
                ],
                is_done=True,
                stop_reason="tool_calls",
            )
            return
        yield StreamChunk(
            content="VISIBLE_AFTER_EFFECT",
            native_tool_calls=[
                {
                    "id": "unbound-write-2",
                    "name": "write_file",
                    "arguments": {
                        "file_path": "unapproved-effect.txt",
                        "content": "must not be written",
                    },
                }
            ],
            provider_state=[
                {
                    "type": "reasoning",
                    "id": "rs-effect-test",
                    "summary": [],
                    "encrypted_content": "synthetic-effect-state",
                }
            ],
            is_done=True,
            stop_reason="stop",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["private_store_unavailable", "corrupt_key", "missing_identity"],
)
async def test_unbound_provider_state_is_persisted_as_failed_turn(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    database = tmp_path / "db" / "sessions.db"
    store = SessionStore(database)
    private_store = database.parent / "provider-replay-state"
    if failure_mode == "private_store_unavailable":
        private_store.write_text("not a directory", encoding="utf-8")
    elif failure_mode == "corrupt_key":
        private_store.mkdir(mode=0o700)
        key_file = private_store / "replay-state-v1.key"
        key_file.write_bytes(b"corrupt synthetic key record")
        key_file.chmod(0o600)
    provider = OpaqueStateProvider(identity_available=failure_mode != "missing_identity")
    ui = CapturingUI()
    lifecycle: list[dict[str, Any]] = []

    async def capture_lifecycle(payload: dict[str, Any]) -> None:
        lifecycle.append(payload)

    hooks = HookRegistry()
    for event in ("post_model", "turn_error", "turn_end"):
        hooks.register_lifecycle(LifecycleHook(event, capture_lifecycle))
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
        hooks=hooks,
        config=AshConfig(
            model="replaytest/unbound-test",
            workspace_root=tmp_path,
            db_directory=tmp_path,
            memory_backend="off",
            provider_max_attempts=1,
        ),
    )

    try:
        session = await loop.start_session()
        initial_error: ProviderReplayAffinityError | None = None
        try:
            await loop.run_turn("Do this task")
        except ProviderReplayAffinityError as exc:
            initial_error = exc

        if initial_error is None:
            saved = store.load_session(session.session_id)
            unbound_message = next(
                message
                for message in reversed(saved.messages)
                if message.role == "assistant"
                and message.metadata.get("provider_state")
            )
            assert unbound_message.content == "VISIBLE_SUCCESS"
            assert "provider_state_credential_binding" not in unbound_message.metadata
            try:
                await loop.run_turn("Please continue")
            except RuntimeError as exc:
                continuation_error = str(exc)
            else:
                continuation_error = "continuation unexpectedly returned"
            pytest.fail(
                "red baseline: first run returned VISIBLE_SUCCESS and persisted "
                "opaque state without a credential binding; the next run failed "
                f"with {continuation_error!r} (failure mode: {failure_mode})"
            )

        assert "/restart-context" in str(initial_error)
        classified = classify_exception(initial_error)
        assert classified.category is ErrorCategory.CONTEXT
        assert classified.exit_code == 1
        assert not classified.retriable
        assert "ash sessions restart-context --session SESSION --handoff TEXT" in (
            classified.remedy
        )
        assert provider.calls == 1
        saved = store.load_session(session.session_id)
        assistant = next(
            message
            for message in reversed(saved.messages)
            if message.role == "assistant" and message.metadata.get("provider_state")
        )
        assert assistant.content == "VISIBLE_SUCCESS"
        assert "provider_state_credential_binding" not in assistant.metadata
        assert any(
            event.get("type") == "assistant.delta"
            and event.get("text") == "VISIBLE_SUCCESS"
            for event in ui.events
        )
        assert any(
            event.get("type") == "turn.error"
            and event.get("stop_reason") == "provider_replay_binding"
            for event in ui.events
        )
        assert [
            (item["event"], item.get("status")) for item in lifecycle
        ] == [
            ("post_model", "error"),
            ("turn_error", None),
            ("turn_end", "error"),
        ]
        assert "synthetic-test-only" not in repr(lifecycle)
        assert "synthetic-opaque-state" not in repr(lifecycle)
        assert "synthetic-test-only" not in repr(ui.events)
        assert "synthetic-opaque-state" not in repr(ui.events)
        assert not any(event.get("type") == "turn.completed" for event in ui.events)
        started = next(
            event for event in ui.events if event.get("type") == "turn.started"
        )
        assert store.turn_journal_status(
            session.session_id,
            str(started["turn_id"]),
        ) == "interrupted"
        assert store.latest_incomplete_turn_reason(session.session_id) == (
            "provider_replay_binding"
        )

        with pytest.raises(ProviderReplayAffinityError, match="/restart-context"):
            await loop.run_turn("Please continue")
        assert provider.calls == 1
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_headless_cli_reports_binding_failure_as_incomplete(
    tmp_path: Path,
) -> None:
    import io
    import json
    from types import SimpleNamespace

    from ash.cli import _bootstrap_and_headless
    from ash.ui.headless import HeadlessUI

    class FailedTurnLoop:
        provider = None
        safety_guard = None
        _last_interrupted_edit_files = 0

        async def start_session(self, session_id: str | None = None) -> Any:
            del session_id
            return SimpleNamespace(session_id="synthetic-session")

        async def run_turn(self, _prompt: str) -> str:
            raise ProviderReplayAffinityError

        async def aclose(self) -> None:
            return None

    stream = io.StringIO()
    code = await _bootstrap_and_headless(
        FailedTurnLoop(),
        SimpleNamespace(model="replaytest/unbound-test"),
        prompt="Do this task",
        session_id=None,
        ui=HeadlessUI(output_format="stream-json", stream=stream),
    )
    events = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert code == 1
    assert [event["type"] for event in events] == ["error"]
    assert events[0]["error"]["category"] == ErrorCategory.CONTEXT.value
    assert events[0]["error"]["retriable"] is False
    assert "ash sessions restart-context --session SESSION --handoff TEXT" in (
        events[0]["error"]["remedy"]
    )


@pytest.mark.asyncio
async def test_rotated_private_key_refuses_old_binding_before_dispatch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db" / "sessions.db"
    store = SessionStore(database)
    provider = OpaqueStateProvider()
    first_loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        CapturingUI(),
        tmp_path,
        config=AshConfig(
            model="replaytest/unbound-test",
            workspace_root=tmp_path,
            db_directory=database.parent,
            memory_backend="off",
            provider_max_attempts=1,
        ),
    )

    try:
        session = await first_loop.start_session()
        assert await first_loop.run_turn("Persist bound state") == "VISIBLE_SUCCESS"
        saved = store.load_session(session.session_id)
        assistant = next(
            message
            for message in reversed(saved.messages)
            if message.role == "assistant" and message.metadata.get("provider_state")
        )
        assert "provider_state_credential_binding" in assistant.metadata
    finally:
        await first_loop.aclose()

    key_files = list((database.parent / "provider-replay-state").glob("*.key"))
    assert len(key_files) == 1
    key_file = key_files[0]
    key_file.chmod(0o600)
    key_record = key_file.read_bytes()
    assert len(key_record) > 1
    key_file.write_bytes(key_record[:-1] + bytes([key_record[-1] ^ 0xFF]))
    key_file.chmod(0o600)

    resumed_store = SessionStore(database)
    resumed_loop = AshLoop(
        resumed_store,
        provider,
        SafetyGuard(tmp_path),
        CapturingUI(),
        tmp_path,
        config=AshConfig(
            model="replaytest/unbound-test",
            workspace_root=tmp_path,
            db_directory=database.parent,
            memory_backend="off",
            provider_max_attempts=1,
        ),
    )
    try:
        await resumed_loop.start_session(session.session_id)
        with pytest.raises(ProviderReplayAffinityError, match="/restart-context"):
            await resumed_loop.run_turn("Continue after key rotation")
        assert provider.calls == 1
    finally:
        await resumed_loop.aclose()


@pytest.mark.asyncio
async def test_binding_failure_keeps_completed_effect_and_blocks_replay(
    tmp_path: Path,
) -> None:
    from ash.core.context_continuation import restart_context_with_handoff

    database = tmp_path / "db" / "sessions.db"
    store = SessionStore(database)
    (database.parent / "provider-replay-state").write_text(
        "not a directory",
        encoding="utf-8",
    )
    provider = EffectThenOpaqueStateProvider()
    effect_tool = WriteFileTool(SafetyGuard(tmp_path))
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        CapturingUI(),
        tmp_path,
        tools={effect_tool.name: effect_tool},
        safety_tier="auto_approve",
        config=AshConfig(
            model="replaytest/unbound-test",
            workspace_root=tmp_path,
            db_directory=database.parent,
            memory_backend="off",
            provider_max_attempts=1,
        ),
    )

    try:
        session = await loop.start_session()
        try:
            await loop.run_turn("Perform the authorized effect, then answer")
        except ProviderReplayAffinityError:
            pass
        else:
            pytest.fail(
                "red baseline: the turn returned success after its later opaque "
                "state could not be bound"
            )

        assert provider.calls == 2
        assert (tmp_path / "approved-effect.txt").read_text(encoding="utf-8") == (
            "executed once"
        )
        assert not (tmp_path / "unapproved-effect.txt").exists()
        saved = store.load_session(session.session_id)
        assert len(saved.tool_calls) == 2
        effect = next(
            call for call in saved.tool_calls if call.call_id == "approved-write-1"
        )
        assert (effect.approved, effect.dispatched, effect.executed, effect.success) == (
            True,
            True,
            True,
            True,
        )
        refused_effect = next(
            call for call in saved.tool_calls if call.call_id == "unbound-write-2"
        )
        assert (
            refused_effect.approved,
            refused_effect.dispatched,
            refused_effect.executed,
            refused_effect.success,
        ) == (False, False, False, None)
        assert refused_effect.error is not None
        assert store.verify_audit_log(session.session_id) == []
        assert list(store.iter_audit_logs(session.session_id))

        with pytest.raises(ProviderReplayAffinityError, match="/restart-context"):
            await loop.run_turn("Please continue")
        assert provider.calls == 2
        assert (tmp_path / "approved-effect.txt").read_text(encoding="utf-8") == (
            "executed once"
        )
        assert not (tmp_path / "unapproved-effect.txt").exists()
        parent_before_restart = store.load_session(session.session_id)
        parent_messages = parent_before_restart.messages
        parent_audit = [
            (entry.log_id, entry.previous_hash, entry.sha256_hash)
            for entry in store.iter_audit_logs(session.session_id)
        ]

        child = restart_context_with_handoff(
            store,
            SafetyGuard(tmp_path),
            session.session_id,
            "Inspect the current workspace and continue from the known effect result.",
        )
        assert len(child.messages) == 1
        assert child.messages[0].role == "user"
        assert child.messages[0].metadata["context_restart"] is True
        assert all(not message.metadata.get("provider_state") for message in child.messages)
        assert all(not message.metadata.get("tool_calls") for message in child.messages)
        assert store.load_session(session.session_id).messages == parent_messages
        assert [
            (entry.log_id, entry.previous_hash, entry.sha256_hash)
            for entry in store.iter_audit_logs(session.session_id)
        ] == parent_audit
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()
