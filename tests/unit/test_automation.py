from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import signal
import sqlite3
import subprocess
import stat
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ash.automation.schedules import (
    build_schedule,
    first_fire_time,
    next_fire_time,
    parse_duration,
)
from ash.automation.models import AutomationJob, AutomationWorkerSummary, ScheduleSpec
from ash.automation.delivery import (
    PreparedWebhook,
    WebhookDeliveryError,
    post_webhook,
    prepare_webhook,
)
from ash.automation.runner import (
    MAX_AUTOMATION_PROMPT_BYTES,
    _execute as execute_automation_subprocess_request,
)
from ash.automation.store import (
    AUTOMATION_SCHEMA_VERSION,
    AutomationError,
    AutomationRestartRequired,
    AutomationStore,
)
from ash.automation.worker import AutomationWorkerService
from ash.commands.automation import automation_config_loader
from ash.config import AshConfig
from ash.core.session import SessionStore, get_db_connection
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.sdk import AshResult
from ash.tools.automation import ListAutomationsTool, ManageAutomationTool


def test_schedule_validation_and_named_timezone() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert parse_duration("1h30m") == 5400
    interval = build_schedule(every="30m", now=now)
    assert first_fire_time(interval, now=now) == now + timedelta(minutes=30)

    cron = build_schedule(
        cron="30 9 * * mon-fri",
        timezone_name="Asia/Kolkata",
        now=now,
    )
    assert first_fire_time(cron, now=now) == datetime(
        2026, 1, 1, 4, 0, tzinfo=timezone.utc
    )

    with pytest.raises(ValueError, match="exactly one"):
        build_schedule(every="1h", cron="0 * * * *", now=now)
    with pytest.raises(ValueError, match="day-of-week"):
        build_schedule(cron="0 9 * * 1", now=now)
    with pytest.raises(ValueError, match="future"):
        build_schedule(at=now.isoformat(), now=now)


def test_automation_job_preserves_legacy_constructor_contract() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    job = AutomationJob(
        "job-id",
        "nightly",
        "Review project health",
        "/workspace",
        ScheduleSpec("every", "1h"),
        True,
        None,
        86_400,
        1800.0,
        100_000,
        now,
        now,
    )

    assert job.created_at == now
    assert job.updated_at == now
    assert job.webhook_url is None
    assert job.webhook_secret_env is None


def test_automation_store_rejects_linked_database_file_and_parent(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    linked_file = tmp_path / "automations.db"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-db"
    try:
        linked_file.symlink_to(target)
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    for database in (linked_file, linked_parent / "automations.db"):
        with pytest.raises(AutomationError, match="symlink or junction"):
            AutomationStore(database)

    assert not (outside / "automations.db").exists()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='automation_jobs'"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_subprocess_runner_rechecks_workspace_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = AshConfig(workspace_root=workspace)
    monkeypatch.setattr("ash.automation.runner.is_workspace_trusted", lambda path: False)

    with pytest.raises(ValueError, match="not trusted"):
        await execute_automation_subprocess_request(
            {
                "config": config.model_dump(mode="json"),
                "workspace": str(workspace),
                "prompt": "Do not run",
                "user_metadata": None,
            }
        )


@pytest.mark.asyncio
async def test_subprocess_runner_rejects_oversized_prompt_before_client_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = AshConfig(workspace_root=workspace)
    monkeypatch.setattr("ash.automation.runner.is_workspace_trusted", lambda path: True)

    with pytest.raises(ValueError, match="prompt exceeds"):
        await execute_automation_subprocess_request(
            {
                "config": config.model_dump(mode="json"),
                "workspace": str(workspace),
                "prompt": "x" * (MAX_AUTOMATION_PROMPT_BYTES + 1),
                "user_metadata": None,
            }
        )


@pytest.mark.asyncio
async def test_subprocess_runner_resolves_relative_workspace_from_child_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("ash.automation.runner.is_workspace_trusted", lambda path: True)
    config = AshConfig(workspace_root=Path("."))

    with pytest.raises(ValueError, match="prompt exceeds"):
        await execute_automation_subprocess_request(
            {
                "config": config.model_dump(mode="json"),
                "workspace": ".",
                "prompt": "x" * (MAX_AUTOMATION_PROMPT_BYTES + 1),
                "user_metadata": None,
            }
        )


def test_automation_runner_rejects_oversized_protocol_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.automation.runner import MAX_AUTOMATION_REQUEST_BYTES, main

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO("x" * (MAX_AUTOMATION_REQUEST_BYTES + 1)),
    )

    assert main() == 1
    assert "automation request exceeds" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_automation_subprocess_runner_uses_isolated_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "repo"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    async def fake_communicate(*args, **kwargs):
        del args, kwargs
        return (
            b'ASH_AUTOMATION_RESULT={"ok":true,"result":{"response":"ok",'
            b'"session_id":"session","model":"fake/model","context_tokens":1}}\n',
            b"",
        )

    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", fake_spawn
    )
    monkeypatch.setattr("ash.automation.worker.communicate_process", fake_communicate)
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )

    result = await client.prompt("run")

    assert result.response == "ok"
    assert captured["args"][-4:] == (
        sys.executable,
        "-I",
        "-m",
        "ash.automation.runner",
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifeline descriptor regression")
@pytest.mark.asyncio
async def test_automation_subprocess_closes_lifeline_fds_when_spawn_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.automation.worker as worker_module
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "repo"
    workspace.mkdir()
    real_pipe = worker_module.os.pipe
    created_fds: list[int] = []

    def recording_pipe() -> tuple[int, int]:
        read_fd, write_fd = real_pipe()
        created_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    monkeypatch.setattr(worker_module.os, "pipe", recording_pipe)
    monkeypatch.setattr(
        worker_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("spawn failed")),
    )
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )

    with pytest.raises(OSError, match="spawn failed"):
        await client.prompt("run")

    assert len(created_fds) == 2
    for descriptor in created_fds:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifeline descriptor regression")
@pytest.mark.parametrize("closed_descriptor", [0, 1, 2])
def test_automation_lifeline_descriptors_never_reuse_standard_streams(
    tmp_path: Path,
    closed_descriptor: int,
) -> None:
    result_path = tmp_path / f"lifeline-fds-{closed_descriptor}.txt"
    probe = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from ash.automation.worker import _create_parent_lifeline_pipe\n"
        "closed=int(sys.argv[1])\n"
        "target=Path(sys.argv[2])\n"
        "os.close(closed)\n"
        "read_fd=write_fd=-1\n"
        "try:\n"
        " read_fd,write_fd=_create_parent_lifeline_pipe()\n"
        " target.write_text(f'{read_fd},{write_fd}', encoding='utf-8')\n"
        "finally:\n"
        " for descriptor in (read_fd,write_fd):\n"
        "  if descriptor >= 0:\n"
        "   try: os.close(descriptor)\n"
        "   except OSError: pass\n"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(closed_descriptor),
            str(result_path),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    read_fd, write_fd = (
        int(value) for value in result_path.read_text(encoding="utf-8").split(",")
    )
    assert read_fd >= 3
    assert write_fd >= 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX isolated runner regression")
@pytest.mark.asyncio
async def test_automation_subprocess_fails_closed_when_worker_inherits_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.automation.worker as worker_module
    from ash.automation.worker import _SubprocessAutomationClient
    from ash.sandbox.process_utils import INHERIT_PROCESS_GROUP_ENV

    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv(INHERIT_PROCESS_GROUP_ENV, "1")
    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(worker_module.asyncio, "create_subprocess_exec", create)
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )

    with pytest.raises(AutomationError, match="isolated POSIX process group"):
        await client.prompt("run")

    create.assert_not_awaited()


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent lifeline regression")
def test_automation_runner_rejects_invalid_parent_lifeline_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.runner import _PARENT_LIFELINE_FD_ENV, _arm_parent_lifeline

    monkeypatch.setenv(_PARENT_LIFELINE_FD_ENV, "not-a-descriptor")

    with pytest.raises(RuntimeError, match="descriptor is invalid"):
        _arm_parent_lifeline()


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent lifeline regression")
def test_automation_runner_rejects_unavailable_parent_lifeline_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.runner import _PARENT_LIFELINE_FD_ENV, _arm_parent_lifeline

    monkeypatch.setenv(_PARENT_LIFELINE_FD_ENV, "2147483647")

    with pytest.raises(RuntimeError, match="descriptor is unavailable"):
        _arm_parent_lifeline()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group lifeline regression")
