from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from ash.providers.base import ProviderABC, StreamChunk


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = ROOT / "devtools" / "ash_explorer_controller.py"
RUNNER_PATH = ROOT / "devtools" / "ash_explorer.py"
_spec = importlib.util.spec_from_file_location(
    "ash_explorer_controller_under_test", CONTROLLER_PATH
)
assert _spec is not None and _spec.loader is not None
controller = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = controller
_spec.loader.exec_module(controller)


class FakeProvider(ProviderABC):
    provider_family = "fake-local"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.max_tokens: int | None = None

    @property
    def model_name(self) -> str:
        return "fake-explorer-v1"

    def count_tokens(self, text: str) -> int:
        return len(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        assert tools is None
        assert temperature == 0.0
        user_payload = json.loads(messages[-1]["content"])
        self.calls.append(user_payload)
        history = user_payload["history"]
        if not history:
            decision = {
                "action": "step",
                "label": "create probe",
                "prompt": "Create probe.txt with the requested bytes.",
                "tool_call": {
                    "name": "write_file",
                    "arguments": {
                        "file_path": "probe.txt",
                        "content": "controller-e2e\n",
                        "overwrite": False,
                    },
                },
                "expect": {"kind": "expected_success"},
            }
            reason = "create the probe file"
        elif len(history) == 1:
            assert history[0]["observation"]["classification"] == "expected_success"
            assert history[0]["observation"]["tool_outcome"]["success"] is True
            decision = {
                "action": "step",
                "label": "read probe",
                "prompt": "Read probe.txt and verify its contents.",
                "tool_call": {
                    "name": "read_file",
                    "arguments": {"file_path": "probe.txt"},
                },
                "expect": {"kind": "expected_success"},
            }
            reason = "verify the written file through Ash"
        else:
            assert len(history) == 2
            assert history[-1]["observation"]["classification"] == "expected_success"
            output = history[-1]["observation"]["tool_outcome"]["output"]
            assert "controller-e2e" in output
            decision = {"action": "finish"}
            reason = "write and read both succeeded"
        response = json.dumps(
            {"decision": decision, "reason": reason}, separators=(",", ":")
        )
        yield StreamChunk(content=response, is_done=True, model=self.model_name)


def _require_child_loopback() -> None:
    environment = {
        "PATH": os.defpath,
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); s.close()",
        ],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if probe.returncode == 0:
        return
    detail = probe.stderr.decode("utf-8", errors="replace").strip()
    if "Operation not permitted" in detail or "Permission denied" in detail:
        pytest.skip(f"child-process loopback bind blocked: {detail}")
    pytest.fail(f"child-process loopback capability probe failed: {detail}")


def test_model_directed_run_adapts_from_real_observation_and_replays(
    tmp_path: Path,
) -> None:
    _require_child_loopback()
    evidence = tmp_path / "controller-evidence"
    replay_evidence = tmp_path / "replay-evidence"
    provider = FakeProvider()

    report = asyncio.run(
        controller.run_controller(
            task="Write probe.txt and verify it by reading it back.",
            evidence=evidence,
            seed=41,
            provider=provider,
            require_write_read=True,
        )
    )

    assert report["status"] == "finished"
    assert report["write_read_verified"] is True
    assert report["finish_reason"] == "model_finish"
    assert report["classifications"] == ["expected_success", "expected_success"]
    assert report["replay_path"] == "actions.jsonl"
    assert len(provider.calls) == 3
    assert provider.calls[0]["history"] == []
    assert provider.calls[1]["history"][0]["observation"]["classification"] == (
        "expected_success"
    )
    assert provider.max_tokens == controller.DEFAULT_MAX_TOKENS

    action_lines = (evidence / "actions.jsonl").read_bytes().splitlines()
    assert len(action_lines) == 3
    assert json.loads(action_lines[-1]) == {"action": "finish"}
    first_observation = json.loads(
        (evidence / "steps" / "01" / "observation.json").read_text("utf-8")
    )
    assert (
        first_observation["target_after_sha256"]
        == hashlib.sha256(b"controller-e2e\n").hexdigest()
    )

    journal_events = [
        json.loads(line)
        for line in (evidence / "controller-decisions.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    decision_events = [
        event for event in journal_events if event.get("event") == "decision"
    ]
    assert len(decision_events) == 3
    assert decision_events[0]["request_size_bytes"] > 0
    assert decision_events[0]["response_size_bytes"] > 0
    assert decision_events[1]["observation_classification"] == "expected_success"
    assert all("task" not in json.dumps(event) for event in decision_events)
    assert all(event["replay_path"] == "actions.jsonl" for event in decision_events)
    assert [event["step_index"] for event in decision_events] == [1, 2, None]
    action_hashes = [hashlib.sha256(line).hexdigest() for line in action_lines]
    assert [event["action_sha256"] for event in decision_events] == action_hashes
    observation_hashes = [
        hashlib.sha256(
            controller._json_bytes(call["history"][-1]["observation"])
        ).hexdigest()
        for call in provider.calls[1:]
    ]
    assert [event["observation_sha256"] for event in decision_events] == [
        None,
        *observation_hashes,
    ]
    dispatch_events = [
        event for event in journal_events if event.get("event") == "dispatch"
    ]
    assert [event["dispatch_status"] for event in dispatch_events] == [
        "pending",
        "sent",
        "pending",
        "sent",
        "pending",
        "sent",
    ]
    assert [
        event for event in journal_events if event.get("event") == "observation_ack"
    ] == [
        {
            "action_sha256": action_hashes[0],
            "attempt": 1,
            "dispatch_status": "observed",
            "event": "observation_ack",
            "observation_classification": "expected_success",
            "observation_sha256": observation_hashes[0],
            "step_index": 1,
        },
        {
            "action_sha256": action_hashes[1],
            "attempt": 2,
            "dispatch_status": "observed",
            "event": "observation_ack",
            "observation_classification": "expected_success",
            "observation_sha256": observation_hashes[1],
            "step_index": 2,
        },
    ]
    assert any(event.get("event") == "finish_ack" for event in journal_events)

    replay = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "replay",
            "--steps",
            str(evidence / "actions.jsonl"),
            "--evidence",
            str(replay_evidence),
            "--seed",
            "41",
        ],
        cwd=ROOT,
        env={
            "PATH": os.defpath,
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        timeout=90,
        check=False,
        text=True,
    )
    assert replay.returncode == 0, replay.stderr
    replay_events = [json.loads(line) for line in replay.stdout.splitlines()]
    replay_classifications = [
        event["classification"]
        for event in replay_events
        if event.get("type") == "observation"
    ]
    assert replay_classifications == report["classifications"]


