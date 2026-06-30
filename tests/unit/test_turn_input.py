import asyncio
import io
from pathlib import Path

import pytest
from types import SimpleNamespace
from rich.console import Console

from core.loop import AshLoop
from core.session import SessionStore
from providers.base import ProviderABC, StreamChunk
from safety.grants import RuleEffect, load_permission_rules
from safety.guard import SafetyGuard
from safety.policy import PolicyAction
from tools.filesystem import WriteFileTool
from ui.terminal import TerminalUI
from ui.notifications import NotificationEvent
from ui.turn_input import InteractiveTurnController


class RoutedPrompt:
    def __init__(self) -> None:
        self.steering: asyncio.Queue[str] = asyncio.Queue()
        self.approvals: asyncio.Queue[str] = asyncio.Queue()
        self.prompts: list[str] = []

    async def read(self, prompt: str = "> ") -> str:
        self.prompts.append(prompt)
        if prompt.startswith(("Approve", "Plan")):
            return await self.approvals.get()
        return await self.steering.get()


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[NotificationEvent, str]] = []

    def notify(self, event: str | NotificationEvent, message: str) -> bool:
        self.calls.append((NotificationEvent(event), message))
        return True


class BlockingProvider(ProviderABC):
    model_name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.messages = []

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        self.messages.append(list(messages))
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            yield StreamChunk(content="initial", is_done=True)
        else:
            yield StreamChunk(content="redirected", is_done=True)


class WriteProvider(ProviderABC):
    model_name = "writer"

    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                content=(
                    '<call_tool name="write_file">'
                    '<arg name="file_path">approved.txt</arg>'
                    '<arg name="content">written</arg>'
                    "</call_tool>"
                ),
                is_done=True,
            )
        else:
            yield StreamChunk(content="done", is_done=True)


def make_ui() -> TerminalUI:
    return TerminalUI(
        safety_tier="interactive",
        console=Console(file=io.StringIO(), force_terminal=False),
    )


@pytest.mark.asyncio
async def test_interactive_controller_queues_steering_during_stream(
    tmp_path: Path,
) -> None:
    provider = BlockingProvider()
    prompt = RoutedPrompt()
    statuses: list[str] = []
    notifier = RecordingNotifier()
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider,
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
        notifier=notifier,
        notification_include_preview=True,
    )

    turn = asyncio.create_task(controller.run("start"))
    await provider.started.wait()
    await prompt.steering.put("change direction")
    for _ in range(20):
        if loop.pending_steering_count:
            break
        await asyncio.sleep(0)
    provider.release.set()

    assert await turn == "redirected"
    assert ui.transcript.snapshot()[0].kind == "user"
    assert ui.transcript.snapshot()[0].content == "start"
    assert statuses == ["Steering queued (1 pending)."]
    assert notifier.calls == [
        (NotificationEvent.TURN_COMPLETE, "Ash finished: redirected")
    ]
    assert any(
        message["role"] == "user" and message["content"] == "change direction"
        for message in provider.messages[1]
    )


@pytest.mark.asyncio
async def test_interactive_controller_cancels_running_turn(tmp_path: Path) -> None:
    provider = BlockingProvider()
    prompt = RoutedPrompt()
    statuses: list[str] = []
    notifier = RecordingNotifier()
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider,
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
        notifier=notifier,
    )

    turn = asyncio.create_task(controller.run("start"))
    await provider.started.wait()
    await prompt.steering.put("/cancel")

    assert await turn is None
    assert statuses == ["Turn cancelled."]
    assert notifier.calls == []
    assert loop.is_turn_running is False
    assert (
        loop.session_store.reconcile_interrupted_turns(loop.current_session.session_id)
        == 0
    )


@pytest.mark.asyncio
async def test_interactive_approval_preempts_steering_reader(tmp_path: Path) -> None:
    prompt = RoutedPrompt()
    await prompt.approvals.put("y")
    statuses: list[str] = []
    notifier = RecordingNotifier()
    ui = make_ui()
    guard = SafetyGuard(tmp_path)
    tool = WriteFileTool(guard)
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        WriteProvider(),
        guard,
        ui,
        tmp_path,
        tools={tool.name: tool},
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
        notifier=notifier,
    )

    response = await controller.run("write the file")

    assert response == "done"
    assert (tmp_path / "approved.txt").read_text() == "written"
    assert any(item.startswith("Approve") for item in prompt.prompts)
    assert statuses == []
    assert notifier.calls == [
        (NotificationEvent.APPROVAL_REQUIRED, "Ash needs approval: write_file"),
        (NotificationEvent.TURN_COMPLETE, "Ash turn complete."),
    ]


