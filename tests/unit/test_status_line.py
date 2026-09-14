from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from ash.config import AshConfig
from ash.core.session import SessionStore
from ash.safety.policy import PermissionPolicy
from ash.ui.status import StatusLine, git_branch


def test_status_line_includes_runtime_git_cost_and_sandbox(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_session_token_stats(
        session.session_id,
        10,
        5,
        0.0123,
        cache_read_tokens=7,
        cache_write_tokens=2,
        estimated_prompt_tokens=3,
        estimated_cost_usd=0.004,
    )
    loop = SimpleNamespace(
        current_session=session,
        session_store=store,
        permission_policy=PermissionPolicy("interactive"),
        project_root=tmp_path,
        _last_context_tokens=123,
    )
    config = AshConfig(
        workspace_root=tmp_path,
        max_context_tokens=1000,
        max_completion_tokens=100,
    )
    sandbox = SimpleNamespace(backend_name="scoped", is_fully_isolated=lambda: False)

    rendered = StatusLine(loop, config, sandbox, refresh_seconds=60)()

    assert "ctx ~123/900" in rendered
    assert "cache:7r/2w" in rendered
    assert "~$0.0123" in rendered
    assert "sb:scoped" in rendered
    assert "sb:scoped!" in rendered
    assert f"s:{session.session_id[:8]}" in rendered


def test_status_line_sanitizes_persisted_model_controls(tmp_path: Path) -> None:
    loop = SimpleNamespace(
        current_session=None,
        session_store=SimpleNamespace(),
        permission_policy=PermissionPolicy("interactive"),
        project_root=tmp_path,
        _last_context_tokens=0,
    )
    config = AshConfig(
        workspace_root=tmp_path,
        model="openrouter/model\x1b[2J\u202ehidden\u202c",
    )
    sandbox = SimpleNamespace(backend_name="scoped", is_fully_isolated=lambda: False)

    rendered = StatusLine(loop, config, sandbox, refresh_seconds=60)()

    assert "openrouter/model\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered


def test_git_branch_reports_branch_and_handles_non_repository(
    tmp_path: Path,
) -> None:
    assert git_branch(tmp_path) == "none"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    assert git_branch(tmp_path) in {"main", "master"}
