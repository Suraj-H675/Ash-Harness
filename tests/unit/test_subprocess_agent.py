# tests/unit/test_subprocess_agent.py
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from ash.agents.shared_state import SharedState
from ash.agents.subprocess_agent import (
    MAX_SUBPROCESS_SPEC_BYTES,
    AgentReport,
    SubprocessAgent,
    make_simple_text_task,
)
from ash.sandbox._base import SANDBOX_TIER_SCOPED


@pytest.fixture
def shared_state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        state = SharedState(Path(tmpdir) / "test.db")
        try:
            yield state
        finally:
            state.close()


def test_is_tool_allowed_respects_allowlist(shared_state):
    agent = SubprocessAgent(
        agent_id="test-agent",
        role="researcher",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        tool_allowlist=("read_file", "search_code"),
    )
    assert agent.is_tool_allowed("read_file") is True
    assert agent.is_tool_allowed("write_file") is False
    assert agent.is_tool_allowed("run_command") is False


def test_is_tool_allowed_allows_all_when_no_allowlist(shared_state):
    agent = SubprocessAgent(
        agent_id="test-agent",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        tool_allowlist=None,
    )
    assert agent.is_tool_allowed("read_file") is True
    assert agent.is_tool_allowed("write_file") is True
    assert agent.is_tool_allowed("anything") is True


def test_spawn_subprocess_does_not_inherit_provider_secrets(
    shared_state, monkeypatch
):
    from ash.agents import subprocess_agent as subprocess_agent_module

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Mock()

    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-secret")
    monkeypatch.setenv("ASH_MODEL", "openai/private-model")
    monkeypatch.setattr(subprocess_agent_module.subprocess, "Popen", fake_popen)
    workspace = Path(shared_state.db_path).parent / "workspace"

    agent = SubprocessAgent(
        agent_id="isolated-agent",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        workspace_root=workspace,
    )
    agent.spawn_subprocess()

    environment = captured["env"]
    assert captured["command"][1:4] == ["-I", "-m", "ash.agents._agent_driver"]
    assert "test task" not in captured["command"]
    assert "OPENROUTER_API_KEY" not in environment
    assert "ASH_MODEL" not in environment
    assert environment["ASH_WORKSPACE_ROOT"] == str(workspace)


def test_spawn_subprocess_rejects_unserializable_metadata(shared_state) -> None:
    agent = SubprocessAgent(
        agent_id="bad-metadata",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        metadata={"invalid": object()},
    )

    with pytest.raises(ValueError, match="not JSON-serializable"):
        agent.spawn_subprocess()


def test_spawn_subprocess_rejects_oversized_spec(shared_state) -> None:
    agent = SubprocessAgent(
        agent_id="oversized",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        metadata={"large": "x" * MAX_SUBPROCESS_SPEC_BYTES},
    )

    with pytest.raises(ValueError, match="specification exceeds"):
        agent.spawn_subprocess()


@pytest.mark.asyncio
async def test_agent_report_redacts_secrets_before_shared_state_publication(
    shared_state: SharedState,
) -> None:
    secret = "sk-proj-" + "A" * 32

    async def failing_runner(_context):
        raise RuntimeError(f"upstream echoed credential {secret}")

    agent = SubprocessAgent(
        agent_id="redaction-agent",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=failing_runner,
    )

    report = await agent.run_in_process()
    status = shared_state.get_status("redaction-agent")
    messages = shared_state.fetch_messages("lead")

    assert secret not in report.summary
    assert secret not in repr(report.artifacts)
    assert status is not None
    assert secret not in status.current_task
    assert len(messages) == 1
    assert secret not in repr(messages[0].content)
    assert "[REDACTED]" in report.summary


def test_agent_report_payload_redacts_mutated_artifacts(shared_state: SharedState) -> None:
    secret = "xoxb-" + "A" * 24
    report = AgentReport(
        agent_id="redaction-agent",
        role="general",
        task="test task",
        success=True,
        summary="done",
        artifacts={"status": "ok"},
    )
    report.artifacts["late_detail"] = f"credential {secret}"

    payload = SubprocessAgent(
        agent_id="redaction-agent",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
    ).report_to_payload(report)

    assert secret not in repr(payload)
    assert "[REDACTED]" in repr(payload)


def test_subagent_spec_sandbox_tier_default():
    from ash.agents.orchestrator import SubagentSpec

    spec = SubagentSpec(role="coder", task="test")
    assert spec.sandbox_tier == SANDBOX_TIER_SCOPED  # default


def test_subagent_spec_sandbox_tier_override():
    from ash.agents.orchestrator import SubagentSpec
    from ash.sandbox._base import SANDBOX_TIER_SANDBOX_EXEC

    spec = SubagentSpec(
        role="coder", task="test", sandbox_tier=SANDBOX_TIER_SANDBOX_EXEC
    )
    assert spec.sandbox_tier == SANDBOX_TIER_SANDBOX_EXEC
