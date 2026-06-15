# tests/unit/test_subprocess_agent.py
import pytest
from ash.agents.subprocess_agent import SubprocessAgent, make_simple_text_task
from ash.agents.shared_state import SharedState
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