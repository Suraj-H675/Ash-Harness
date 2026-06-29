import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from core.loop import AshLoop
from core.session import SessionStore
from providers.base import ProviderABC, StreamChunk
from safety.guard import SafetyGuard
from tools.filesystem import WriteFileTool
from ui.terminal import TerminalUI
from ui.turn_input import InteractiveTurnController


class RoutedPrompt:
    def __init__(self) -> None:
        self.steering: asyncio.Queue[str] = asyncio.Queue()
        self.approvals: asyncio.Queue[str] = asyncio.Queue()
        self.prompts: list[str] = []

    async def read(self, prompt: str = "> ") -> str:
        self.prompts.append(prompt)
        if prompt.startswith("Approve"):
            return await self.approvals.get()
        return await self.steering.get()


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
    assert statuses == ["Steering queued (1 pending)."]
    assert any(
        message["role"] == "user" and message["content"] == "change direction"
        for message in provider.messages[1]
    )


@pytest.mark.asyncio
async def test_interactive_controller_cancels_running_turn(tmp_path: Path) -> None:
    provider = BlockingProvider()
    prompt = RoutedPrompt()
    statuses: list[str] = []
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
    )

    turn = asyncio.create_task(controller.run("start"))
    await provider.started.wait()
    await prompt.steering.put("/cancel")

    assert await turn is None
    assert statuses == ["Turn cancelled."]
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
    )

    response = await controller.run("write the file")

    assert response == "done"
    assert (tmp_path / "approved.txt").read_text() == "written"
    assert any(item.startswith("Approve") for item in prompt.prompts)
    assert statuses == []
