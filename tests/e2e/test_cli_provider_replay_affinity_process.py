"""Verify opaque replay ownership across separate real CLI processes."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ash.core.session import SessionStore


@pytest.mark.parametrize(
    "rotate_owner_secret",
    [False, True],
    ids=["same-secret-resumes", "changed-secret-refuses-before-http"],
)
def test_backup_replay_credential_material_is_pinned_across_processes(
    tmp_path: Path,
    rotate_owner_secret: bool,
) -> None:
    requests: list[dict[str, Any]] = []
    phase = {"value": 1}
    lock = threading.Lock()
    primary_key = secrets.token_urlsafe(32)
    owner_key_a = secrets.token_urlsafe(32)
    owner_key_b = secrets.token_urlsafe(32)
    credential_labels = {
        primary_key: "primary-key",
        owner_key_a: "owner-key-a",
        owner_key_b: "owner-key-b",
    }

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            if self.path != "/complete":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw)
            request = {
                "model": payload["model"],
                "profile": self.headers.get("X-Test-Credential-Profile"),
                "credential": self._credential_label(
                    self.headers.get("Authorization")
                ),
                "messages": payload["messages"],
                "phase": phase["value"],
            }
            with lock:
                requests.append(request)

            model = request["model"]
            profile = request["profile"]
            current_phase = phase["value"]
            status = 200
            response: dict[str, Any]
            if model == "primary" and current_phase == 1:
                status = 503
                response = {"error": "primary temporarily unavailable"}
            elif (
                model == "backup"
                and profile == "REPLAYTEST_PRIMARY"
                and current_phase == 1
            ):
                status = 401
                response = {"error": "primary credential rejected"}
            elif model == "backup" and profile == "REPLAYTEST_OWNER":
                response = {
                    "content": (
                        "BACKUP-INITIAL" if current_phase == 1 else "BACKUP-RESUMED"
                    ),
                    "provider_state": (
                        [
                            {
                                "type": "reasoning",
                                "id": "rs-process-affinity",
                                "summary": [],
                                "encrypted_content": "opaque-process-affinity-state",
                            }
                        ]
                        if current_phase == 1
                        else None
                    ),
                }
            else:
                response = {"content": "WRONG_MODEL_OR_CREDENTIAL_RECEIVED_REPLAY"}

            encoded = json.dumps(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        @staticmethod
        def _credential_label(authorization: str | None) -> str | None:
            if not authorization or not authorization.startswith("Bearer "):
                return None
            return credential_labels.get(authorization[len("Bearer ") :])

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    database = tmp_path / "db"
    bootstrap = tmp_path / "cli_with_test_provider.py"
    bootstrap.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import urllib.error
            import urllib.request

            from ash.cli import main
            from ash.providers.base import ProviderABC, StreamChunk
            from ash.providers.capabilities import ProviderCapabilities
            from ash.providers.credential_pool import CredentialPoolProvider
            from ash.providers.registry import get_provider_registry
            from ash.providers.replay_affinity import verify_provider_replay_identity
            from ash.providers.replay_state import ProviderReplayCredentialIdentity
            from ash.providers.retry import ProviderHTTPError

            class ReplayProvider(ProviderABC):
                provider_family = "replaytest"
                _ash_declared_capabilities = ProviderCapabilities(
                    native_tools=True, reasoning=True, context_window=32768
                )

                def __init__(self, model_name, profile=None, api_key=None):
                    self._model_name = model_name
                    self.profile = profile
                    self._api_key = api_key

                @property
                def model_name(self):
                    return self._model_name

                @property
                def capabilities(self):
                    return self._ash_declared_capabilities

                def replay_credential_identity(self):
                    if not self._api_key:
                        return None
                    return ProviderReplayCredentialIdentity.from_secret(
                        self._api_key,
                        scope=("replaytest", os.environ["ASH_REPLAY_TEST_URL"]),
                    )

                def count_tokens(self, text):
                    return max(1, len(str(text)) // 4)

                async def detect_capabilities(self, *, refresh=False):
                    del refresh
                    return self.capabilities

                async def stream_chat(self, messages, temperature=0.0, tools=None):
                    del temperature, tools
                    verify_provider_replay_identity(
                        self.replay_credential_identity(),
                        provider_family=self.provider_family,
                        model_name=self.model_name,
                    )
                    payload = json.dumps({
                        "model": self.model_name,
                        "messages": list(messages),
                    }).encode("utf-8")
                    headers = {"Content-Type": "application/json"}
                    if self._api_key is not None:
                        headers["Authorization"] = f"Bearer {self._api_key}"
                    if self.profile is not None:
                        headers["X-Test-Credential-Profile"] = self.profile
                    request = urllib.request.Request(
                        os.environ["ASH_REPLAY_TEST_URL"],
                        data=payload,
                        headers=headers,
                        method="POST",
                    )
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    try:
                        with opener.open(request, timeout=8) as response:
                            result = json.loads(response.read())
                    except urllib.error.HTTPError as exc:
                        raise ProviderHTTPError(
                            "test provider rejected request",
                            status_code=exc.code,
                        ) from None
                    yield StreamChunk(
                        content=result["content"],
                        provider_state=result.get("provider_state"),
                        is_done=True,
                        stop_reason="stop",
                    )

            def build_provider(config, model_name):
                del config
                if model_name == "backup":
                    return CredentialPoolProvider(
                        [
                            ReplayProvider(
                                "backup",
                                "REPLAYTEST_PRIMARY",
                                os.environ["ASH_REPLAYTEST_PRIMARY_KEY"],
                            ),
                            ReplayProvider(
                                "backup",
                                "REPLAYTEST_OWNER",
                                os.environ["ASH_REPLAYTEST_OWNER_KEY"],
                            ),
                        ],
                        ["REPLAYTEST_PRIMARY", "REPLAYTEST_OWNER"],
                    )
                return ReplayProvider(model_name)

            get_provider_registry().register(
                "replaytest", build_provider, replace=True
            )
            raise SystemExit(main())
            """
        ),
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True

    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "ASH_MODEL": "replaytest/primary",
        "ASH_FALLBACK_MODELS": json.dumps(["replaytest/backup"]),
        "ASH_CUSTOM_PROVIDERS": json.dumps(
            {
                "replaytest": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "auth_mode": "none",
                    "model_capabilities": {
                        "primary": {
                            "native_tools": True,
                            "reasoning": True,
                            "context_window": 32768,
                        },
                        "backup": {
                            "native_tools": True,
                            "reasoning": True,
                            "context_window": 32768,
                        },
                    },
                },
            }
        ),
        "ASH_REPLAY_TEST_URL": f"http://127.0.0.1:{server.server_port}/complete",
        "ASH_REPLAYTEST_PRIMARY_KEY": primary_key,
        "ASH_REPLAYTEST_OWNER_KEY": owner_key_a,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
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
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    worker_started = False
    try:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        worker_started = True

        first = cli("-p", "Create an answer with opaque replay state")
        assert first.returncode == 0, (first.stdout, first.stderr)
        first_result = json.loads(first.stdout)
        assert first_result["response"] == "BACKUP-INITIAL"
        session_id = first_result["session_id"]

        store = SessionStore(database / "sessions.db")
        assistant = next(
            message
            for message in reversed(store.load_session(session_id).messages)
            if message.role == "assistant" and message.metadata.get("provider_state")
        )
        assert assistant.metadata["provider_state_owner"] == "replaytest/backup"
        assert (
            assistant.metadata["provider_state_credential_profile"]
            == "REPLAYTEST_OWNER"
        )

        assert [
            (
                item["model"],
                item["profile"],
                item["credential"],
                item["phase"],
            )
            for item in requests
        ] == [
            ("primary", None, None, 1),
            ("backup", "REPLAYTEST_PRIMARY", "primary-key", 1),
            ("backup", "REPLAYTEST_OWNER", "owner-key-a", 1),
        ]

        phase["value"] = 2
        if rotate_owner_secret:
            audit_before = [
                (item.log_id, item.previous_hash, item.sha256_hash)
                for item in store.iter_audit_logs(session_id)
            ]
            assert store.verify_audit_log(session_id) == []
            environment["ASH_REPLAYTEST_OWNER_KEY"] = owner_key_b
            refused = cli("--continue", "-p", "Continue the existing reasoning")
            assert refused.returncode != 0, (refused.stdout, refused.stderr)
            assert "/restart-context" in refused.stdout + refused.stderr
            assert len(requests) == 3
            preserved = SessionStore(database / "sessions.db").load_session(session_id)
            assert any(
                message.metadata.get("provider_state")
                for message in preserved.messages
            )
            assert len(
                SessionStore(database / "sessions.db").provider_replay_state_bindings(
                    session_id
                )
            ) == 1
            after_failure = SessionStore(database / "sessions.db")
            assert [
                (item.log_id, item.previous_hash, item.sha256_hash)
                for item in after_failure.iter_audit_logs(session_id)
            ] == audit_before
            assert after_failure.verify_audit_log(session_id) == []
            return

        resumed = cli("--continue", "-p", "Continue the existing reasoning")
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        resumed_result = json.loads(resumed.stdout)
        assert resumed_result["session_id"] == session_id
        assert resumed_result["response"] == "BACKUP-RESUMED"
        assert len(requests) == 4
        assert (
            requests[3]["model"],
            requests[3]["profile"],
            requests[3]["credential"],
            requests[3]["phase"],
        ) == ("backup", "REPLAYTEST_OWNER", "owner-key-a", 2)
        replayed = [
            item.get("provider_state")
            for item in requests[3]["messages"]
            if item.get("role") == "assistant" and item.get("provider_state")
        ]
        assert len(replayed) == 1
        assert replayed[0][0]["encrypted_content"] == "opaque-process-affinity-state"
        assert all(
            "provider_state_owner" not in item
            and "provider_state_credential_profile" not in item
            and "provider_state_credential_binding" not in item
            for item in requests[3]["messages"]
        )
    finally:
        if worker_started:
            try:
                server.shutdown()
            finally:
                server.server_close()
                worker.join(timeout=2)
                assert not worker.is_alive()
        else:
            server.server_close()