@pytest.mark.parametrize(
    "response",
    [
        '{"decision":{"action":"finish"},"reason":"ok","extra":1}',
        '{"decision":{"action":"finish","action":"step"},"reason":"ok"}',
        '{"decision":{"action":"finish"},"reason":"ok","reason":"again"}',
        '{"decision":{"action":"finish","command":"echo unsafe"},"reason":"ok"}',
    ],
)
def test_controller_rejects_open_or_duplicate_decision_fields(response: str) -> None:
    with pytest.raises(controller.ControllerError):
        controller._parse_decision(response)


def test_local_ollama_adapter_drives_real_ash_cli_without_credentials(
    tmp_path: Path,
) -> None:
    _require_child_loopback()
    requests: list[dict[str, object]] = []

    class LocalModelFixture(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == "/api/chat"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            requests.append(payload)
            assert payload["model"] == "synthetic-local"
            assert payload["stream"] is True
            assert payload["options"]["num_predict"] == controller.DEFAULT_MAX_TOKENS
            assert "tools" not in payload
            history = json.loads(payload["messages"][-1]["content"])["history"]
            if not history:
                action = {
                    "action": "step",
                    "label": "write probe",
                    "prompt": "Write a probe.",
                    "tool_call": {
                        "name": "write_file",
                        "arguments": {
                            "file_path": "probe.txt",
                            "content": "local-adapter\n",
                            "overwrite": False,
                        },
                    },
                    "expect": {"kind": "expected_success"},
                }
            elif len(history) == 1:
                assert (
                    history[-1]["observation"]["classification"] == "expected_success"
                )
                action = {
                    "action": "step",
                    "label": "read probe",
                    "prompt": "Read the probe.",
                    "tool_call": {
                        "name": "read_file",
                        "arguments": {"file_path": "probe.txt"},
                    },
                    "expect": {"kind": "expected_success"},
                }
            else:
                assert len(history) == 2
                assert (
                    "local-adapter"
                    in history[-1]["observation"]["tool_outcome"]["output"]
                )
                action = {"action": "finish"}
            content = json.dumps(
                {"decision": action, "reason": "fixture selected next probe"}
            )
            message = (
                json.dumps(
                    {
                        "model": "synthetic-local",
                        "message": {"role": "assistant", "content": content},
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode()
                + b"\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModelFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = asyncio.run(
            controller._run_cli(
                Namespace(
                    task="Test writing and reading back a file.",
                    evidence=tmp_path / "ollama-adapter",
                    seed=42,
                    model="synthetic-local",
                    port=server.server_port,
                    max_tokens=controller.DEFAULT_MAX_TOKENS,
                    model_timeout=15.0,
                    run_timeout=60.0,
                )
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert report["status"] == "finished", report
    assert report["write_read_verified"] is True
    assert report["classifications"] == ["expected_success", "expected_success"]
    assert len(requests) == 3
    assert report["provider_family"] == "ollama"
    assert report["selected_model"] == "synthetic-local"
    evidence = tmp_path / "ollama-adapter"
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()


def test_chatgpt_subscription_provider_uses_existing_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_child_loopback()
    from ash.providers import openai_chatgpt

    class ChatGPTFixture(FakeProvider):
        provider_family = "openai"

        def __init__(self, model_name: str):
            super().__init__()
            self._model_name = model_name

        @property
        def model_name(self) -> str:
            return self._model_name

    monkeypatch.setattr(openai_chatgpt, "OpenAIChatGPTProvider", ChatGPTFixture)
    evidence = tmp_path / "chatgpt-adapter"
    report = asyncio.run(
        controller._run_cli(
            Namespace(
                task="Write a probe and read it back.",
                evidence=evidence,
                seed=48,
                provider="chatgpt",
                model=None,
                port=11434,
                require_write_read=True,
                max_tokens=controller.DEFAULT_MAX_TOKENS,
                model_timeout=30.0,
                run_timeout=90.0,
            )
        )
    )
    assert report["status"] == "finished", report
    assert report["provider_family"] == "openai"
    assert report["selected_model"] == "gpt-6-luna"
    assert report["requested_model"] == "gpt-6-luna"
    assert report["write_read_verified"] is True
    assert report["classifications"] == ["expected_success", "expected_success"]
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()


def test_chatgpt_provider_rejects_local_port_override() -> None:
    with pytest.raises(ValueError, match="port"):
        asyncio.run(
            controller._run_cli(
                Namespace(
                    task="test",
                    evidence=Path("/tmp/unused-explorer-evidence"),
                    seed=1,
                    provider="chatgpt",
                    model="gpt-6-luna",
                    port=12345,
                    max_tokens=256,
                    model_timeout=30.0,
                    run_timeout=90.0,
                )
            )
        )


def test_unverified_model_finish_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run(_args: Namespace) -> dict[str, object]:
        return {
            "status": "inconclusive",
            "finish_reason": "unverified_completion",
            "write_read_verified": False,
        }

    monkeypatch.setattr(controller, "_run_cli", fake_run)
    result = controller.main(
        [
            "--provider",
            "chatgpt",
            "--task",
            "Write and read a file.",
            "--evidence",
            str(tmp_path / "unused"),
            "--require-write-read",
        ]
    )
    assert result == 2
    assert "unverified_completion" in capsys.readouterr().err


def test_model_cannot_claim_readback_without_a_real_read(tmp_path: Path) -> None:
    _require_child_loopback()

    class PrematureFinish(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            history = json.loads(messages[-1]["content"])["history"]
            if not history:
                action = {
                    "action": "step",
                    "label": "write only",
                    "prompt": "Write a disposable file.",
                    "tool_call": {
                        "name": "write_file",
                        "arguments": {
                            "file_path": "proof.txt",
                            "content": "proof\n",
                            "overwrite": False,
                        },
                    },
                    "expect": {"kind": "expected_success"},
                }
                reason = "write a probe"
            else:
                assert history[0]["observation"]["classification"] == "expected_success"
                action = {"action": "finish"}
                reason = "The file was written and read back successfully."
            yield StreamChunk(
                content=json.dumps({"decision": action, "reason": reason}),
                is_done=True,
                model=self.model_name,
            )

    evidence = tmp_path / "premature-finish"
    report = asyncio.run(
        controller.run_controller(
            task="Write a file and verify it by reading it back.",
            evidence=evidence,
            seed=43,
            provider=PrematureFinish(),
            require_write_read=True,
        )
    )
    assert report["status"] == "inconclusive"
    assert report["finish_reason"] == "unverified_completion"
    assert report["write_read_verified"] is False
    assert report["classifications"] == ["expected_success"]
    actions = [
        json.loads(line)
        for line in (evidence / "actions.jsonl").read_text().splitlines()
    ]
    assert [action["action"] for action in actions] == ["step", "finish"]
    assert actions[0]["tool_call"]["name"] == "write_file"
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()


def test_readback_verifier_rejects_unverified_or_stale_observations() -> None:
    def step(
        tool: str, path: str, classification: str, before: str | None, after: str | None
    ):
        return {
            "action": {"tool_call": {"name": tool, "arguments": {"file_path": path}}},
            "observation": {
                "classification": classification,
                "tool_outcome": {"success": True},
                "target_before_sha256": before,
                "target_after_sha256": after,
            },
        }

    digest, changed = "a" * 64, "b" * 64
    wrote = step("write_file", "same.txt", "expected_success", None, digest)
    read = step("read_file", "same.txt", "expected_success", digest, digest)
    refused = step("write_file", "same.txt", "expected_refusal", digest, digest)
    refused["observation"]["tool_outcome"]["success"] = False
    assert controller._has_verified_write_read([wrote, read]) is True
    assert controller._has_verified_write_read([wrote, refused, read]) is True
    assert controller._has_verified_write_read([wrote]) is False
    assert (
        controller._has_verified_write_read(
            [
                wrote,
                step("write_file", "same.txt", "expected_refusal", digest, changed),
                read,
            ]
        )
        is False
    )
    assert (
        controller._has_verified_write_read(
            [wrote, step("read_file", "other.txt", "expected_success", digest, digest)]
        )
        is False
    )
    assert (
        controller._has_verified_write_read(
            [wrote, step("read_file", "same.txt", "observed", digest, digest)]
        )
        is False
    )
    assert (
        controller._has_verified_write_read(
            [wrote, step("read_file", "same.txt", "expected_success", digest, changed)]
        )
        is False
    )
    assert (
        controller._has_verified_write_read(
            [wrote, step("write_file", "same.txt", "observed", digest, changed), read]
        )
        is False
    )


def test_controller_rejects_unauthorized_model_tool_before_dispatch(
    tmp_path: Path,
) -> None:
    _require_child_loopback()

    class UnsafeProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                content=json.dumps(
                    {
                        "decision": {
                            "action": "step",
                            "label": "unauthorized command",
                            "prompt": "run arbitrary shell",
                            "tool_call": {
                                "name": "execute_shell",
                                "arguments": {"command": "touch private-file"},
                            },
                            "expect": {"kind": "observed"},
                        },
                        "reason": "attempt a command",
                    }
                ),
                is_done=True,
                model=self.model_name,
            )

    evidence = tmp_path / "rejected-decision"
    report = asyncio.run(
        controller.run_controller(
            task="Explore command tool isolation.",
            evidence=evidence,
            seed=43,
            provider=UnsafeProvider(),
        )
    )

    assert report["status"] == "failed"
    assert report["error_code"] == "runner_rejected_action"
    assert report["step_count"] == 0
    actions = [
        json.loads(line)
        for line in (evidence / "actions.jsonl").read_text().splitlines()
    ]
    assert actions == [{"action": "finish"}]
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [("timeout", "provider_timeout"), ("unauthorized", "runner_rejected_action")],
)
def test_controller_preserves_completed_step_when_later_model_decision_fails(
    tmp_path: Path, failure_mode: str, expected_error: str
) -> None:
    _require_child_loopback()

    class FailingSecondDecision(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            history = json.loads(messages[-1]["content"])["history"]
            if not history:
                decision = {
                    "action": "step",
                    "label": "completed write",
                    "prompt": "Create a proof file.",
                    "tool_call": {
                        "name": "write_file",
                        "arguments": {
                            "file_path": "proof.txt",
                            "content": "retained\n",
                            "overwrite": False,
                        },
                    },
                    "expect": {"kind": "expected_success"},
                }
                yield StreamChunk(
                    content=json.dumps({"decision": decision, "reason": "first probe"}),
                    is_done=True,
                    model=self.model_name,
                )
                return
            assert len(history) == 1
            assert history[0]["observation"]["classification"] == "expected_success"
            if failure_mode == "timeout":
                await asyncio.sleep(1)
                return
            decision = {
                "action": "step",
                "label": "prohibited command",
                "prompt": "Run a shell command.",
                "tool_call": {
                    "name": "execute_shell",
                    "arguments": {"command": "echo unauthorized"},
                },
                "expect": {"kind": "observed"},
            }
            yield StreamChunk(
                content=json.dumps({"decision": decision, "reason": "unsafe"}),
                is_done=True,
                model=self.model_name,
            )

    evidence = tmp_path / failure_mode
    report = asyncio.run(
        controller.run_controller(
            task="Stop safely when a later model decision fails.",
            evidence=evidence,
            seed=44,
            provider=FailingSecondDecision(),
            config=controller.ControllerConfig(
                model_timeout_seconds=0.2, run_timeout_seconds=90
            ),
        )
    )

    assert report["status"] == "failed", report
    assert report["error_code"] == expected_error, report
    assert report["classifications"] == ["expected_success"]
    actions = [
        json.loads(line)
        for line in (evidence / "actions.jsonl").read_text().splitlines()
    ]
    assert [action["action"] for action in actions] == ["step", "finish"]
    assert actions[0]["tool_call"]["name"] == "write_file"
    observation = json.loads(
        (evidence / "steps" / "01" / "observation.json").read_text()
    )
    assert (
        observation["target_after_sha256"] == hashlib.sha256(b"retained\n").hexdigest()
    )
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()