def test_automation_subprocess_dies_with_abrupt_worker_parent(
    tmp_path: Path,
) -> None:
    def process_is_running(pid: int) -> bool:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        state = completed.stdout.strip()
        return completed.returncode == 0 and bool(state) and not state.startswith("Z")

    workspace = tmp_path / "repo"
    workspace.mkdir()
    wrapper = tmp_path / "automation-runner-wrapper"
    runner_pid_path = tmp_path / "runner.pid"
    child_pid_path = tmp_path / "child.pid"
    heartbeat_path = tmp_path / "heartbeat.txt"
    helper = tmp_path / "worker-helper.py"

    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "from ash.automation.runner import _arm_parent_lifeline\n"
        "_arm_parent_lifeline()\n"
        "root = Path(os.environ['ASH_AUTOMATION_LIFELINE_TEST_DIR'])\n"
        "heartbeat = root / 'heartbeat.txt'\n"
        "child_code = (\n"
        "    'import sys,time\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'path=Path(sys.argv[1])\\n'\n"
        "    'counter=0\\n'\n"
        "    'while True:\\n'\n"
        "    ' counter += 1\\n'\n"
        "    ' path.write_text(str(counter), encoding=\\\"utf-8\\\")\\n'\n"
        "    ' time.sleep(0.05)\\n'\n"
        ")\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code, str(heartbeat)])\n"
        "(root / 'runner.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "(root / 'child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    helper.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "import ash.automation.worker as worker_module\n"
        "from ash.automation.worker import _SubprocessAutomationClient\n"
        "from ash.config import AshConfig\n"
        "wrapper = Path(__import__('sys').argv[1])\n"
        "workspace = Path(__import__('sys').argv[2])\n"
        "worker_module.sys = SimpleNamespace(executable=str(wrapper))\n"
        "client = _SubprocessAutomationClient(AshConfig(workspace_root=workspace), workspace)\n"
        "asyncio.run(client.prompt('run'))\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["ASH_AUTOMATION_LIFELINE_TEST_DIR"] = str(tmp_path)
    worker = subprocess.Popen(
        [sys.executable, str(helper), str(wrapper), str(workspace)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    runner_pid = child_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if runner_pid_path.exists() and child_pid_path.exists() and heartbeat_path.exists():
                runner_pid = int(runner_pid_path.read_text(encoding="utf-8"))
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                break
            if worker.poll() is not None:
                stderr = worker.stderr.read() if worker.stderr is not None else ""
                pytest.fail(f"automation helper exited before launch: {stderr}")
            time.sleep(0.01)
        else:
            pytest.fail("automation runner and descendant did not start")

        assert process_is_running(runner_pid)
        assert process_is_running(child_pid)
        before = int(heartbeat_path.read_text(encoding="utf-8"))
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not process_is_running(runner_pid) and not process_is_running(child_pid):
                break
            time.sleep(0.05)
        assert not process_is_running(runner_pid)
        assert not process_is_running(child_pid)
        after = int(heartbeat_path.read_text(encoding="utf-8"))
        time.sleep(0.15)
        assert int(heartbeat_path.read_text(encoding="utf-8")) == after
        assert after >= before
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if runner_pid is not None:
            try:
                os.killpg(runner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="signed webhook child secret-isolation success path is Linux-only",
)
@pytest.mark.asyncio
async def test_automation_subprocess_isolates_only_webhook_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import (
        _SubprocessAutomationClient,
        _linux_process_dumpable,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

    async def fake_spawn(*args, **kwargs):
        del args
        captured["env"] = kwargs["env"]
        if sys.platform.startswith("linux"):
            captured["dumpable_during_spawn"] = _linux_process_dumpable()
        return Process()

    async def fake_communicate(*args, **kwargs):
        del args, kwargs
        return (
            b'ASH_AUTOMATION_RESULT={"ok":true,"result":{"response":"ok",'
            b'"session_id":"session","model":"fake/model","context_tokens":1}}\n',
            b"",
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-secret")
    monkeypatch.setenv("ASH_AUTOMATION_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("ASH_TOOL_TOKEN", "tool-token")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "search-secret")
    monkeypatch.setenv("ASH_A2A_TOKEN", "a2a-secret")
    monkeypatch.setenv("MCP_TOKEN", "mcp-secret")
    monkeypatch.setenv("UNRELATED_PARENT_SECRET", "unrelated-secret")
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", fake_spawn
    )
    monkeypatch.setattr("ash.automation.worker.communicate_process", fake_communicate)
    client = _SubprocessAutomationClient(
        AshConfig(
            workspace_root=workspace,
            model="openrouter/vendor/agent",
            command_env_allowlist=["ASH_TOOL_TOKEN"],
        ),
        workspace,
        blocked_environment_names=frozenset({"ASH_AUTOMATION_WEBHOOK_SECRET"}),
    )

    dumpable_before = (
        _linux_process_dumpable() if sys.platform.startswith("linux") else None
    )
    result = await client.prompt("run")

    assert result.response == "ok"
    if sys.platform.startswith("linux"):
        assert captured["dumpable_during_spawn"] == 0
        assert _linux_process_dumpable() == dumpable_before
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["OPENROUTER_API_KEY"] == "provider-secret"
    assert environment["ASH_TOOL_TOKEN"] == "tool-token"
    assert environment["BRAVE_SEARCH_API_KEY"] == "search-secret"
    assert environment["ASH_A2A_TOKEN"] == "a2a-secret"
    assert environment["MCP_TOKEN"] == "mcp-secret"
    assert environment["UNRELATED_PARENT_SECRET"] == "unrelated-secret"
    assert "ASH_AUTOMATION_WEBHOOK_SECRET" not in environment


@pytest.mark.asyncio
async def test_automation_subprocess_rejects_webhook_secret_provider_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-secret")
    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", create
    )
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace, model="openrouter/vendor/agent"),
        workspace,
        blocked_environment_names=frozenset({"OPENROUTER_API_KEY"}),
    )

    with pytest.raises(AutomationError, match="required by the automation subprocess"):
        await client.prompt("run")

    create.assert_not_awaited()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux parent-environment protection regression",
)
@pytest.mark.asyncio
async def test_automation_subprocess_fails_closed_when_parent_env_cannot_be_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "repo"
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(
        "ash.automation.worker._linux_process_dumpable",
        lambda **kwargs: (_ for _ in ()).throw(OSError("prctl unavailable")),
    )
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", create
    )
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
        blocked_environment_names=frozenset({"ASH_AUTOMATION_WEBHOOK_SECRET"}),
    )

    with pytest.raises(AutomationError, match="could not protect the worker environment"):
        await client.prompt("run")

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_automation_subprocess_rejects_operational_webhook_secret_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "repo"
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", create
    )
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
        blocked_environment_names=frozenset({"HOME"}),
    )

    with pytest.raises(AutomationError, match="required by the automation subprocess"):
        await client.prompt("run")

    create.assert_not_awaited()


def test_signed_automation_webhook_fails_closed_without_parent_env_isolation() -> None:
    from ash.automation.worker import _protect_worker_parent_environment

    with pytest.raises(AutomationError, match="currently supported only on Linux"):
        with _protect_worker_parent_environment(True, platform_name="darwin"):
            pytest.fail("unsupported platform must not enter protected scope")


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux /proc environment privacy regression",
)
def test_automation_parent_environment_is_hidden_from_child_proc_reads() -> None:
    from ash.automation.worker import _protect_worker_parent_environment

    parent_environ = f"/proc/{os.getpid()}/environ"
    probe = (
        "import pathlib,sys; "
        "path=pathlib.Path(sys.argv[1]); "
        "\ntry: path.open('rb').read(1)"
        "\nexcept PermissionError: raise SystemExit(13)"
        "\nexcept OSError: raise SystemExit(12)"
        "\nraise SystemExit(0)"
    )

    def probe_parent() -> int:
        return subprocess.run(
            [sys.executable, "-c", probe, parent_environ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode

    if probe_parent() != 0:
        pytest.skip("parent /proc environment is already unreadable")
    with _protect_worker_parent_environment(True):
        assert probe_parent() == 13
    assert probe_parent() == 0


def test_automation_environment_name_matching_is_case_insensitive_on_windows() -> None:
    from ash.automation.worker import _environment_names_overlap

    assert _environment_names_overlap(
        {"openai_api_key"}, {"OPENAI_API_KEY"}, platform_name="nt"
    )
    assert not _environment_names_overlap(
        {"openai_api_key"}, {"OPENAI_API_KEY"}, platform_name="posix"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_automation_subprocess_refuses_workspace_replaced_before_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient

    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )
    workspace.rename(saved)
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec",
        create,
    )

    with pytest.raises(AutomationError, match="working directory identity changed"):
        await client.prompt("run")

    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_automation_subprocess_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.automation.worker as worker_module
    from ash.automation.worker import _SubprocessAutomationClient
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    cwd_log = tmp_path / "automation-cwd.txt"
    wrapper = tmp_path / "automation-python"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "request=json.loads(sys.stdin.read())\n"
        f"Path({str(cwd_log)!r}).write_text(json.dumps({{'cwd': os.getcwd(), "
        "'workspace': request['workspace'], 'config_workspace': "
        "request['config']['workspace_root']}), encoding='utf-8')\n"
        "payload={'ok': True, 'result': {'response': 'ok', 'session_id': 'session', "
        "'model': 'fake/model', 'context_tokens': 1}}\n"
        "print('ASH_AUTOMATION_RESULT=' + json.dumps(payload), flush=True)\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(worker_module, "sys", SimpleNamespace(executable=str(wrapper)))
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.automation.worker.prepare_process_tree", prepare_then_swap)
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )

    result = await client.prompt("run")

    assert result.response == "ok"
    assert swapped is True
    launch = json.loads(cwd_log.read_text(encoding="utf-8"))
    assert Path(launch["cwd"]).resolve() == saved.resolve()
    assert launch["workspace"] == "."
    assert launch["config_workspace"] == "."


def test_automation_worker_requires_restart_after_workspace_identity_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    store = AutomationStore(tmp_path / "automation.db")
    worker = AutomationWorkerService(store, workspace)
    workspace.rename(saved)
    workspace.mkdir()

    with pytest.raises(
        AutomationRestartRequired,
        match="workspace identity changed",
    ):
        worker._validate_workspace()


def test_automation_worker_requires_restart_after_database_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "automation.db"
    replacement = tmp_path / "replacement.db"
    store = AutomationStore(database)
    with AutomationStore(replacement):
        pass
    worker = AutomationWorkerService(store, workspace)
    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)

    try:
        database.rename(tmp_path / "automation-original.db")
        replacement.rename(database)
    except OSError as exc:
        store.close()
        pytest.skip(f"open database replacement is unavailable: {exc}")

    with pytest.raises(
        AutomationRestartRequired,
        match="automation database identity changed",
    ):
        worker._validate_workspace()

    store.close()


def test_automation_worker_requires_restart_when_database_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "automation.db"
    store = AutomationStore(database)
    worker = AutomationWorkerService(store, workspace)
    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)

    try:
        database.rename(tmp_path / "automation-original.db")
    except OSError as exc:
        store.close()
        pytest.skip(f"open database rename is unavailable: {exc}")

    with pytest.raises(
        AutomationRestartRequired,
        match="automation database identity changed",
    ):
        worker._validate_workspace()

    store.close()


def test_automation_worker_requires_restart_when_database_identity_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutomationStore(tmp_path / "automation.db")
    worker = AutomationWorkerService(store, workspace)
    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr("ash.automation.worker._regular_file_identity", lambda path: None)

    with pytest.raises(
        AutomationRestartRequired,
        match="automation database identity changed",
    ):
        worker._validate_workspace()

    store.close()


