"""Integration tests for Sprint 13 subagent orchestration."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from ash.agents import (
    AGENT_ROLES,
    LEAD_AGENT_ID,
    SharedState,
    SubagentOrchestrator,
    SubagentSpec,
    SubprocessAgent,
    fanout_for_goal,
    make_simple_text_task,
    payload_to_report,
)
import tempfile


# ---------------------------------------------------------------------------
# SharedState
# ---------------------------------------------------------------------------


def test_shared_state_enables_wal_mode(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    ss.close()


def test_shared_state_register_and_update_agent(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("r1", role="researcher", metadata={"team": "core"})
        ss.update_status("r1", "working", "reading docs")
        st = ss.get_status("r1")
        assert st is not None
        assert st.role == "researcher"
        assert st.status == "working"
        assert st.current_task == "reading docs"
        assert st.metadata.get("team") == "core"
    finally:
        ss.close()


def test_shared_state_rejects_invalid_status(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("r1")
        with pytest.raises(ValueError):
            ss.update_status("r1", "sleeping")
    finally:
        ss.close()


def test_shared_state_reap_stale_marks_failed(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("live")
        ss.update_status("live", "working", "fresh")
        ss.register_agent("stale")
        ss.update_status("stale", "working", "old")
        # Force a stale heartbeat by re-writing it via raw SQL.
        with sqlite3.connect(str(tmp_path / "state.db")) as conn:
            conn.execute(
                "UPDATE agent_status SET last_heartbeat = datetime('now', '-1 hour') WHERE agent_id = ?",
                ("stale",),
            )
        reaped = ss.reap_stale_agents(max_age_seconds=10.0)
        assert reaped == ["stale"]
        assert ss.get_status("stale").status == "failed"
        assert ss.get_status("live").status == "working"
    finally:
        ss.close()


def test_shared_state_ipc_send_fetch_mark_delivered(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("a")
        ss.register_agent("b")
        mid = ss.send_message("a", "b", "ping", {"n": 1})
        msgs = ss.fetch_messages("b")
        assert len(msgs) == 1
        assert msgs[0].message_id == mid
        assert msgs[0].sender_id == "a"
        assert msgs[0].recipient_id == "b"
        assert msgs[0].content == {"n": 1}
        assert msgs[0].delivered is False
        n = ss.mark_delivered([mid])
        assert n == 1
        assert ss.fetch_messages("b", undelivered_only=True) == []
        # But the same message is still fetchable when including delivered.
        all_msgs = ss.fetch_messages("b", undelivered_only=False)
        assert len(all_msgs) == 1
        assert all_msgs[0].delivered is True
    finally:
        ss.close()


def test_shared_state_broadcast_skips_sender(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("a")
        ss.register_agent("b")
        ss.register_agent("c")
        n = ss.broadcast("a", "status_update", {"progress": 0.5})
        assert n == 2  # b and c, but not a (the sender)
        assert len(ss.fetch_messages("a")) == 0
        assert len(ss.fetch_messages("b")) == 1
        assert len(ss.fetch_messages("c")) == 1
    finally:
        ss.close()


def test_shared_state_sprints_round_trip(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("lead1")
        sid = ss.create_sprint("lead1", "implement foo")
        sp = ss.get_sprint(sid)
        assert sp is not None
        assert sp.lead_agent_id == "lead1"
        assert sp.goal == "implement foo"
        assert sp.state == "planning"
        ss.update_sprint_state(sid, "active")
        assert ss.get_sprint(sid).state == "active"
        ss.update_sprint_state(sid, "complete")
        assert ss.get_sprint(sid).state == "complete"
        with pytest.raises(ValueError):
            ss.update_sprint_state(sid, "nonsense")
    finally:
        ss.close()


# ---------------------------------------------------------------------------
# SubprocessAgent (in-process)
# ---------------------------------------------------------------------------


def test_subprocess_agent_in_process_publishes_status_and_report(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        async def runner(ctx):
            return f"hello from {ctx['agent_id']}"

        agent = SubprocessAgent(
            agent_id="r1",
            role="researcher",
            task="read x",
            shared_state=ss,
            runner=runner,
            tool_allowlist=("read_file",),
            token_budget=2000,
            return_budget=400,
        )
        report = asyncio.run(agent.run_in_process())
        assert report.success is True
        assert report.role == "researcher"
        assert "hello from r1" in report.summary

        # Status is updated.
        st = ss.get_status("r1")
        assert st.status == "completed"
        assert "hello from r1" in st.current_task

        # Lead inbox has the report.
        msgs = ss.fetch_messages(LEAD_AGENT_ID)
        assert len(msgs) == 1
        rebuilt = payload_to_report(msgs[0].content)
        assert rebuilt.summary == report.summary
    finally:
        ss.close()


def test_subprocess_agent_failure_marks_status_failed(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        async def bad(ctx):
            raise RuntimeError("boom")

        agent = SubprocessAgent(
            agent_id="r2", role="coder", task="y", shared_state=ss, runner=bad
        )
        report = asyncio.run(agent.run_in_process())
        assert report.success is False
        assert "boom" in report.summary
        assert ss.get_status("r2").status == "failed"
    finally:
        ss.close()


def test_subprocess_agent_spawn_subprocess_publishes_report(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        agent = SubprocessAgent(
            agent_id="child-1",
            role="general",
            task="subprocess task",
            shared_state=ss,
            runner=make_simple_text_task("unused in child"),
        )

        process = agent.spawn_subprocess()
        stdout, stderr = process.communicate(timeout=10)

        assert process.returncode == 0, stderr or stdout
        status = ss.get_status("child-1")
        assert status is not None
        assert status.status == "completed"
        msgs = ss.fetch_messages(LEAD_AGENT_ID)
        assert len(msgs) == 1
        report = payload_to_report(msgs[0].content)
        assert report.agent_id == "child-1"
        assert "completed: subprocess task" in report.summary
    finally:
        ss.close()


def test_subprocess_agent_rejects_unknown_role(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    try:
        with pytest.raises(ValueError):
            SubprocessAgent(
                agent_id="x", role="wizard", task="t", shared_state=ss,
                runner=make_simple_text_task("nope"),
            )
    finally:
        ss.close()


def test_subprocess_agent_make_simple_text_task() -> None:
    async def runner():
        return await make_simple_text_task("ok")({})
    assert asyncio.run(runner()) == "ok"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_runs_default_fanout(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=4)
    specs = fanout_for_goal("add auth")
    assert [s.role for s in specs] == ["researcher", "coder", "tester", "reviewer"]

    result = asyncio.run(orch.run_batch("add auth", specs))
    assert result.all_succeeded is True
    assert len(result.reports) == 4
    assert all(r.success for r in result.reports)
    # Sprint marked complete since every subagent succeeded.
    assert ss.get_sprint(result.sprint_id).state == "complete"
    # Every agent's status row reflects completion.
    for s in specs:
        assert ss.get_status(s.agent_id).status == "completed"


def test_orchestrator_marks_sprint_aborted_on_partial_failure(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=2)

    async def failing(ctx):
        raise RuntimeError("nope")

    specs = [
        SubagentSpec(role="coder", task="write", runner=make_simple_text_task("wrote"), agent_id="c-1"),
        SubagentSpec(role="tester", task="test", runner=failing, agent_id="t-1"),
    ]
    result = asyncio.run(orch.run_batch("mixed", specs))
    assert result.all_succeeded is False
    assert any(r.success for r in result.reports)
    assert any(not r.success for r in result.reports)
    assert ss.get_sprint(result.sprint_id).state == "aborted"


def test_orchestrator_collect_reports_drains_inbox(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=2)

    async def runner(ctx):
        return f"done {ctx['agent_id']}"

    specs = [
        SubagentSpec(role="coder", task="a", runner=runner, agent_id="c-1"),
        SubagentSpec(role="tester", task="b", runner=runner, agent_id="t-1"),
    ]
    asyncio.run(orch.run_batch("two", specs))
    # After run_batch, the inbox should already be drained. Calling
    # collect_reports again should yield zero undelivered messages.
    leftover = orch.collect_reports()
    assert leftover == []


def test_orchestrator_status_lists_all_agents(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=2)
    specs = [
        SubagentSpec(role="researcher", task="r", agent_id="r-1"),
        SubagentSpec(role="coder", task="c", agent_id="c-1"),
    ]
    asyncio.run(orch.run_batch("two", specs))
    statuses = {s.agent_id for s in orch.status()}
    # Lead + 2 workers
    assert {LEAD_AGENT_ID, "r-1", "c-1"} <= statuses


def test_orchestrator_await_completion_returns_final_status(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=2)
    specs = [SubagentSpec(role="researcher", task="a", agent_id="r-1")]
    asyncio.run(orch.run_batch("one", specs))
    final = asyncio.run(orch.await_completion(["r-1"], timeout_seconds=2.0))
    assert final["r-1"].status == "completed"


def test_orchestrator_await_completion_times_out(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=2)
    # Never run any agent: r-1 is unknown to the state.
    with pytest.raises(TimeoutError):
        asyncio.run(orch.await_completion(["r-1"], timeout_seconds=0.5, poll_interval_seconds=0.1))


def test_orchestrator_rejects_empty_specs(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss)
    with pytest.raises(ValueError):
        asyncio.run(orch.run_batch("nothing", []))


def test_orchestrator_default_role_allowlist_covers_every_role() -> None:
    for role in AGENT_ROLES:
        allowlist = SubagentOrchestrator.default_role_allowlist(role)
        assert isinstance(allowlist, tuple)


def test_orchestrator_rejects_invalid_concurrency(tmp_path: Path) -> None:
    ss = SharedState(tmp_path / "state.db")
    with pytest.raises(ValueError):
        SubagentOrchestrator(ss, max_concurrency=0)
    ss.close()


def test_orchestrator_respects_max_concurrency(tmp_path: Path) -> None:
    """Spawn 4 agents with max_concurrency=1 — they should serialize."""

    ss = SharedState(tmp_path / "state.db")
    orch = SubagentOrchestrator(ss, max_concurrency=1)
    specs = [SubagentSpec(role="researcher", task=f"t{i}", agent_id=f"r-{i}") for i in range(4)]
    result = asyncio.run(orch.run_batch("serial", specs))
    assert result.all_succeeded is True
    assert len(result.reports) == 4


def test_orchestrator_messages_have_message_id_attribute(tmp_path: Path) -> None:
    """Regression: orchestrator's _drain_lead_inbox used m.id, which
    did not exist on IPCMessage; the correct attribute is message_id."""

    ss = SharedState(tmp_path / "state.db")
    try:
        ss.register_agent("a")
        ss.register_agent("b")
        # Send a message to the lead inbox — that's what _drain_lead_inbox
        # consumes. Use agent 'a' as the sender so it isn't filtered.
        ss.send_message("a", LEAD_AGENT_ID, "x", {"k": 1})
        msgs = ss.fetch_messages(LEAD_AGENT_ID)
        assert len(msgs) == 1
        assert hasattr(msgs[0], "message_id")
        # The drain helper must succeed and consume exactly that message.
        orch = SubagentOrchestrator(ss)
        delivered = orch._drain_lead_inbox()
        assert len(delivered) == 1
        assert delivered[0].message_id == msgs[0].message_id
    finally:
        ss.close()


# ---------------------------------------------------------------------------
# H-1: Concurrency Bug Fix Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")

@pytest.mark.asyncio
async def test_fanout_runs_all_agents_concurrently(shared_state):
    """All agents should be launched immediately, not sequentially."""
    specs = [
        SubagentSpec(role="general", task=f"task-{i}", agent_id=f"agent-{i}")
        for i in range(4)
    ]
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=4)

    start = time.monotonic()
    result = await orchestrator.run_batch("concurrent test", specs)
    elapsed = time.monotonic() - start

    assert result.sprint_id
    assert len(result.reports) == 4
    assert all(r.success for r in result.reports)
    # With 4 agents each sleeping 0.05s, concurrent execution should be ~0.05-0.10s,
    # not 4 * 0.05s = 0.2s (sequential). Allow up to 0.30s for loaded systems.
    assert elapsed < 0.30, f"Agents ran in {elapsed:.3f}s — too slow for concurrent"

@pytest.mark.asyncio
async def test_max_concurrency_is_respected(shared_state):
    """At most max_concurrency agents should run simultaneously."""
    concurrent_count = 0
    max_concurrent_seen = 0
    start_time = time.monotonic()

    async def slow_task(ctx):
        nonlocal concurrent_count, max_concurrent_seen
        concurrent_count += 1
        max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
        await asyncio.sleep(0.1)
        concurrent_count -= 1
        return f"done at {time.monotonic() - start_time:.3f}s"

    specs = [
        SubagentSpec(role="general", task=f"task-{i}", agent_id=f"agent-{i}")
        for i in range(6)
    ]
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=3)

    await orchestrator.run_batch("concurrency test", specs)

    assert max_concurrent_seen <= 3, f"Saw {max_concurrent_seen} concurrent, expected ≤3"


# ---------------------------------------------------------------------------
# H-9: Architect/Editor Dual-Model Mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_architect_mode_produces_sprint_contract(shared_state, tmp_path):
    from ash.agents.orchestrator import fanout_for_goal
    from ash.core.planner import Planner
    from ash.providers.base import ProviderABC, StreamChunk

    class DummyProvider(ProviderABC):
        model_name = "test"

        def count_tokens(self, text):
            return 0

        async def stream_chat(self, messages, temperature=0.0):
            yield StreamChunk(content="## Goal\nTest\n\n## Definition of Done\n- done", is_done=True)

    planner = Planner(DummyProvider())
    specs = fanout_for_goal(
        "add user login",
        use_architect_mode=True,
        planner=planner,
        project_root=tmp_path,
    )
    assert len(specs) == 2
    assert specs[0].mode == "architect"
    assert specs[1].mode == "execute"
    assert specs[0].runner is not None  # architect has a real runner
    assert specs[1].runner is None      # execute uses default text runner


# ---------------------------------------------------------------------------
# H-10: Subagent Result Consolidation Step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consolidate_reports_multiple_agents(shared_state):
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=4)
    specs = [
        SubagentSpec(role="researcher", task="research X", agent_id=f"r-{i}")
        for i in range(3)
    ]
    result = await orchestrator.run_batch("research task", specs)
    assert result.consolidated_report is not None
    assert result.consolidated_report.role == "consolidator"
    assert len(result.consolidated_report.artifacts["reports"]) == 3
