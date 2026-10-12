from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GIT = shutil.which("git", path=os.defpath) or "git"
PS = shutil.which("ps", path=os.defpath) or "ps"


class FakeModel:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[dict] = []
        self.parent_calls = 0
        self.child_calls = 0
        self.saw_driver_process = False
        self.live_status_probe = None
        self.running_snapshot = None
        self.auth_headers: list[str | None] = []
        self.server: ThreadingHTTPServer
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _send(self, payload: object) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path != "/api/v1/models":
                    self.send_response(404)
                    self.end_headers()
                    return
                self._send(
                    {
                        "models": [
                            {
                                "key": "local-model",
                                "type": "llm",
                                "loaded_instances": [
                                    {"config": {"context_length": 32768}}
                                ],
                                "capabilities": {
                                    "trained_for_tool_use": True,
                                    "vision": False,
                                },
                            }
                        ]
                    }
                )

            def _stream(
                self, *, tool: tuple[str, dict] | None = None, content: str = ""
            ) -> None:
                chunks: list[dict] = []
                if tool is not None:
                    name, arguments = tool
                    chunks.append(
                        {
                            "id": "fake",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": f"call-{len(model.requests)}",
                                                "type": "function",
                                                "function": {
                                                    "name": name,
                                                    "arguments": json.dumps(arguments),
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    chunks.append(
                        {
                            "id": "fake",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                            ],
                        }
                    )
                else:
                    chunks.append(
                        {
                            "id": "fake",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": content},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    )
                body = (
                    "".join(
                        "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"
                        for chunk in chunks
                    )
                    + "data: [DONE]\n\n"
                )
                encoded = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                model.requests.append(request)
                model.auth_headers.append(self.headers.get("Authorization"))
                names = {
                    entry["function"]["name"] for entry in request.get("tools", [])
                }
                serialized = json.dumps(request.get("messages", []))
                if "spawn_agent" in names:
                    model.parent_calls += 1
                    if model.scenario == "deny":
                        if model.parent_calls == 1:
                            self._stream(
                                tool=(
                                    "spawn_agent",
                                    {
                                        "role": "coder",
                                        "task": "Make an isolated change.",
                                        "agent_id": "denied-coder",
                                        "background": False,
                                        "isolation": "worktree",
                                    },
                                )
                            )
                        else:
                            self._stream(
                                content="The default permission denied the delegation."
                            )
                        return
                    if model.parent_calls == 1:
                        self._stream(
                            tool=(
                                "spawn_agent",
                                {
                                    "role": "coder",
                                    "task": "Read check.py and solution.txt. Edit solution.txt so check.py passes. Report whether you could run the check.",
                                    "agent_id": "probe-coder",
                                    "background": False,
                                    "isolation": "worktree",
                                },
                            )
                        )
                    else:
                        assert "branch=ash-agent/probe-coder commit=" in serialized, (
                            serialized
                        )
                        self._stream(
                            content="Delegation completed and the isolated branch is ready for review."
                        )
                    return
                model.child_calls += 1
                process_list = subprocess.run(
                    [PS, "-axww", "-o", "command="],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                model.saw_driver_process |= any(
                    "ash.agents._agent_driver" in line
                    for line in process_list.splitlines()
                )
                assert "read_file" in names and "write_file" in names, names
                assert "run_command" not in names, names
                if model.child_calls == 1:
                    if model.live_status_probe is not None:
                        model.running_snapshot = model.live_status_probe()
                    self._stream(tool=("read_file", {"file_path": "check.py"}))
                elif model.child_calls == 2:
                    self._stream(tool=("read_file", {"file_path": "solution.txt"}))
                elif model.child_calls == 3:
                    self._stream(
                        tool=(
                            "write_file",
                            {
                                "file_path": "solution.txt",
                                "content": "OK\n",
                                "overwrite": True,
                            },
                        )
                    )
                elif model.child_calls == 4:
                    self._stream(
                        content="solution.txt now satisfies check.py; the coder role has no command runner."
                    )
                else:
                    raise AssertionError(
                        f"unexpected child request {model.child_calls}"
                    )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _run(
    workspace: Path, env: dict[str, str], *args: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ash", "--db-directory", env["ASH_DB_DIRECTORY"], *args],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _environment(home: Path, db: Path, endpoint: str) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "ASH_DB_DIRECTORY": str(db),
        "ASH_MODEL": "lmstudio/local-model",
        "LMSTUDIO_API_BASE": endpoint,
        "ASH_MEMORY_BACKEND": "off",
        "ASH_AGENT_EXECUTION_MODE": "subprocess",
    }


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run([GIT, "init", "-q"], cwd=repo, check=True)
    subprocess.run([GIT, "config", "user.name", "Probe User"], cwd=repo, check=True)
    subprocess.run(
        [GIT, "config", "user.email", "probe@example.invalid"], cwd=repo, check=True
    )
    (repo / "check.py").write_text(
        "from pathlib import Path\nassert Path('solution.txt').read_text() == 'OK\\n'\n"
    )
    (repo / "solution.txt").write_text("BAD\n")
    subprocess.run([GIT, "add", "check.py", "solution.txt"], cwd=repo, check=True)
    subprocess.run([GIT, "commit", "-qm", "initial"], cwd=repo, check=True)


def test_provider_backed_coder_branch_handoff_through_cli(tmp_path: Path) -> None:
    deny_repo = tmp_path / "deny-repo"
    _init_repo(deny_repo)
    deny_model = FakeModel("deny")
    deny_env = _environment(
        tmp_path / "deny-home", tmp_path / "deny-state", deny_model.endpoint
    )
    try:
        denied = _run(
            deny_repo,
            deny_env,
            "-p",
            "Ask one coder to make an isolated change.",
            "--output-format",
            "json",
        )
        assert denied.returncode == 0, (denied.stdout, denied.stderr)
        assert deny_model.parent_calls == 2
        assert deny_model.child_calls == 0
        assert len(deny_model.requests) >= 2
        denied_context = json.dumps(deny_model.requests[-1].get("messages", []))
        assert (
            "denied" in denied_context.casefold()
            or "approval" in denied_context.casefold()
        ), denied_context
        assert (
            json.loads(_run(deny_repo, deny_env, "agents", "tasks", "--json").stdout)[
                "tasks"
            ]
            == []
        )
        assert (
            "No isolated agent branches."
            in _run(deny_repo, deny_env, "agents", "branches").stdout
        )
    finally:
        deny_model.close()

    repo = tmp_path / "success-repo"
    _init_repo(repo)
    model = FakeModel("success")
    env = _environment(
        tmp_path / "success-home", tmp_path / "success-state", model.endpoint
    )
    model.live_status_probe = lambda: _run(
        repo, env, "agents", "tasks", "--state", "running", "--json"
    )
    try:
        launch = _run(repo, env, "permissions", "allow", "spawn_agent")
        assert launch.returncode == 0, launch.stderr
        result = _run(
            repo,
            env,
            "-p",
            "Use exactly one coder to check and repair solution.txt on an isolated branch. Do not apply it to my lead worktree.",
            "--mode",
            "auto_edit",
            "--output-format",
            "stream-json",
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert model.parent_calls >= 2, len(model.requests)
        assert model.child_calls == 4
        assert model.saw_driver_process
        assert (
            model.running_snapshot is not None
            and model.running_snapshot.returncode == 0
        )
        running_tasks = json.loads(model.running_snapshot.stdout)["tasks"]
        assert len(running_tasks) == 1 and running_tasks[0]["state"] == "running", (
            running_tasks
        )
        assert all(value is None for value in model.auth_headers), model.auth_headers
        events = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        ]
        live_types = {event.get("type") for event in events}
        assert "agent.task.created" in live_types, live_types
        assert {"agent.task.running", "agent.task.succeeded"} <= live_types, live_types

        tasks = json.loads(_run(repo, env, "agents", "tasks", "--json").stdout)["tasks"]
        assert len(tasks) == 1 and tasks[0]["state"] == "succeeded", tasks
        task_id = tasks[0]["task_id"]
        durable_events = json.loads(
            _run(repo, env, "agents", "events", "--task", task_id, "--json").stdout
        )["events"]
        durable_types = [entry["event"]["type"] for entry in durable_events]
        required = [
            "agent.task.created",
            "agent.task.leased",
            "agent.task.running",
            "agent.task.succeeded",
        ]
        positions = [durable_types.index(event) for event in required]
        assert positions == sorted(positions), durable_types
        reports = json.loads(_run(repo, env, "agents", "reports", "--json").stdout)[
            "reports"
        ]
        assert len(reports) == 1 and "ash-agent/probe-coder" in json.dumps(reports), (
            reports
        )
        listed = _run(repo, env, "agents", "branches")
        assert listed.returncode == 0 and "ash-agent/probe-coder" in listed.stdout, (
            listed.stdout
        )
        assert (repo / "solution.txt").read_text() == "BAD\n"
        snapshot = tmp_path / "branch-snapshot"
        snapshot.mkdir()
        for name in ("check.py", "solution.txt"):
            content = subprocess.run(
                [GIT, "show", f"ash-agent/probe-coder:{name}"],
                cwd=repo,
                check=True,
                capture_output=True,
            ).stdout
            (snapshot / name).write_bytes(content)
        checked = subprocess.run(
            [sys.executable, "check.py"],
            cwd=snapshot,
            check=False,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, (checked.stdout, checked.stderr)

        (repo / "lead-dirty.txt").write_text("preserve me\n")
        refused = _run(repo, env, "agents", "apply", "ash-agent/probe-coder")
        assert refused.returncode != 0 and "clean lead worktree" in refused.stderr, (
            refused.stdout,
            refused.stderr,
        )
        assert "commit or stash" in refused.stderr.casefold(), refused.stderr
        assert (repo / "lead-dirty.txt").read_text() == "preserve me\n"
        assert (repo / "solution.txt").read_text() == "BAD\n"
        assert "ash-agent/probe-coder" in _run(repo, env, "agents", "branches").stdout
        (repo / "lead-dirty.txt").unlink()

        applied = _run(repo, env, "agents", "apply", "ash-agent/probe-coder")
        assert applied.returncode == 0, (applied.stdout, applied.stderr)
        assert (repo / "solution.txt").read_text() == "OK\n"
        assert (
            _run(repo, env, "agents", "branches").stdout.strip()
            == "No isolated agent branches."
        )
        status = subprocess.run(
            [GIT, "status", "--porcelain=v1"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status == ""
    finally:
        model.close()