def test_automation_worker_refuses_start_without_database_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutomationStore(tmp_path / "automation.db")
    monkeypatch.setattr("ash.automation.worker._regular_file_identity", lambda path: None)

    with pytest.raises(
        AutomationRestartRequired,
        match="automation database identity is unavailable",
    ):
        AutomationWorkerService(store, workspace)

    store.close()


@pytest.mark.asyncio
async def test_automation_subprocess_fails_closed_without_stable_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.automation.worker import _SubprocessAutomationClient
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    create = AsyncMock(side_effect=AssertionError("automation must not launch"))
    monkeypatch.setattr(
        "ash.automation.worker.prepare_scoped_process_launch", unavailable
    )
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_subprocess_exec", create
    )
    client = _SubprocessAutomationClient(
        AshConfig(workspace_root=workspace),
        workspace,
    )

    with pytest.raises(AutomationError, match="stable cwd unavailable"):
        await client.prompt("run")

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_automation_maintenance_prunes_through_open_store_after_file_set_swap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    old = 1_700_000_000.0
    clock = [old]
    now = datetime.fromtimestamp(old, tz=timezone.utc)

    store = AutomationStore(state / "automation.db", clock=lambda: clock[0])
    try:
        original_job = store.create_job(
            name="original",
            prompt="noop",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
            enabled=False,
        )
        original_claim = store.claim_manual(
            original_job.job_id,
            workspace=workspace,
            worker_id="original",
        )
        store.finish_run(
            original_claim.run.run_id,
            original_claim.token,
            status="succeeded",
        )

        with AutomationStore(
            state / "replacement.db", clock=lambda: clock[0]
        ) as replacement_store:
            replacement_job = replacement_store.create_job(
                name="replacement",
                prompt="noop",
                workspace=workspace,
                schedule=build_schedule(every="1h", now=now),
                enabled=False,
            )
            replacement_claim = replacement_store.claim_manual(
                replacement_job.job_id,
                workspace=workspace,
                worker_id="replacement",
            )
            replacement_store.finish_run(
                replacement_claim.run.run_id,
                replacement_claim.token,
                status="succeeded",
            )
            replacement_run_id = replacement_claim.run.run_id

        worker = AutomationWorkerService(
            store,
            workspace,
            run_retention_days=1,
            maintenance_interval_seconds=1,
        )
        clock[0] += 3 * 86400

        original_map = {
            "automation.db": "original-automation.db",
            "automation.db-wal": "original-automation.db-wal",
            "automation.db-shm": "original-automation.db-shm",
            ".automation.db.ash-lock": ".original-automation.db.ash-lock",
        }
        replacement_map = {
            "replacement.db": "automation.db",
            "replacement.db-wal": "automation.db-wal",
            "replacement.db-shm": "automation.db-shm",
            ".replacement.db.ash-lock": ".automation.db.ash-lock",
        }
        try:
            for source, destination in original_map.items():
                path = state / source
                if path.exists():
                    path.rename(state / destination)
        except OSError as exc:
            pytest.skip(f"open database file-set replacement is unavailable: {exc}")
        for source, destination in replacement_map.items():
            path = state / source
            if path.exists():
                path.rename(state / destination)

        await worker._run_maintenance()

        assert store.get_run(original_claim.run.run_id) is None
        with AutomationStore(
            state / "automation.db", clock=lambda: clock[0]
        ) as visible_store:
            assert visible_store.get_run(replacement_run_id) is not None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_automation_maintenance_preserves_sessions_when_retention_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    automation_store = AutomationStore(tmp_path / "automation.db")
    session_store = SessionStore(tmp_path / "sessions.db")
    session = session_store.create_session(str(workspace))
    with get_db_connection(session_store.db_path) as connection, connection:
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        connection.execute(
            "UPDATE sessions SET created_at = ?, updated_at = ? WHERE session_id = ?",
            (old, old, session.session_id),
        )

    warnings: list[str] = []
    monkeypatch.setattr(
        "ash.automation.worker._log.warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )
    try:
        worker = AutomationWorkerService(
            automation_store,
            workspace,
            session_retention_days=1,
            session_store_path=Path(session_store.db_path),
            maintenance_interval_seconds=1,
        )

        await worker._run_maintenance()
        worker._last_maintenance_at = float("-inf")
        await worker._run_maintenance()

        assert session_store.load_session(session.session_id).session_id == session.session_id
        assert len(warnings) == 1
        assert "skipped session retention" in warnings[0]
    finally:
        automation_store.close()


@pytest.mark.asyncio
async def test_automation_maintenance_cancellation_waits_for_prune_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutomationStore(tmp_path / "automation.db")
    worker = AutomationWorkerService(
        store,
        workspace,
        maintenance_interval_seconds=1,
    )
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original_prune = store._prune_runs_for_workspace_key

    def blocking_prune(*, workspace_key: str, older_than_days: int) -> int:
        started.set()
        assert release.wait(timeout=2)
        try:
            return original_prune(
                workspace_key=workspace_key,
                older_than_days=older_than_days,
            )
        finally:
            completed.set()

    monkeypatch.setattr(store, "_prune_runs_for_workspace_key", blocking_prune)
    maintenance = asyncio.create_task(worker._run_maintenance())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        maintenance.cancel()
        await asyncio.sleep(0)
        assert not maintenance.done()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await maintenance

        assert completed.is_set()
    finally:
        release.set()
        if not maintenance.done():
            maintenance.cancel()
            with pytest.raises(asyncio.CancelledError):
                await maintenance
        store.close()


def test_cron_trigger_handles_spring_forward_and_fall_back() -> None:
    before_transition = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
    schedule = build_schedule(
        cron="30 2 * * sun",
        timezone_name="Europe/Berlin",
        now=before_transition,
    )
    first = first_fire_time(schedule, now=before_transition)
    second = next_fire_time(schedule, previous=first, now=first)

    # The nonexistent 02:30 local occurrence advances to the first valid time.
    assert first == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert second == datetime(2026, 4, 5, 0, 30, tzinfo=timezone.utc)

    before_fall_back = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    fall_back = build_schedule(
        cron="30 2 * * sun",
        timezone_name="Europe/Berlin",
        now=before_fall_back,
    )
    first_fold = first_fire_time(fall_back, now=before_fall_back)
    second_fold = next_fire_time(fall_back, previous=first_fold, now=first_fold)
    assert second_fold is not None
    following_week = next_fire_time(fall_back, previous=second_fold, now=second_fold)

    assert first_fold == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert second_fold == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert following_week == datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)


def test_daily_midnight_cron_does_not_skip_spring_dst_day() -> None:
    zone = "America/Los_Angeles"
    before = datetime(2026, 3, 7, 12, tzinfo=timezone.utc)
    schedule = build_schedule(cron="0 0 * * *", timezone_name=zone, now=before)
    march_eighth = first_fire_time(schedule, now=before)
    march_ninth = next_fire_time(schedule, previous=march_eighth, now=march_eighth)

    assert march_eighth == datetime(2026, 3, 8, 8, tzinfo=timezone.utc)
    assert march_ninth == datetime(2026, 3, 9, 7, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> list[float]:
    return [datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()]


@pytest.fixture
def store(tmp_path: Path, clock: list[float]) -> AutomationStore:
    value = AutomationStore(tmp_path / "automation.db", clock=lambda: clock[0])
    yield value
    value.close()


def test_due_claim_is_atomic_advances_and_completes(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="hourly review",
        prompt="Review the repository",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
    )
    assert job.next_run_at == now + timedelta(hours=1)

    clock[0] += 3600
    second_store = AutomationStore(store.db_path, clock=lambda: clock[0])
    try:
        first_claim = store.claim_due(
            workspace=workspace, worker_id="worker-a", lease_seconds=30
        )
        second_claim = second_store.claim_due(
            workspace=workspace, worker_id="worker-b", lease_seconds=30
        )
    finally:
        second_store.close()

    assert len(first_claim) == 1
    assert second_claim == []
    claim = first_claim[0]
    assert claim.run.status == "running"
    advanced = store.get_job(job.job_id, workspace=workspace)
    assert advanced is not None
    assert advanced.next_run_at == now + timedelta(hours=2)

    completed = store.finish_run(
        claim.run.run_id,
        claim.token,
        status="succeeded",
        session_id="session-1",
        response="done",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    assert completed.status == "succeeded"
    assert completed.session_id == "session-1"
    assert store.get_job(job.job_id, workspace=workspace).consecutive_failures == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("misfire_grace_seconds", 1.5, "misfire_grace_seconds"),
        ("misfire_grace_seconds", True, "misfire_grace_seconds"),
        ("token_budget", 1.5, "token_budget"),
        ("token_budget", True, "token_budget"),
        ("timeout_seconds", True, "timeout_seconds"),
        ("timeout_seconds", float("nan"), "timeout_seconds"),
    ],
)
def test_create_job_rejects_malformed_numeric_values(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    field: str,
    value: object,
    message: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)

    with pytest.raises(ValueError, match=message):
        store.create_job(
            name=f"invalid {field}",
            prompt="Do not persist malformed values",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
            **{field: value},  # type: ignore[arg-type]
        )

    assert store.list_jobs(workspace) == []


