"""Server-side Responses compaction through separate real Ash CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from ash.context.history import resolve_request_token_budget
from ash.core.session import SessionStore
from ash.providers.capabilities import get_capability_registry


def test_compacted_cli_session_replays_anchor_after_restart_without_rewriting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    database = tmp_path / "db"
    for directory in (workspace, home, database):
        directory.mkdir()
    state_path = tmp_path / "fake_provider_count"
    requests_path = tmp_path / "fake_provider_requests.jsonl"
    bootstrap = tmp_path / "cli_with_mocked_openai.py"
    bootstrap.write_text(
        textwrap.dedent(
            """
            import json
            import asyncio
            import os
            from pathlib import Path

            import httpx
            import openai

            counter_path = Path(os.environ["ASH_TEST_COUNTER_PATH"])
            requests_path = Path(os.environ["ASH_TEST_REQUESTS_PATH"])

            def make_item(index):
                if index == 0:
                    return [
                        {
                            "type": "compaction",
                            "id": "cmp_process_fixture",
                            "encrypted_content": "synthetic-encrypted-context",
                            "created_by": "auto",
                        },
                        {
                            "type": "function_call",
                            "id": "fc_write_once",
                            "call_id": "call_write_once",
                            "name": "write_file",
                            "arguments": json.dumps({
                                "file_path": "result.txt",
                                "content": "written once\\n",
                                "overwrite": False,
                            }),
                            "status": "completed",
                        },
                    ]
                if index == 1:
                    return [{
                        "type": "message",
                        "id": "msg_write_report",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{
                            "type": "output_text",
                            "text": "The file was written.",
                            "annotations": [],
                        }],
                    }]
                if index == 2:
                    return [{
                        "type": "function_call",
                        "id": "fc_read_after_restart",
                        "call_id": "call_read_after_restart",
                        "name": "read_file",
                        "arguments": json.dumps({"file_path": "result.txt"}),
                        "status": "completed",
                    }]
                return [{
                    "type": "message",
                    "id": "msg_verified_after_restart",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": "The saved file contents are written once.",
                        "annotations": [],
                    }],
                }]

            def mock_response(request):
                payload = json.loads(request.content)
                index = int(counter_path.read_text()) if counter_path.exists() else 0
                counter_path.write_text(str(index + 1))
                with requests_path.open("a", encoding="utf-8") as log:
                    log.write(json.dumps(payload, sort_keys=True) + "\\n")
                output = make_item(index)
                events = []
                sequence = 1
                for output_index, item in enumerate(output):
                    if item["type"] == "message":
                        text = item["content"][0]["text"]
                        events.append({
                            "type": "response.output_text.delta",
                            "sequence_number": sequence,
                            "output_index": output_index,
                            "content_index": 0,
                            "item_id": item["id"],
                            "delta": text,
                        })
                        sequence += 1
                    events.append({
                        "type": "response.output_item.done",
                        "sequence_number": sequence,
                        "output_index": output_index,
                        "item": item,
                    })
                    sequence += 1
                events.append({
                    "type": "response.completed",
                    "sequence_number": sequence,
                    "response": {
                        "id": "resp_mock_" + str(index),
                        "object": "response",
                        "created_at": 1765000000,
                        "status": "completed",
                        "model": "gpt-6-luna",
                        "output": output,
                        "usage": {
                            "input_tokens": 20,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 8,
                            "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 28,
                        },
                    },
                })
                body = "".join(
                    "event: " + event["type"] + "\\ndata: "
                    + json.dumps(event) + "\\n\\n"
                    for event in events
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=body,
                )

            original_init = openai.AsyncOpenAI.__init__

            def mocked_init(self, *args, **kwargs):
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5

                def keep_local_transport_responses_waking():
                    if loop.time() < deadline:
                        loop.call_later(0.05, keep_local_transport_responses_waking)

                loop.call_later(0.05, keep_local_transport_responses_waking)
                kwargs["http_client"] = httpx.AsyncClient(
                    transport=httpx.MockTransport(mock_response), timeout=10
                )
                kwargs["max_retries"] = 0
                original_init(self, *args, **kwargs)

            openai.AsyncOpenAI.__init__ = mocked_init

            from ash.cli import main

            raise SystemExit(main())
            """
        ),
        encoding="utf-8",
    )

    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(
            [str(tmp_path), str(Path(__file__).resolve().parents[2] / "src")]
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "OPENAI_API_KEY": "synthetic-process-test-key",
        "ASH_MODEL": "openai/gpt-6-luna",
        "ASH_MEMORY_BACKEND": "off",
        "ASH_REPO_MAP_ENABLED": "false",
        "ASH_TEST_COUNTER_PATH": str(state_path),
        "ASH_TEST_REQUESTS_PATH": str(requests_path),
    }

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(bootstrap),
                "--db-directory",
                str(database),
                *args,
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    trusted = subprocess.run(
        [sys.executable, str(bootstrap), "--db-directory", str(database), "trust", "add", str(workspace)],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert trusted.returncode == 0, (trusted.stdout, trusted.stderr)
    initial = cli(
        "--mode",
        "auto_edit",
        "--openai-server-side-compaction",
        "-p",
        "Create result.txt with the verified content.",
    )
    assert initial.returncode == 0, (initial.stdout, initial.stderr)
    initial_result = json.loads(initial.stdout)
    session_id = initial_result["session_id"]
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "written once\n"

    store = SessionStore(database / "sessions.db")
    first_session = store.load_session(session_id)
    anchor_message = next(
        message
        for message in first_session.messages
        if any(
            isinstance(item, dict)
            and item.get("type") == "responses_server_compaction_output"
            for item in message.metadata.get("provider_state", [])
        )
    )
    anchor = anchor_message.metadata["provider_state"]
    assert anchor_message.metadata["provider_state_owner"] == "openai/gpt-6-luna"
    assert first_session.tool_calls[0].tool_name == "write_file"
    assert first_session.tool_calls[0].approved
    assert first_session.tool_calls[0].executed
    assert first_session.tool_calls[0].success

    environment["OPENAI_API_KEY"] = "synthetic-changed-process-test-key"
    changed_credential = cli(
        "--session",
        session_id,
        "--mode",
        "auto_edit",
        "--openai-server-side-compaction",
        "-p",
        "Continue safely after the route changed.",
    )
    assert changed_credential.returncode != 0
    assert "credentials" in changed_credential.stdout
    assert state_path.read_text(encoding="utf-8") == "2"
    environment["OPENAI_API_KEY"] = "synthetic-process-test-key"

    environment["ASH_MAX_CONTEXT_TOKENS"] = "96000"
    changed_context = cli(
        "--session",
        session_id,
        "--mode",
        "auto_edit",
        "--openai-server-side-compaction",
        "-p",
        "Continue safely after the context ceiling changed.",
    )
    assert changed_context.returncode != 0
    assert "context limits changed" in changed_context.stdout
    assert state_path.read_text(encoding="utf-8") == "2"
    environment.pop("ASH_MAX_CONTEXT_TOKENS")

    resumed = cli(
        "--session",
        session_id,
        "--mode",
        "auto_edit",
        "--openai-server-side-compaction",
        "-p",
        "Continue by reading result.txt and confirm its exact contents.",
    )
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    resumed_result = json.loads(resumed.stdout)
    assert resumed_result["session_id"] == session_id
    assert resumed_result["response"] == "The saved file contents are written once."

    session = store.load_session(session_id)
    persisted_anchor_message = next(
        message
        for message in session.messages
        if any(
            isinstance(item, dict)
            and item.get("type") == "responses_server_compaction_output"
            for item in message.metadata.get("provider_state", [])
        )
    )
    assert persisted_anchor_message.metadata["provider_state"] == anchor
    assert [call.tool_name for call in session.tool_calls].count("write_file") == 1
    assert [call.tool_name for call in session.tool_calls].count("read_file") == 1
    assert all(call.executed and call.success for call in session.tool_calls)

    requests = [json.loads(line) for line in requests_path.read_text().splitlines()]
    assert len(requests) == 4
    for request in requests:
        assert request["store"] is False
        management = request["context_management"]
        assert len(management) == 1 and management[0]["type"] == "compaction"
        threshold = management[0]["compact_threshold"]
        assert type(threshold) is int and threshold > 0
    capabilities = get_capability_registry().resolve("openai", "gpt-6-luna")
    budget = resolve_request_token_budget(
        configured_context_tokens=128_000,
        configured_response_tokens=4_000,
        provider_context_window=capabilities.context_window,
        provider_max_input_tokens=capabilities.max_input_tokens,
        provider_max_output_tokens=capabilities.max_output_tokens,
    )
    assert all(
        0 < request["context_management"][0]["compact_threshold"] < budget.input_limit
        for request in requests
    )
    assert requests[0]["input"][0]["role"] == "user"
    for index in (1, 2, 3):
        request_input = requests[index]["input"]
        assert request_input[0]["type"] == "compaction"
        assert request_input[0]["id"] == "cmp_process_fixture"
        assert "created_by" not in request_input[0]  # Output-only Responses field.
        assert sum(item.get("type") == "compaction" for item in request_input) == 1
    second_input_types = [item.get("type") for item in requests[1]["input"]]
    assert second_input_types == [
        "compaction",
        "function_call",
        "function_call_output",
    ]
    third_calls = [
        item.get("call_id")
        for item in requests[2]["input"]
        if item.get("type") == "function_call"
    ]
    assert third_calls == ["call_write_once"]
    assert requests[2]["input"][-1] == {
        "role": "user",
        "content": "Continue by reading result.txt and confirm its exact contents.",
    }
