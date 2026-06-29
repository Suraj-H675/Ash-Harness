from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from config import AshConfig
from core.session import SessionStore
from safety.policy import PermissionPolicy
from ui.status import StatusLine, git_branch


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
    sandbox = SimpleNamespace(backend_name="scoped")

    rendered = StatusLine(loop, config, sandbox, refresh_seconds=60)()

    assert "ctx ~123/900" in rendered
    assert "cache:7r/2w" in rendered
    assert "$0.0123" in rendered
    assert "sb:scoped" in rendered
    assert f"s:{session.session_id[:8]}" in rendered


def test_git_branch_reports_branch_and_handles_non_repository(
    tmp_path: Path,
) -> None:
    assert git_branch(tmp_path) == "none"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    assert git_branch(tmp_path) in {"main", "master"}