@pytest.mark.parametrize("method", ["list_jobs", "list_runs"])
def test_automation_list_apis_reject_fractional_limits(
    tmp_path: Path, store: AutomationStore, method: str
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with pytest.raises(ValueError, match="limit must be between"):
        if method == "list_jobs":
            store.list_jobs(workspace, limit=1.5)  # type: ignore[arg-type]
        else:
            store.list_runs(workspace=workspace, limit=1.5)  # type: ignore[arg-type]


def test_claim_due_rejects_fractional_limit(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with pytest.raises(ValueError, match="limit must be between"):
        store.claim_due(
            workspace=workspace,
            worker_id="worker",
            limit=1.5,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [1.5, True, float("nan"), float("inf")])
def test_finish_run_rejects_non_integer_token_usage(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    value: object,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="typed usage",
        prompt="Run",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    lease = store.claim_manual(job.job_id, workspace=workspace, worker_id="worker")

    with pytest.raises(ValueError, match="non-negative integers"):
        store.finish_run(
            lease.run.run_id,
            lease.token,
            status="succeeded",
            prompt_tokens=value,  # type: ignore[arg-type]
        )

    run = store.get_run(lease.run.run_id)
    assert run is not None
    assert run.status == "running"
    assert run.prompt_tokens == 0


def test_worker_numeric_boundaries_reject_malformed_values(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(ValueError, match="max_concurrent_runs"):
        store.heartbeat_worker(
            worker_id="worker",
            workspace=workspace,
            pid=1,
            max_concurrent_runs=1.5,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="stale_after_seconds"):
        store.list_workers(workspace, stale_after_seconds=float("nan"))
    with pytest.raises(ValueError, match="older_than_days"):
        store.prune_runs(workspace=workspace, older_than_days=1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("token_budget", [1, 2, 5, 6, 10, 1000, 4000])
def test_apply_token_budget_keeps_runtime_config_valid(token_budget: int) -> None:
    from ash.automation.worker import _apply_token_budget
    from ash.context.history import ContextBudgetAllocator

    config = AshConfig(model="ollama/test")
    bounded = _apply_token_budget(config, token_budget)

    assert bounded.max_turn_total_tokens == token_budget
    assert bounded.max_context_tokens >= len(bounded.context_budget_weights) + 1
    assert (
        bounded.max_context_tokens - bounded.max_completion_tokens
        >= len(bounded.context_budget_weights)
    )
    ContextBudgetAllocator(
        max_context_tokens=bounded.max_context_tokens,
        completion_reserve=bounded.max_completion_tokens,
        weights=bounded.context_budget_weights,
    )


def test_store_claims_both_fall_back_occurrences_without_stalling(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    clock[0] = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()
    job = store.create_job(
        name="fall back",
        prompt="Run at both real instants",
        workspace=workspace,
        schedule=build_schedule(
            cron="30 2 * * sun",
            timezone_name="Europe/Berlin",
            now=datetime.fromtimestamp(clock[0], tz=timezone.utc),
        ),
    )

    clock[0] = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
    first = store.claim_due(workspace=workspace, worker_id="worker")[0]
    after_first = store.get_job(job.job_id, workspace=workspace)
    assert after_first is not None
    assert after_first.next_run_at == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    store.finish_run(first.run.run_id, first.token, status="succeeded")

    clock[0] = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp()
    second = store.claim_due(workspace=workspace, worker_id="worker")[0]
    assert second.run.run_id != first.run.run_id
    after_second = store.get_job(job.job_id, workspace=workspace)
    assert after_second is not None
    assert after_second.next_run_at == datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)


def test_expired_one_shot_is_interrupted_and_not_replayed(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="one shot",
        prompt="Run once",
        workspace=workspace,
        schedule=build_schedule(at=(now + timedelta(seconds=20)).isoformat(), now=now),
    )
    clock[0] += 20
    claim = store.claim_due(workspace=workspace, worker_id="worker-a", lease_seconds=5)[
        0
    ]
    assert store.get_job(job.job_id, workspace=workspace).enabled is False

    clock[0] += 6
    assert store.recover_expired() == [claim.run.run_id]
    recovered = store.get_run(claim.run.run_id)
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert "ambiguous" in (recovered.error or "")
    assert store.claim_due(workspace=workspace, worker_id="worker-b") == []


@pytest.mark.asyncio
async def test_misfire_is_skipped_and_recurring_schedule_coalesces(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="frequent",
        prompt="Check state",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        misfire_grace_seconds=5,
    )
    clock[0] += 60

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
        worker_id="worker",
    )
    summary = await asyncio.wait_for(worker.run_forever(once=True), timeout=2)

    assert summary.completed == 1
    assert summary.skipped == 1
    assert summary.ok is True
    runs = store.list_runs(workspace=workspace, job_id=job.job_id)
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    updated = store.get_job(job.job_id, workspace=workspace)
    assert updated is not None
    assert updated.next_run_at == now + timedelta(seconds=70)


@pytest.mark.asyncio
async def test_worker_reports_only_its_workspace_expired_leases(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    first = store.create_job(
        name="first crashed",
        prompt="Crash",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    second = store.create_job(
        name="second crashed",
        prompt="Crash",
        workspace=other_workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    first_claim = store.claim_manual(
        first.job_id, workspace=workspace, worker_id="dead-a", lease_seconds=5
    )
    second_claim = store.claim_manual(
        second.job_id,
        workspace=other_workspace,
        worker_id="dead-b",
        lease_seconds=5,
    )
    clock[0] += 6

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
    )
    summary = await worker.run_forever(once=True)

    assert summary.completed == 1
    assert summary.interrupted == 1
    assert summary.ok is False
    assert store.get_run(first_claim.run.run_id).status == "interrupted"
    assert store.get_run(second_claim.run.run_id).status == "running"
    assert store.recover_expired(workspace=other_workspace) == [
        second_claim.run.run_id
    ]


@pytest.mark.asyncio
async def test_once_worker_scans_past_large_misfire_prefix_to_fill_capacity(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    initial_time = clock[0]
    now = datetime.fromtimestamp(initial_time, tz=timezone.utc)
    for index in range(4):
        store.create_job(
            name=f"stale-{index}",
            prompt="Skip",
            workspace=workspace,
            schedule=build_schedule(every="10s", now=now),
            misfire_grace_seconds=0,
        )
        clock[0] += 0.001
    eligible = store.create_job(
        name="eligible after stale prefix",
        prompt="Run",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        misfire_grace_seconds=60,
    )
    clock[0] = initial_time + 20

    async def factory(config, root):
        return _FakeClient()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        max_concurrent_runs=1,
        config=AshConfig(workspace_root=workspace),
        client_factory=factory,
    )
    summary = await worker.run_forever(once=True)

    assert summary.completed == 5
    assert summary.skipped == 4
    assert summary.succeeded == 1
    assert store.list_runs(workspace=workspace, job_id=eligible.job_id)[0].status == (
        "succeeded"
    )


def test_manual_run_cancel_and_failure_state(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="manual",
        prompt="Run manually",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )

    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="operator", lease_seconds=30
    )
    cancelled = store.request_cancel(claim.run.run_id)
    assert cancelled.cancel_requested is True
    assert store.cancel_requested(claim.run.run_id, claim.token) is True
    result = store.finish_run(
        claim.run.run_id,
        claim.token,
        status="cancelled",
        error="cancelled by operator",
    )
    assert result.status == "cancelled"
    assert store.get_job(job.job_id, workspace=workspace).consecutive_failures == 0

    with pytest.raises(AutomationError, match="not running"):
        store.finish_run(claim.run.run_id, claim.token, status="failed")
    with pytest.raises(AutomationError, match="not running"):
        store.request_cancel(claim.run.run_id, workspace=workspace)

    second_claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="operator",
        lease_seconds=30,
    )
    assert second_claim.run.run_id != claim.run.run_id
    assert second_claim.run.scheduled_for == claim.run.scheduled_for
    assert store.renew_lease(second_claim.run.run_id, second_claim.token).status == (
        "running"
    )


def test_one_shot_cannot_be_resumed_after_its_fire_time(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="one shot pause",
        prompt="Run once",
        workspace=workspace,
        schedule=build_schedule(at=(now + timedelta(seconds=20)).isoformat(), now=now),
    )
    store.set_enabled(job.job_id, workspace=workspace, enabled=False)
    clock[0] += 21

    with pytest.raises(AutomationError, match="cannot be resumed"):
        store.set_enabled(job.job_id, workspace=workspace, enabled=True)


def test_cancellation_is_scoped_to_the_workspace(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="private run",
        prompt="Run privately",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=30
    )

    with pytest.raises(AutomationError, match="not found"):
        store.request_cancel(claim.run.run_id, workspace=other_workspace)
    assert store.get_run(claim.run.run_id).cancel_requested is False


def test_run_retention_preserves_event_ledger(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="retained audit",
        prompt="Finish",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=30
    )
    store.finish_run(claim.run.run_id, claim.token, status="succeeded")
    clock[0] += 2 * 86400

    assert store.prune_runs(workspace=workspace, older_than_days=1) == 1
    assert store.get_run(claim.run.run_id) is None
    with sqlite3.connect(store.db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM automation_events WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
        linked_count = connection.execute(
            "SELECT COUNT(*) FROM automation_events WHERE run_id = ?",
            (claim.run.run_id,),
        ).fetchone()[0]
    assert event_count >= 3
    assert linked_count == 0


def test_retention_is_scoped_to_one_workspace(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    run_ids: list[str] = []
    for index, workspace in enumerate((first_workspace, second_workspace)):
        job = store.create_job(
            name=f"retention-{index}",
            prompt="Finish",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
            enabled=False,
        )
        claim = store.claim_manual(
            job.job_id, workspace=workspace, worker_id=f"worker-{index}"
        )
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")
        run_ids.append(claim.run.run_id)
    clock[0] += 2 * 86400

    assert store.prune_runs(workspace=first_workspace, older_than_days=1) == 1
    assert store.get_run(run_ids[0]) is None
    assert store.get_run(run_ids[1]) is not None


def test_expired_run_is_recovered_before_cancel_or_remove(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="crashed",
        prompt="Crash",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="crashed-worker", lease_seconds=5
    )
    clock[0] += 6

    with pytest.raises(AutomationError, match="not running"):
        store.request_cancel(claim.run.run_id, workspace=workspace)
    assert store.get_run(claim.run.run_id).status == "interrupted"
    assert store.remove_job(job.job_id, workspace=workspace).job_id == job.job_id


def test_finish_samples_lease_time_after_transaction_lock(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="lock wait",
        prompt="Wait for lock",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=5
    )
    original_transaction = store._transaction

    @contextmanager
    def delayed_transaction():
        with original_transaction():
            clock[0] += 6
            yield

    monkeypatch.setattr(store, "_transaction", delayed_transaction)
    with pytest.raises(AutomationError, match="lease expired"):
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")
    assert store.get_run(claim.run.run_id).status == "running"


def test_running_count_is_not_limited_by_history_window(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    active_job = store.create_job(
        name="old active",
        prompt="Stay active",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    store.claim_manual(
        active_job.job_id, workspace=workspace, worker_id="active-worker"
    )
    completed_job = store.create_job(
        name="many completed",
        prompt="Finish",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    for index in range(101):
        clock[0] += 0.001
        claim = store.claim_manual(
            completed_job.job_id,
            workspace=workspace,
            worker_id=f"history-worker-{index}",
        )
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")

    assert all(run.status != "running" for run in store.list_runs(workspace=workspace))
    assert store.count_running_runs(workspace) == 1
    assert store.job_counts(workspace) == (2, 0)


def test_store_refuses_future_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(AutomationError, match="newer than supported"):
        AutomationStore(database)


def test_store_migrates_true_v1_run_rows_to_v2(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v1.db"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with AutomationStore(database) as initial:
        job = initial.create_job(
            name="legacy run",
            prompt="Finish",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            enabled=False,
        )
        claim = initial.claim_manual(
            job.job_id, workspace=workspace, worker_id="legacy-worker"
        )
        initial.finish_run(
            claim.run.run_id,
            claim.token,
            status="succeeded",
            prompt_tokens=7,
            completion_tokens=3,
        )

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            DROP TABLE automation_events;
            ALTER TABLE automation_runs RENAME TO automation_runs_v2;
            CREATE TABLE automation_runs (
                run_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES automation_jobs(job_id),
                scheduled_for REAL NOT NULL,
                trigger TEXT NOT NULL CHECK(trigger IN ('scheduled','manual')),
                status TEXT NOT NULL CHECK(status IN
                    ('running','succeeded','failed','cancelled','interrupted','skipped')),
                attempt INTEGER NOT NULL DEFAULT 1,
                worker_id TEXT,
                lease_token_hash TEXT,
                lease_expires_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                response TEXT,
                error TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            INSERT INTO automation_runs
            SELECT run_id, job_id, scheduled_for, trigger, status, attempt,
                   worker_id, lease_token_hash, lease_expires_at, cancel_requested,
                   session_id, response, error, prompt_tokens, completion_tokens,
                   cost_usd, created_at, started_at, finished_at
            FROM automation_runs_v2;
            DROP TABLE automation_runs_v2;
            PRAGMA user_version=1;
            """
        )

    with AutomationStore(database) as migrated:
        runs = migrated.list_runs(workspace=workspace)
        columns = {
            str(row["name"])
            for row in migrated._conn.execute(
                "PRAGMA table_info(automation_runs)"
            ).fetchall()
        }
        version = int(migrated._conn.execute("PRAGMA user_version").fetchone()[0])

    assert version == AUTOMATION_SCHEMA_VERSION == 3
    assert {
        "cache_read_tokens",
        "cache_write_tokens",
        "usage_source",
        "estimated_prompt_tokens",
        "estimated_completion_tokens",
        "estimated_cost_usd",
    } <= columns
    assert len(runs) == 1
    assert runs[0].prompt_tokens == 7
    assert runs[0].completion_tokens == 3
    assert runs[0].usage_source == "unavailable"
    assert runs[0].cache_read_tokens == 0


def test_store_migrates_true_v2_jobs_to_v3(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v2.db"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with AutomationStore(database) as initial:
        job = initial.create_job(
            name="legacy job",
            prompt="Keep me",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            enabled=False,
        )

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            DROP TABLE automation_deliveries;
            ALTER TABLE automation_jobs RENAME TO automation_jobs_v3;
            CREATE TABLE automation_jobs (
                job_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                prompt TEXT NOT NULL,
                schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('at','every','cron')),
                schedule_value TEXT NOT NULL,
                schedule_timezone TEXT NOT NULL,
                schedule_anchor_at REAL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                next_run_at REAL,
                misfire_grace_seconds INTEGER NOT NULL
                    CHECK(misfire_grace_seconds BETWEEN 0 AND 2592000),
                timeout_seconds REAL NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 86400),
                token_budget INTEGER NOT NULL CHECK(token_budget BETWEEN 1 AND 10000000),
                last_run_at REAL,
                last_run_status TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL
            );
            INSERT INTO automation_jobs
            SELECT job_id, workspace, name, prompt, schedule_kind, schedule_value,
                   schedule_timezone, schedule_anchor_at, enabled, next_run_at,
                   misfire_grace_seconds, timeout_seconds, token_budget,
                   last_run_at, last_run_status, last_error, consecutive_failures,
                   created_at, updated_at, deleted_at
            FROM automation_jobs_v3;
            DROP TABLE automation_jobs_v3;
            PRAGMA user_version=2;
            """
        )

    with AutomationStore(database) as migrated:
        loaded = migrated.get_job(job.job_id, workspace=workspace)
        job_columns = {
            str(row["name"])
            for row in migrated._conn.execute(
                "PRAGMA table_info(automation_jobs)"
            ).fetchall()
        }
        delivery_table = migrated._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='automation_deliveries'"
        ).fetchone()[0]
        version = int(migrated._conn.execute("PRAGMA user_version").fetchone()[0])

    assert version == AUTOMATION_SCHEMA_VERSION == 3
    assert {"webhook_url", "webhook_secret_env"} <= job_columns
    assert delivery_table == 1
    assert loaded is not None
    assert loaded.prompt == "Keep me"
    assert loaded.webhook_url is None
    assert loaded.webhook_secret_env is None


def test_webhook_job_configuration_is_validated_and_persisted(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(ValueError, match="must use https"):
        store.create_job(
            name="bad webhook",
            prompt="No",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            webhook_url="http://example.com/hook",
        )
    with pytest.raises(ValueError, match="query string"):
        store.create_job(
            name="query secret",
            prompt="No",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            webhook_url="https://example.com/hook?token=secret",
        )
    with pytest.raises(ValueError, match="requires webhook_url"):
        store.create_job(
            name="orphan secret",
            prompt="No",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            webhook_secret_env="ASH_WEBHOOK_SECRET",
        )
    with pytest.raises(ValueError, match="control characters"):
        store.create_job(
            name="header injection",
            prompt="No",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            webhook_url="https://example.com/ok\r\nX-Test: injected",
        )

    job = store.create_job(
        name="webhook",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
        webhook_secret_env="ASH_WEBHOOK_SECRET",
    )

    assert job.webhook_url == "https://example.com/hook"
    assert job.webhook_secret_env == "ASH_WEBHOOK_SECRET"


def test_webhook_delivery_recovery_never_replays_ambiguous_dispatch(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="run-worker",
        lease_seconds=5,
    )
    finished = store.finish_run(
        run_claim.run.run_id,
        run_claim.token,
        status="succeeded",
        response="done",
    )
    deliveries = store.list_deliveries(workspace=workspace)
    assert len(deliveries) == 1
    assert deliveries[0].run_id == finished.run_id
    assert deliveries[0].status == "pending"
    assert deliveries[0].attempt == 0

    first = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
        lease_seconds=5,
    )
    assert len(first) == 1
    assert first[0].delivery.status == "delivering"
    assert first[0].delivery.recovery_safe is True
    assert first[0].delivery.attempt == 1

    clock[0] += 6
    assert store.recover_expired_deliveries(workspace=workspace) == [
        first[0].delivery.delivery_id
    ]
    recovered = store.get_delivery(first[0].delivery.delivery_id, workspace=workspace)
    assert recovered is not None
    assert recovered.status == "pending"
    assert recovered.attempt == 1

    second = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker-2",
        lease_seconds=5,
    )
    assert len(second) == 1
    fenced = store.mark_delivery_dispatch_started(
        second[0].delivery.delivery_id, second[0].token
    )
    assert fenced.recovery_safe is False

    clock[0] += 6
    assert store.recover_expired_deliveries(workspace=workspace) == [
        fenced.delivery_id
    ]
    ambiguous = store.get_delivery(fenced.delivery_id, workspace=workspace)
    assert ambiguous is not None
    assert ambiguous.status == "ambiguous"
    assert "outcome is ambiguous" in (ambiguous.last_error or "")
    assert (
        store.claim_pending_deliveries(
            workspace=workspace,
            worker_id="delivery-worker-3",
            lease_seconds=5,
        )
        == []
    )


def test_webhook_delivery_getter_is_workspace_scoped(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    job = store.create_job(
        name="scoped delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery = store.list_deliveries(workspace=workspace)[0]

    assert store.get_delivery(delivery.delivery_id, workspace=workspace) == delivery
    assert store.get_delivery(delivery.delivery_id, workspace=other_workspace) is None


def test_webhook_delivery_terminal_success_requires_dispatch_fence(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="fenced terminal state",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace, worker_id="delivery-worker"
    )[0]
    delivery_id = delivery_claim.delivery.delivery_id

    with pytest.raises(AutomationError, match="dispatch fence"):
        store.finish_delivery(
            delivery_id,
            delivery_claim.token,
            status="delivered",
            response_status=204,
        )
    with pytest.raises(AutomationError, match="dispatch fence"):
        store.finish_delivery(
            delivery_id,
            delivery_claim.token,
            status="ambiguous",
            error="unknown outcome",
        )

    still_owned = store.get_delivery(delivery_id, workspace=workspace)
    assert still_owned is not None
    assert still_owned.status == "delivering"
    assert still_owned.recovery_safe is True

    with pytest.raises(ValueError, match="failed webhook cannot use a 2xx"):
        store.finish_delivery(
            delivery_id,
            delivery_claim.token,
            status="failed",
            response_status=204,
        )
    with pytest.raises(ValueError, match="ambiguous webhook cannot include"):
        store.finish_delivery(
            delivery_id,
            delivery_claim.token,
            status="ambiguous",
            response_status=503,
        )

    failed = store.finish_delivery(
        delivery_id,
        delivery_claim.token,
        status="failed",
        error="pre-dispatch validation failed",
    )
    assert failed.status == "failed"
    assert failed.recovery_safe is True


def test_webhook_delivery_claim_is_atomic_across_store_connections(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="single owner delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")

    second_store = AutomationStore(store.db_path, clock=lambda: clock[0])
    try:
        first = store.claim_pending_deliveries(
            workspace=workspace, worker_id="delivery-worker-a"
        )
        second = second_store.claim_pending_deliveries(
            workspace=workspace, worker_id="delivery-worker-b"
        )
    finally:
        second_store.close()

    assert len(first) == 1
    assert second == []
    assert first[0].delivery.attempt == 1


def test_run_retention_preserves_pending_webhook_outbox(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="retained delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    finished = store.finish_run(
        run_claim.run.run_id,
        run_claim.token,
        status="succeeded",
    )
    clock[0] += 2 * 86_400

    assert store.prune_runs(workspace=workspace, older_than_days=1) == 0
    assert store.get_run(finished.run_id) is not None

    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )[0]
    store.mark_delivery_dispatch_started(
        delivery_claim.delivery.delivery_id,
        delivery_claim.token,
    )
    store.finish_delivery(
        delivery_claim.delivery.delivery_id,
        delivery_claim.token,
        status="delivered",
        response_status=204,
    )

    assert store.prune_runs(workspace=workspace, older_than_days=1) == 1
    assert store.get_run(finished.run_id) is None


def test_webhook_delivery_survives_job_removal(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="removed delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    finished = store.finish_run(
        run_claim.run.run_id,
        run_claim.token,
        status="succeeded",
    )
    store.remove_job(job.job_id, workspace=workspace)

    claimed = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )

    assert len(claimed) == 1
    assert claimed[0].job.job_id == job.job_id
    assert claimed[0].run.run_id == finished.run_id
    assert claimed[0].delivery.run_id == finished.run_id


def test_webhook_delivery_can_target_new_run_without_draining_older_backlog(
    tmp_path: Path, store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="targeted delivery",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    first_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker-1"
    )
    first_run = store.finish_run(
        first_claim.run.run_id,
        first_claim.token,
        status="succeeded",
    )
    second_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker-2"
    )
    second_run = store.finish_run(
        second_claim.run.run_id,
        second_claim.token,
        status="succeeded",
    )

    targeted = store.claim_delivery_for_run(
        second_run.run_id,
        workspace=workspace,
        worker_id="delivery-worker",
    )

    assert targeted is not None
    assert targeted.run.run_id == second_run.run_id
    deliveries = store.list_deliveries(workspace=workspace)
    by_run = {delivery.run_id: delivery for delivery in deliveries}
    assert by_run[first_run.run_id].status == "pending"
    assert by_run[second_run.run_id].status == "delivering"


@pytest.mark.asyncio
async def test_webhook_preflight_builds_stable_signed_payload(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="signed delivery",
        prompt="TOP SECRET PROMPT",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
        webhook_secret_env="ASH_WEBHOOK_SECRET",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(
        run_claim.run.run_id,
        run_claim.token,
        status="succeeded",
        response="safe result",
        prompt_tokens=7,
        completion_tokens=3,
    )
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(return_value=("93.184.216.34",)),
    )

    prepared = await prepare_webhook(
        delivery_claim,
        environ={"ASH_WEBHOOK_SECRET": "signing-secret"},
    )

    payload = json.loads(prepared.body)
    assert prepared.headers["Idempotency-Key"] == delivery_claim.delivery.delivery_id
    assert prepared.headers["X-Ash-Delivery-Id"] == delivery_claim.delivery.delivery_id
    expected = hmac.new(
        b"signing-secret", prepared.body, hashlib.sha256
    ).hexdigest()
    assert prepared.headers["X-Ash-Signature"] == f"sha256={expected}"
    assert payload["job"] == {"job_id": job.job_id, "name": "signed delivery"}
    assert payload["run"]["response"] == "safe result"
    assert payload["run"]["prompt_tokens"] == 7
    assert "prompt" not in payload["job"]
    assert "workspace" not in payload["job"]


@pytest.mark.asyncio
async def test_webhook_preflight_rejects_private_target(
    tmp_path: Path,
    store: AutomationStore,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="private webhook",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://127.0.0.1/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )[0]

    with pytest.raises(WebhookDeliveryError, match="non-public"):
        await prepare_webhook(delivery_claim)


@pytest.mark.asyncio
async def test_webhook_sender_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://other.example/redirected"},
        )

    prepared = PreparedWebhook(
        url="https://example.com/hook",
        body=b'{"ok":true}',
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "delivery-id",
        },
    )

    status = await post_webhook(
        prepared,
        transport=httpx.MockTransport(handler),
    )

    assert status == 302
    assert len(requests) == 1
    assert str(requests[0].url) == "https://example.com/hook"


@pytest.mark.asyncio
async def test_worker_fences_webhook_before_network_dispatch(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="fenced webhook",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(return_value=("93.184.216.34",)),
    )
    observed: dict[str, bool] = {}

    async def fake_post(prepared) -> int:
        del prepared
        current = store.get_delivery(delivery_claim.delivery.delivery_id, workspace=workspace)
        assert current is not None
        observed["recovery_safe"] = current.recovery_safe
        return 204

    monkeypatch.setattr("ash.automation.worker.post_webhook", fake_post)
    service = AutomationWorkerService(store, workspace)

    await service._deliver_webhook(delivery_claim)

    finished = store.get_delivery(delivery_claim.delivery.delivery_id, workspace=workspace)
    assert observed == {"recovery_safe": False}
    assert finished is not None
    assert finished.status == "delivered"
    assert finished.response_status == 204


@pytest.mark.asyncio
async def test_worker_marks_uncertain_webhook_transport_ambiguous(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="uncertain webhook",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace,
        worker_id="delivery-worker",
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(return_value=("93.184.216.34",)),
    )

    async def fail_post(prepared) -> int:
        del prepared
        raise WebhookDeliveryError("transport outcome unknown")

    monkeypatch.setattr("ash.automation.worker.post_webhook", fail_post)
    service = AutomationWorkerService(store, workspace)

    await service._deliver_webhook(delivery_claim)

    finished = store.get_delivery(delivery_claim.delivery.delivery_id, workspace=workspace)
    assert finished is not None
    assert finished.status == "ambiguous"
    assert finished.recovery_safe is False
    assert finished.last_error == "transport outcome unknown"
    assert (
        store.claim_pending_deliveries(
            workspace=workspace,
            worker_id="another-worker",
        )
        == []
    )


@pytest.mark.asyncio
async def test_worker_dns_preflight_timeout_fails_before_dispatch_fence(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="dns timeout",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace, worker_id="delivery-worker"
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(side_effect=ValueError("DNS resolution timed out for host 'example.com'")),
    )
    post = AsyncMock(return_value=204)
    monkeypatch.setattr("ash.automation.worker.post_webhook", post)

    await AutomationWorkerService(store, workspace)._deliver_webhook(delivery_claim)

    post.assert_not_awaited()
    finished = store.get_delivery(
        delivery_claim.delivery.delivery_id, workspace=workspace
    )
    assert finished is not None
    assert finished.status == "failed"
    assert finished.recovery_safe is True
    assert "DNS resolution timed out" in (finished.last_error or "")


@pytest.mark.asyncio
async def test_worker_does_not_dispatch_when_webhook_signing_secret_is_missing(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="missing signing secret",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
        webhook_secret_env="ASH_MISSING_WEBHOOK_SECRET",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace, worker_id="delivery-worker"
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(return_value=("93.184.216.34",)),
    )
    post = AsyncMock(return_value=204)
    monkeypatch.setattr("ash.automation.worker.post_webhook", post)

    await AutomationWorkerService(store, workspace)._deliver_webhook(delivery_claim)

    post.assert_not_awaited()
    finished = store.get_delivery(delivery_claim.delivery.delivery_id, workspace=workspace)
    assert finished is not None
    assert finished.status == "failed"
    assert finished.recovery_safe is True
    assert "ASH_MISSING_WEBHOOK_SECRET" in (finished.last_error or "")


@pytest.mark.asyncio
async def test_worker_does_not_replay_known_non_success_webhook_response(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="known webhook failure",
        prompt="Deliver",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
    )
    run_claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="run-worker"
    )
    store.finish_run(run_claim.run.run_id, run_claim.token, status="succeeded")
    delivery_claim = store.claim_pending_deliveries(
        workspace=workspace, worker_id="delivery-worker"
    )[0]
    monkeypatch.setattr(
        "ash.automation.delivery._resolve_public_addresses_with_timeout",
        AsyncMock(return_value=("93.184.216.34",)),
    )
    post = AsyncMock(return_value=503)
    monkeypatch.setattr("ash.automation.worker.post_webhook", post)

    await AutomationWorkerService(store, workspace)._deliver_webhook(delivery_claim)

    post.assert_awaited_once()
    finished = store.get_delivery(delivery_claim.delivery.delivery_id, workspace=workspace)
    assert finished is not None
    assert finished.status == "failed"
    assert finished.response_status == 503
    assert finished.recovery_safe is False
    assert (
        store.claim_pending_deliveries(
            workspace=workspace, worker_id="another-worker"
        )
        == []
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are unavailable")
def test_store_restricts_database_and_wal_sidecar_permissions(
    tmp_path: Path, clock: list[float]
) -> None:
    database = tmp_path / "automation.db"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    with AutomationStore(database, clock=lambda: clock[0]) as private_store:
        private_store.create_job(
            name="private prompt",
            prompt="SECRET_AUTOMATION_PROMPT",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
        )
        sidecars = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        assert all(path.exists() for path in sidecars)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sidecars)


def test_worker_heartbeat_and_soft_delete(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cleanup",
        prompt="Clean up",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
    )
    worker = store.heartbeat_worker(
        worker_id="worker-one",
        workspace=workspace,
        pid=123,
        max_concurrent_runs=2,
    )
    assert worker.pid == 123
    assert [item.worker_id for item in store.list_workers(workspace)] == ["worker-one"]

    removed = store.remove_job(job.name, workspace=workspace)
    assert removed.job_id == job.job_id
    assert store.get_job(job.job_id, workspace=workspace) is None
    assert (
        store.get_job(job.job_id, workspace=workspace, include_deleted=True).job_id
        == job.job_id
    )
    assert store.list_jobs(workspace, include_disabled=True) == []

    store.remove_worker(worker.worker_id)
    assert store.list_workers(workspace) == []


class _FakeClient:
    def __init__(
        self,
        *,
        result: AshResult | None = None,
        wait: bool = False,
    ) -> None:
        self.result = result or AshResult(
            response="automation complete",
            session_id="scheduled-session",
            model="fake/model",
            context_tokens=20,
            prompt_tokens=12,
            completion_tokens=8,
            cache_read_tokens=3,
            cache_write_tokens=2,
            cost_usd=0.02,
            usage_source="mixed",
            estimated_prompt_tokens=4,
            estimated_completion_tokens=2,
            estimated_cost_usd=0.006,
        )
        self.wait = wait
        self.closed = False
        self.started = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        assert text
        assert user_metadata["source"] == "automation"
        return self.result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_worker_blocks_webhook_secret_from_default_automation_child(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    job = store.create_job(
        name="secret isolation",
        prompt="Run safely",
        workspace=workspace,
        schedule=build_schedule(every="1h"),
        enabled=False,
        webhook_url="https://example.com/hook",
        webhook_secret_env="ASH_AUTOMATION_WEBHOOK_SECRET",
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker"
    )
    captured: dict[str, object] = {}
    fake = _FakeClient()

    class FakeSubprocessClient:
        def __init__(
            self,
            config,
            child_workspace,
            *,
            expected_workspace_identity=None,
            blocked_environment_names=frozenset(),
        ) -> None:
            del config, child_workspace, expected_workspace_identity
            captured["blocked"] = blocked_environment_names

        async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
            return await fake.prompt(text, user_metadata=user_metadata)

        async def close(self) -> None:
            await fake.close()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr(
        "ash.automation.worker._SubprocessAutomationClient", FakeSubprocessClient
    )
    worker = AutomationWorkerService(store, workspace)

    result = await worker.execute(claim)

    assert result.status == "succeeded"
    assert captured["blocked"] == frozenset({"ASH_AUTOMATION_WEBHOOK_SECRET"})
    assert fake.closed is True


class _GatedClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        await self.release.wait()
        return self.result


class _CancellationResistantClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        return self.result


class _CloseResistantClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_worker_executes_due_prompt_through_client(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="scheduled agent",
        prompt="Inspect the project",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    client = _FakeClient()
    worker_config = AshConfig(
        workspace_root=workspace,
        db_directory=tmp_path / "session-db",
        model="ollama/automation-test",
    )

    async def factory(config, root):
        assert config.workspace_root == workspace.resolve()
        assert config.db_directory == tmp_path / "session-db"
        assert config.model == "ollama/automation-test"
        assert root == workspace.resolve()
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="test-worker",
        config=worker_config,
        client_factory=factory,
    )
    summary = await worker.run_forever(once=True)
    assert summary.completed == 1
    assert summary.succeeded == 1
    assert summary.ok is True

    runs = store.list_runs(workspace=workspace, job_id=job.job_id)
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert runs[0].response == "automation complete"
    assert runs[0].session_id == "scheduled-session"
    assert runs[0].cache_read_tokens == 3
    assert runs[0].cache_write_tokens == 2
    assert runs[0].usage_source == "mixed"
    assert runs[0].estimated_prompt_tokens == 4
    assert runs[0].estimated_completion_tokens == 2
    assert runs[0].estimated_cost_usd == 0.006
    assert client.closed is True
    assert store.list_workers(workspace) == []


@pytest.mark.asyncio
async def test_worker_executes_documented_small_token_budget_with_valid_config(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.context.history import ContextBudgetAllocator

    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="small token budget",
        prompt="Return a short response",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        token_budget=1000,
        enabled=False,
    )
    client = _FakeClient()
    observed: list[AshConfig] = []

    async def factory(config: AshConfig, root: Path) -> _FakeClient:
        assert root == workspace.resolve()
        observed.append(config)
        ContextBudgetAllocator(
            max_context_tokens=config.max_context_tokens,
            completion_reserve=config.max_completion_tokens,
            weights=config.context_budget_weights,
        )
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(
            workspace_root=workspace,
            db_directory=tmp_path / "session-db",
            model="ollama/automation-test",
        ),
        client_factory=factory,
    )

    result = await worker.run_manual(job.job_id)

    assert result.status == "succeeded"
    assert len(observed) == 1
    assert observed[0].max_turn_total_tokens == 1000
    assert (
        observed[0].max_context_tokens - observed[0].max_completion_tokens
        >= len(observed[0].context_budget_weights)
    )
    assert client.closed is True


@pytest.mark.asyncio
async def test_worker_collector_marks_unfinalizable_owned_run_interrupted(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="finalization fault",
        prompt="Return",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="fault-worker"
    )

    async def fail_execution():
        raise AutomationError("simulated finalization failure")

    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
        worker_id="fault-worker",
    )
    task = asyncio.create_task(fail_execution())
    await asyncio.sleep(0)
    assert task.done()
    worker._tasks[claim.run.run_id] = task
    worker._claims[claim.run.run_id] = claim
    summary = AutomationWorkerSummary()
    worker._collect_finished(summary)

    assert summary.completed == 1
    assert summary.interrupted == 1
    assert summary.ok is False
    run = store.list_runs(workspace=workspace, job_id=job.job_id)[0]
    assert run.status == "interrupted"
    assert "simulated finalization failure" in (run.error or "")


@pytest.mark.asyncio
async def test_worker_refuses_untrusted_workspace(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="trust required",
        prompt="Do not run",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    factory_called = False

    async def factory(config, root):
        nonlocal factory_called
        factory_called = True
        return _FakeClient()

    monkeypatch.setattr(
        "ash.automation.worker.is_workspace_trusted", lambda path: False
    )
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    with pytest.raises(AutomationError, match="not trusted"):
        await worker.run_manual(job.job_id)

    assert store.list_runs(workspace=workspace, job_id=job.job_id) == []
    assert factory_called is False


@pytest.mark.asyncio
async def test_worker_timeout_covers_runtime_initialization(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="slow startup",
        prompt="Never reached",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
        timeout_seconds=1,
    )
    startup_cancelled = asyncio.Event()

    async def factory(config, root):
        try:
            await asyncio.Event().wait()
        finally:
            startup_cancelled.set()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    result = await worker.run_manual(job.job_id)

    assert result.status == "failed"
    assert "wall-clock timeout" in (result.error or "")
    assert startup_cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_timeout_does_not_wait_forever_for_ignored_cancellation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="resists cancellation",
        prompt="Wait forever",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
        timeout_seconds=1,
    )
    client = _CancellationResistantClient()

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    started_at = asyncio.get_running_loop().time()
    result = await worker.run_manual(job.job_id)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert result.status == "failed"
    assert "wall-clock timeout" in (result.error or "")
    assert client.closed is False
    assert elapsed < 2.0
    assert client.cancel_seen.is_set()
    client.release.set()
    for _ in range(20):
        if client.closed:
            break
        await asyncio.sleep(0)
    assert client.closed is True


@pytest.mark.asyncio
async def test_worker_defers_late_client_close_until_factory_operation_settles(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="late client",
        prompt="Must not start after cancellation",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
        timeout_seconds=1,
    )
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    client = _FakeClient()

    async def factory(config, root):
        del config, root
        factory_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_factory.wait()
            return client
        raise AssertionError("factory unexpectedly returned without cancellation")

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    execution = asyncio.create_task(worker.run_manual(job.job_id))
    await factory_started.wait()
    result = await asyncio.wait_for(execution, timeout=2)

    assert result.status == "failed"
    assert "cleanup is deferred" in (result.error or "")
    assert client.closed is False
    assert client.started.is_set() is False

    release_factory.set()
    for _ in range(20):
        if client.closed:
            break
        await asyncio.sleep(0)
    assert client.closed is True
    await asyncio.sleep(0)
    assert worker._deferred_cleanup_tasks == set()


@pytest.mark.asyncio
async def test_worker_retains_client_close_after_repeated_cancellation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="close cancellation",
        prompt="Complete promptly",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    client = _CloseResistantClient()

    async def factory(config, root):
        del config, root
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    claim = store.claim_manual(job.job_id, workspace=workspace, worker_id="close-worker")
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    execution = asyncio.create_task(worker.execute(claim))
    await client.close_started.wait()

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert client.closed is False
    assert len(worker._deferred_cleanup_tasks) == 1
    client.release_close.set()
    for _ in range(20):
        if client.closed and not worker._deferred_cleanup_tasks:
            break
        await asyncio.sleep(0)
    assert client.closed is True
    assert worker._deferred_cleanup_tasks == set()


@pytest.mark.asyncio
async def test_once_worker_stop_cancels_in_flight_batch(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="stop batch",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    service_task = asyncio.create_task(worker.run_forever(once=True))
    await client.started.wait()
    worker.request_stop()
    summary = await asyncio.wait_for(service_task, timeout=2)

    assert summary.stopped is True
    assert summary.cancelled == 1
    assert summary.ok is False
    assert store.list_runs(workspace=workspace, job_id=job.job_id)[0].status == "cancelled"


@pytest.mark.asyncio
async def test_worker_reloads_disabled_setting_before_claim(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="disabled live",
        prompt="Must not run",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    enabled = AshConfig(workspace_root=workspace, automation_enabled=True)
    disabled = enabled.model_copy(update={"automation_enabled": False})
    first_load = True

    def load_config() -> AshConfig:
        nonlocal first_load
        if first_load:
            first_load = False
            return enabled
        return disabled

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, config_loader=load_config)

    with pytest.raises(AutomationError, match="disabled"):
        await worker.run_forever(once=True)
    assert store.list_runs(workspace=workspace, job_id=job.job_id) == []
    assert store.get_worker(worker.worker_id) is None


@pytest.mark.asyncio
async def test_continuous_worker_pauses_and_resumes_with_live_configuration(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    automation_enabled = True

    def load_config() -> AshConfig:
        return AshConfig(
            workspace_root=workspace,
            automation_enabled=automation_enabled,
        )

    async def wait_until(predicate) -> None:
        for _ in range(50):
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("worker state did not settle")

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config_loader=load_config,
        poll_seconds=0.1,
    )
    service = asyncio.create_task(worker.run_forever())
    await wait_until(lambda: store.get_worker(worker.worker_id) is not None)

    automation_enabled = False
    await wait_until(lambda: store.get_worker(worker.worker_id) is None)
    assert service.done() is False

    automation_enabled = True
    await wait_until(lambda: store.get_worker(worker.worker_id) is not None)
    worker.request_stop()
    summary = await asyncio.wait_for(service, timeout=2)

    assert summary.stopped is True
    assert summary.completed == 0
    assert store.get_worker(worker.worker_id) is None


def test_worker_config_loader_preserves_cli_overrides_and_refreshes_user_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    database = tmp_path / "cli-db"
    user_config = home / ".ash" / "ash.toml"
    workspace.mkdir()
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        'sandbox_network = true\nallowed_web_domains = ["old.example"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    startup = AshConfig.load(
        _override_source="cli",
        _override_detail="test CLI",
        workspace_root=workspace,
        db_directory=database,
        safety_tier="plan",
    )
    load = automation_config_loader(startup)

    user_config.write_text(
        'sandbox_network = false\nallowed_web_domains = ["new.example"]\n',
        encoding="utf-8",
    )
    refreshed = load()

    assert refreshed.workspace_root == workspace.resolve()
    assert refreshed.db_directory == database
    assert refreshed.safety_tier == "plan"
    assert refreshed.sandbox_network is False
    assert refreshed.allowed_web_domains == ["new.example"]
    assert refreshed.config_source("db_directory")[0] == "cli"
    assert refreshed.config_source("safety_tier")[0] == "cli"
    assert refreshed.config_source("sandbox_network")[0] == "user"


def test_worker_config_loader_requires_restart_when_database_path_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    first_database = tmp_path / "first-db"
    second_database = tmp_path / "second-db"
    user_config = home / ".ash" / "ash.toml"
    workspace.mkdir()
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        f"db_directory = {json.dumps(str(first_database))}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    startup = AshConfig.load(
        _override_source="cli",
        workspace_root=workspace,
    )
    load = automation_config_loader(startup)

    user_config.write_text(
        f"db_directory = {json.dumps(str(second_database))}\n", encoding="utf-8"
    )

    with pytest.raises(AutomationError, match="restart the worker"):
        load()


@pytest.mark.asyncio
async def test_worker_refreshes_runtime_policy_after_claim_before_client_creation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="tighten live policy",
        prompt="Use current policy",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    unsafe = AshConfig(
        workspace_root=workspace,
        safety_tier="auto_approve",
        allow_unsafe_auto_approve=True,
        sandbox_backend="direct",
        sandbox_network=True,
        allowed_web_domains=[],
    )
    tightened = unsafe.model_copy(
        update={
            "safety_tier": "dry_run",
            "allow_unsafe_auto_approve": False,
            "sandbox_backend": "auto",
            "sandbox_network": False,
            "allowed_web_domains": ["approved.example"],
        }
    )
    loads = iter((unsafe, unsafe, tightened))
    seen: list[AshConfig] = []

    def load_config() -> AshConfig:
        return next(loads)

    async def factory(config, root):
        seen.append(config)
        return _FakeClient()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config_loader=load_config,
        client_factory=factory,
    )
    result = await worker.run_manual(job.job_id)

    assert result.status == "succeeded"
    assert len(seen) == 1
    assert seen[0].safety_tier == "dry_run"
    assert seen[0].allow_unsafe_auto_approve is False
    assert seen[0].sandbox_backend == "auto"
    assert seen[0].sandbox_network is False
    assert seen[0].allowed_web_domains == ["approved.example"]


@pytest.mark.asyncio
async def test_manual_claim_failure_removes_worker_heartbeat(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="already running",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    existing = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="existing-worker",
    )
    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, worker_id="manual-worker")

    with pytest.raises(AutomationError, match="already has an active run"):
        await worker.run_manual(job.job_id)
    assert store.get_worker(worker.worker_id) is None
    store.finish_run(existing.run.run_id, existing.token, status="cancelled")


@pytest.mark.asyncio
async def test_worker_finalizes_claim_cancelled_before_coroutine_start(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="pre-start stop",
        prompt="Do not start",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    real_create_task = asyncio.create_task

    def cancel_execution_task(coro, *, name=None, context=None):
        task = real_create_task(coro, name=name, context=context)
        if (
            name
            and name.startswith("ash-automation-")
            and not name.startswith(("ash-automation-turn-", "ash-automation-lease-"))
        ):
            task.cancel()
        return task

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_task", cancel_execution_task
    )
    worker = AutomationWorkerService(store, workspace, worker_id="stopping-worker")

    summary = await worker.run_forever(once=True)
    assert summary.completed == 1
    assert summary.cancelled == 1
    run = store.list_runs(workspace=workspace, job_id=job.job_id)[0]
    assert run.status == "cancelled"
    assert "before execution began" in (run.error or "")


@pytest.mark.asyncio
async def test_worker_cancels_agent_operation_when_heartbeat_fails(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="heartbeat failure",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="heartbeat-worker",
        lease_seconds=5,
    )
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    async def no_wait(_: float) -> None:
        return None

    def fail_heartbeat() -> None:
        raise AutomationError("heartbeat write failed")

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr("ash.automation.worker.asyncio.sleep", no_wait)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="heartbeat-worker",
        lease_seconds=5,
        client_factory=factory,
    )
    monkeypatch.setattr(worker, "_heartbeat", fail_heartbeat)
    result = await worker.execute(claim)

    assert result.status == "failed"
    assert "heartbeat write failed" in (result.error or "")
    assert client.closed is True