@pytest.mark.asyncio
async def test_plan_approval_uses_shared_prompt_owner(tmp_path: Path) -> None:
    prompt = RoutedPrompt()
    await prompt.approvals.put("y")
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    notifier = RecordingNotifier()
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        notifier=notifier,
    )
    execution = SimpleNamespace(
        contract=SimpleNamespace(
            contract_id="12345678-plan",
            goal="ship safely",
            definition_of_done=["tests pass"],
            files_in_scope=["ui/"],
        ),
        items=[],
    )

    assert await controller._request_plan_approval(execution) is True
    assert prompt.prompts == ["Plan [y/e/N]? "]
    assert ui.transcript.snapshot()[-1].metadata == {"type": "plan.approval"}
    assert notifier.calls == [
        (NotificationEvent.APPROVAL_REQUIRED, "Ash needs plan approval.")
    ]


@pytest.mark.asyncio
async def test_approval_can_persist_exact_project_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = RoutedPrompt()
    await prompt.approvals.put("p")
    statuses: list[str] = []
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
    )

    approved = await controller._request_approval(
        "write_file",
        {"file_path": "docs/result.md", "content": "first"},
    )

    assert approved is True
    rules = load_permission_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].effect == RuleEffect.ALLOW
    assert [matcher.argument for matcher in rules[0].matchers] == ["file_path"]
    assert (
        loop.permission_policy.evaluate(
            "write_file",
            {"file_path": "docs/result.md", "content": "changed"},
        ).action
        == PolicyAction.ALLOW
    )
    assert (
        loop.permission_policy.evaluate(
            "write_file",
            {"file_path": "src/result.py", "content": "changed"},
        ).action
        == PolicyAction.ASK
    )
    assert statuses == [f"Saved allow rule {rules[0].rule_id} for write_file."]


@pytest.mark.asyncio
async def test_approval_can_allow_exact_session_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = RoutedPrompt()
    await prompt.approvals.put("s")
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(loop, prompt, ui)  # type: ignore[arg-type]
    arguments = {"file_path": "notes.txt", "content": "one"}

    assert await controller._request_approval("write_file", arguments) is True
    assert load_permission_rules(tmp_path) == []
    assert (
        loop.permission_policy.evaluate(
            "write_file", {"file_path": "notes.txt", "content": "two"}
        ).action
        == PolicyAction.ALLOW
    )
    assert (
        loop.permission_policy.evaluate(
            "write_file", {"file_path": "other.txt", "content": "two"}
        ).action
        == PolicyAction.ASK
    )


@pytest.mark.asyncio
async def test_approval_can_persist_verified_command_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = RoutedPrompt()
    await prompt.approvals.put("c")
    await prompt.approvals.put("pytest")
    statuses: list[str] = []
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
    )

    assert (
        await controller._request_approval(
            "run_command",
            {"command_line": "pytest tests/unit -q"},
        )
        is True
    )
    assert (
        loop.permission_policy.evaluate(
            "run_command", {"command_line": "pytest tests/integration -q"}
        ).action
        == PolicyAction.ALLOW
    )
    assert (
        loop.permission_policy.evaluate(
            "run_command", {"command_line": "pytest -q && echo unsafe"}
        ).action
        == PolicyAction.ASK
    )
    assert prompt.prompts[-1].startswith("Approve command prefix")
    assert statuses[0].startswith("Saved allow rule ")


@pytest.mark.asyncio
async def test_approval_can_persist_scoped_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = RoutedPrompt()
    await prompt.approvals.put("x")
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(loop, prompt, ui)  # type: ignore[arg-type]
    arguments = {"file_path": ".env", "content": "secret"}

    assert await controller._request_approval("write_file", arguments) is False
    decision = loop.permission_policy.evaluate("write_file", arguments)
    assert decision.action == PolicyAction.DENY
    assert decision.rule_id == load_permission_rules(tmp_path)[0].rule_id


@pytest.mark.asyncio
async def test_ambiguous_command_prefix_approval_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prompt = RoutedPrompt()
    await prompt.approvals.put("c")
    await prompt.approvals.put("pytest")
    statuses: list[str] = []
    ui = make_ui()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        BlockingProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )
    controller = InteractiveTurnController(
        loop,
        prompt,  # type: ignore[arg-type]
        ui,
        write_status=statuses.append,
    )

    approved = await controller._request_approval(
        "run_command",
        {"command_line": "pytest -q && echo unsafe"},
    )

    assert approved is False
    assert load_permission_rules(tmp_path) == []
    assert statuses and statuses[0].startswith("Permission scope rejected:")
