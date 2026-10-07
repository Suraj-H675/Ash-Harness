from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ash.config import AshConfig
from ash.ui.status import StatusLine


def _loop(root: Path, *, model: str, reasoning: bool, context: int = 0):
    return SimpleNamespace(
        project_root=root,
        active_model_id=model,
        provider=SimpleNamespace(
            capabilities=SimpleNamespace(reasoning=reasoning)
        ),
        _last_context_tokens=context,
    )


def test_status_line_shows_model_reasoning_and_directory() -> None:
    root = Path.home() / "projects" / "Ash-Harness"
    config = AshConfig(
        workspace_root=root,
        model="openai/gpt-test",
    )
    status = StatusLine(
        _loop(root, model="openai/gpt-test", reasoning=True),
        config,
    )

    assert status.left() == "gpt-test  ·  reasoning  ·  ~/projects/Ash-Harness"


def test_status_line_reports_no_reasoning_when_active_model_lacks_it() -> None:
    root = Path.home() / "project"
    config = AshConfig(workspace_root=root, model="local/plain")
    status = StatusLine(
        _loop(root, model="local/plain", reasoning=False),
        config,
    )

    assert "no reasoning" in status.left()


def test_status_line_tracks_active_runtime_model_without_io() -> None:
    root = Path.home() / "project"
    loop = _loop(root, model="openai/primary", reasoning=True)
    status = StatusLine(loop, AshConfig(workspace_root=root, model="openai/primary"))

    assert status.left().startswith("primary  ·")
    loop.active_model_id = "groq/fallback"
    loop.provider.capabilities.reasoning = False

    assert status.left().startswith("fallback  ·  no reasoning  ·")


def test_status_line_only_adds_safety_state_when_non_default(tmp_path: Path) -> None:
    loop = _loop(tmp_path, model="provider/model", reasoning=True)
    loop.permission_policy = SimpleNamespace(
        mode=SimpleNamespace(value="auto_edit")
    )
    sandbox = SimpleNamespace(is_fully_isolated=lambda: False)

    rendered = StatusLine(
        loop,
        AshConfig(workspace_root=tmp_path, model="provider/model"),
        sandbox,
    ).left()

    assert "auto_edit" in rendered
    assert "⚠ limited isolation" in rendered


def test_status_line_context_usage_uses_prompt_context_ceiling(tmp_path: Path) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        max_context_tokens=2_000,
        max_completion_tokens=500,
    )
    status = StatusLine(
        _loop(tmp_path, model="openai/test", reasoning=True, context=375),
        config,
    )

    assert status.context_usage() == (375, 1_500)


def test_status_line_context_usage_prefers_negotiated_runtime_ceiling(
    tmp_path: Path,
) -> None:
    config = AshConfig(
        workspace_root=tmp_path,
        max_context_tokens=2_000,
        max_completion_tokens=500,
    )
    loop = _loop(
        tmp_path,
        model="provider/smaller",
        reasoning=True,
        context=225,
    )
    loop._last_context_maximum = 900

    assert StatusLine(loop, config).context_usage() == (225, 900)


def test_status_line_sanitizes_model_and_directory_controls(tmp_path: Path) -> None:
    root = tmp_path / "safe\u202ehidden\u202c"
    loop = _loop(
        root,
        model="provider/model\x1b[2J\u202ehidden\u202c",
        reasoning=True,
    )
    status = StatusLine(loop, AshConfig(workspace_root=root, model="provider/model"))

    rendered = status.left()
    assert "model\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "safe\\u202ehidden\\u202c" in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