@pytest.mark.asyncio
async def test_worker_honors_cross_process_cancellation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cancel me",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="cancel-worker",
        lease_seconds=5,
        client_factory=factory,
    )
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
        lease_seconds=5,
    )
    execution = asyncio.create_task(worker.execute(claim))
    await client.started.wait()
    store.request_cancel(claim.run.run_id)
    result = await asyncio.wait_for(execution, timeout=2)

    assert result.status == "cancelled"
    assert client.closed is True


@pytest.mark.asyncio
async def test_committed_cancellation_wins_over_immediate_success(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cancel race",
        prompt="Return immediately after cancellation",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    client = _GatedClient()

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
    )
    execution = asyncio.create_task(worker.execute(claim))
    await client.started.wait()
    store.request_cancel(claim.run.run_id, workspace=workspace)
    client.release.set()
    result = await asyncio.wait_for(execution, timeout=2)

    assert result.status == "cancelled"
    assert result.cancel_requested is True
    assert result.response is None


@pytest.mark.asyncio
async def test_lease_monitor_cancels_prompt_when_ownership_is_lost(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="lease loss",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    worker = AutomationWorkerService(store, workspace, lease_seconds=5)
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
        lease_seconds=5,
    )
    prompt_task = asyncio.create_task(asyncio.Event().wait())

    async def no_wait(_: float) -> None:
        return None

    def lose_lease(*args, **kwargs):
        raise AutomationError("lease ownership lost")

    monkeypatch.setattr("ash.automation.worker.asyncio.sleep", no_wait)
    monkeypatch.setattr(store, "renew_lease", lose_lease)
    with pytest.raises(AutomationError, match="ownership lost"):
        await worker._monitor_lease(claim, prompt_task)
    await asyncio.gather(prompt_task, return_exceptions=True)
    assert prompt_task.cancelled()


