# tests/unit/test_subprocess_agent.py
from unittest.mock import Mock

import pytest

from ash.agents.subprocess_agent import (
    MAX_SUBPROCESS_SPEC_BYTES,
    SubprocessAgent,
    make_simple_text_task,
)
from ash.agents.shared_state import SharedState
from ash.sandbox._base import SANDBOX_TIER_SCOPED
import tempfile
from pathlib import Path


@pytest.fixture
def shared_state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")


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