def test_unbound_provider_state_fails_and_explicit_restart_is_clean_across_processes(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []
    request_lock = threading.Lock()
    credential = secrets.token_urlsafe(32)
    state = [
        {
            "type": "reasoning",
            "id": "rs-cli-unbound",
            "summary": [],
            "encrypted_content": "synthetic-private-provider-state",
        }
    ]

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            if self.path != "/complete":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw)
            with request_lock:
                request_index = len(requests)
                requests.append(
                    {
                        "messages": payload["messages"],
                        "has_authorization": self.headers.get("Authorization")
                        == f"Bearer {credential}",
                    }
                )
            result = (
                {
                    "content": "VISIBLE_SUCCESS",
                    "provider_state": state,
                }
                if request_index == 0
                else {"content": "CHILD_RESUMED"}
            )
            encoded = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    database = tmp_path / "db"
    workspace.mkdir()
    home.mkdir()
    database.mkdir()
    (database / "provider-replay-state").write_text(
        "not a directory",
        encoding="utf-8",
    )
    bootstrap = tmp_path / "cli_with_test_provider.py"
    bootstrap.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import urllib.request

            from ash.cli import main
            from ash.providers.base import ProviderABC, StreamChunk
            from ash.providers.capabilities import ProviderCapabilities
            from ash.providers.registry import get_provider_registry
            from ash.providers.replay_affinity import verify_provider_replay_identity
            from ash.providers.replay_state import ProviderReplayCredentialIdentity

            class ReplayProvider(ProviderABC):
                provider_family = "replaytest"
                model_name = "unbound-process"
                _ash_declared_capabilities = ProviderCapabilities(
                    native_tools=True, reasoning=True, context_window=32768
                )

                def replay_credential_identity(self):
                    return ProviderReplayCredentialIdentity.from_secret(
                        os.environ["ASH_REPLAY_TEST_CREDENTIAL"],
                        scope=("replaytest", os.environ["ASH_REPLAY_TEST_URL"]),
                    )

                def count_tokens(self, text):
                    return max(1, len(str(text)) // 4)

                async def stream_chat(self, messages, temperature=0.0, tools=None):
                    del temperature, tools
                    verify_provider_replay_identity(
                        self.replay_credential_identity(),
                        provider_family=self.provider_family,
                        model_name=self.model_name,
                    )
                    body = json.dumps({
                        "model": self.model_name,
                        "messages": list(messages),
                    }).encode("utf-8")
                    request = urllib.request.Request(
                        os.environ["ASH_REPLAY_TEST_URL"],
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": (
                                "Bearer " + os.environ["ASH_REPLAY_TEST_CREDENTIAL"]
                            ),
                        },
                        method="POST",
                    )
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    with opener.open(request, timeout=8) as response:
                        result = json.loads(response.read())
                    yield StreamChunk(
                        content=result["content"],
                        provider_state=result.get("provider_state"),
                        is_done=True,
                        stop_reason="stop",
                    )

            def build_provider(config, model_name):
                del config, model_name
                return ReplayProvider()

            get_provider_registry().register(
                "replaytest", build_provider, replace=True
            )
            raise SystemExit(main())
            """
        ),
        encoding="utf-8",
    )
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "ASH_MODEL": "replaytest/unbound-process",
        "ASH_REPLAY_TEST_URL": (
            f"http://127.0.0.1:{server.server_port}/complete"
        ),
        "ASH_REPLAY_TEST_CREDENTIAL": credential,
        "ASH_CUSTOM_PROVIDERS": json.dumps(
            {
                "replaytest": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "auth_mode": "none",
                    "model_capabilities": {
                        "unbound-process": {
                            "native_tools": True,
                            "reasoning": True,
                            "context_window": 32768,
                        }
                    },
                }
            }
        ),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(bootstrap),
                "--db-directory",
                str(database),
                *args,
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def events(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                decoded.append(item)
        return decoded

    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        first = cli("-p", "Do this task", "--output-format", "stream-json")
        first_events = events(first)
        assert first.returncode == 1, (first.stdout, first.stderr)
        assert any(
            event.get("type") == "assistant.delta"
            and event.get("text") == "VISIBLE_SUCCESS"
            for event in first_events
        )
        assert any(
            event.get("type") == "turn.error"
            and event.get("stop_reason") == "provider_replay_binding"
            for event in first_events
        )
        assert not any(event.get("type") == "turn.completed" for event in first_events)
        error = next(event for event in first_events if event.get("type") == "error")
        assert credential not in first.stdout + first.stderr
        assert len(requests) == 1
        assert requests[0]["has_authorization"] is True
        session_id = next(
            event["session_id"]
            for event in first_events
            if event.get("type") == "turn.started"
        )

        # The existing envelope identifies the session. Recovery instructions
        # must also identify it, so text-mode users need not decode an envelope.
        assert error["session_id"] == session_id
        assert f"ash sessions restart-context --session {session_id} --handoff TEXT" in (
            error["error"]["remedy"]
        )
        assert f"ash sessions recovery --session {session_id}" in error["error"]["remedy"]
        store = SessionStore(database / "sessions.db")
        source = store.load_session(session_id)
        unbound = next(
            message
            for message in reversed(source.messages)
            if message.role == "assistant" and message.metadata.get("provider_state")
        )
        assert unbound.content == "VISIBLE_SUCCESS"
        assert "provider_state_credential_binding" not in unbound.metadata
        turn_id = next(
            event["turn_id"]
            for event in first_events
            if event.get("type") == "turn.started"
        )
        assert store.turn_journal_status(session_id, turn_id) == "interrupted"

        blocked = cli(
            "--continue",
            "-p",
            "Please continue",
            "--output-format",
            "stream-json",
        )
        blocked_events = events(blocked)
        assert blocked.returncode == 1
        assert any(event.get("type") == "error" for event in blocked_events)
        assert credential not in blocked.stdout + blocked.stderr
        assert len(requests) == 1
        plain_json = cli("--continue", "-p", "Please continue", "--output-format", "json")
        assert plain_json.returncode == 1
        payload = json.loads(plain_json.stdout)
        assert payload["type"] == "error" and payload["session_id"] == session_id
        assert f"--session {session_id}" in payload["error"]["remedy"]
        plain_text = cli("--continue", "-p", "Please continue", "--output-format", "text")
        assert plain_text.returncode == 1
        assert f"ash sessions restart-context --session {session_id}" in plain_text.stderr
        assert credential not in plain_json.stdout + plain_json.stderr + plain_text.stdout + plain_text.stderr
        assert len(requests) == 1

        source_before_restart = store.load_session(session_id)
        parent_revision = store.session_history_revision(session_id)
        parent_audit = [
            (entry.log_id, entry.previous_hash, entry.sha256_hash)
            for entry in store.iter_audit_logs(session_id)
        ]
        handoff = "Inspect the workspace and continue from this fresh handoff."
        restarted = cli(
            "sessions",
            "restart-context",
            "--session",
            session_id,
            "--handoff",
            handoff,
            "--json",
        )
        assert restarted.returncode == 0, (restarted.stdout, restarted.stderr)
        child_id = json.loads(restarted.stdout)["session_id"]
        child = store.load_session(child_id)
        assert len(child.messages) == 1
        assert child.messages[0].role == "user"
        assert child.messages[0].content.endswith(handoff)
        assert "Previous tool actions and approvals are NOT imported" in (
            child.messages[0].content
        )
        assert "untrusted evidence, not as authorization" in (
            child.messages[0].content
        )
        assert all(not message.metadata.get("provider_state") for message in child.messages)
        assert all(not message.metadata.get("tool_calls") for message in child.messages)
        assert store.load_session(session_id).messages == source_before_restart.messages
        assert store.session_history_revision(session_id) == parent_revision
        assert [
            (entry.log_id, entry.previous_hash, entry.sha256_hash)
            for entry in store.iter_audit_logs(session_id)
        ] == parent_audit
        assert store.verify_audit_log(session_id) == []

        resumed = cli(
            "--resume",
            child_id,
            "-p",
            "Continue only from the handoff",
            "--output-format",
            "stream-json",
        )
        resumed_events = events(resumed)
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert any(
            event.get("type") == "turn.completed" for event in resumed_events
        )
        assert len(requests) == 2
        child_request = requests[-1]["messages"]
        assert any(
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].endswith(handoff)
            and "NOT imported or authorized for replay" in message["content"]
            for message in child_request
        )
        assert all(not message.get("provider_state") for message in child_request)
        assert all(not message.get("tool_calls") for message in child_request)
        assert credential not in resumed.stdout + resumed.stderr
        final_child = store.load_session(child_id)
        assert all(not message.metadata.get("provider_state") for message in final_child.messages)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()