@pytest.mark.asyncio
async def test_automation_tools_separate_read_and_mutating_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    guard = SafetyGuard(workspace)
    store_path = tmp_path / "automation.db"
    list_tool = ListAutomationsTool(guard, store_path)
    manage_tool = ManageAutomationTool(guard, store_path)
    policy = PermissionPolicy("interactive")

    assert policy.evaluate(list_tool.name, {}).action == PolicyAction.ALLOW
    assert policy.evaluate(manage_tool.name, {}).action == PolicyAction.ASK
    monkeypatch.setattr("ash.tools.automation.is_workspace_trusted", lambda path: True)

    created = await manage_tool.run(
        action="create",
        name="nightly review",
        prompt="Review project health",
        every="1h",
        webhook_url="https://example.com/tool-hook",
        webhook_secret_env="ASH_TOOL_WEBHOOK_SECRET",
    )
    assert created.success is True
    created_payload = json.loads(created.output)
    assert created_payload["webhook_target"] == "https://example.com/…"
    assert created_payload["webhook_secret_env"] == "ASH_TOOL_WEBHOOK_SECRET"
    listed = await list_tool.run(include_disabled=False)
    assert listed.success is True
    assert "nightly review" in listed.output

    payload = created_payload
    paused = await manage_tool.run(action="pause", job=payload["job_id"])
    assert paused.success is True
    assert json.loads(paused.output)["enabled"] is False

    monkeypatch.setattr(
        "ash.tools.automation.is_workspace_trusted", lambda path: False
    )
    resumed = await manage_tool.run(action="resume", job=payload["job_id"])
    assert resumed.success is False
    assert "trusted" in (resumed.error or "")
    removed = await manage_tool.run(action="remove", job=payload["job_id"])
    assert removed.success is True


@pytest.mark.asyncio
async def test_list_automations_is_side_effect_free_when_store_is_absent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "state" / "automation.db"
    tool = ListAutomationsTool(SafetyGuard(workspace), store_path)

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output) == []
    assert not store_path.exists()
    assert not store_path.parent.exists()
