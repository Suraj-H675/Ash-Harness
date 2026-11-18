"""Verify opaque replay ownership across separate real CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ash.core.session import SessionStore


def test_backup_replay_resumes_on_exact_model_and_credential_profile_process(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []
    phase = {"value": 1}
    lock = threading.Lock()

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
            from ash.providers.retry import ProviderHTTPError

            class ReplayProvider(ProviderABC):
                provider_family = "replaytest"
                _ash_declared_capabilities = ProviderCapabilities(
                    native_tools=True, reasoning=True, context_window=32768
                )

                def __init__(self, model_name, profile=None):
                    self._model_name = model_name
                    self.profile = profile

                @property
                def model_name(self):
                    return self._model_name

                @property
                def capabilities(self):
                    return self._ash_declared_capabilities

                def count_tokens(self, text):
                    return max(1, len(str(text)) // 4)

                async def detect_capabilities(self, *, refresh=False):
                    del refresh
                    return self.capabilities

                async def stream_chat(self, messages, temperature=0.0, tools=None):
                    del temperature, tools
                    payload = json.dumps({
                        "model": self.model_name,
                        "messages": list(messages),
                    }).encode("utf-8")
                    headers = {"Content-Type": "application/json"}
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
                            ReplayProvider("backup", "REPLAYTEST_PRIMARY"),
                            ReplayProvider("backup", "REPLAYTEST_OWNER"),
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
            (item["model"], item["profile"], item["phase"]) for item in requests
        ] == [
            ("primary", None, 1),
            ("backup", "REPLAYTEST_PRIMARY", 1),
            ("backup", "REPLAYTEST_OWNER", 1),
        ]

        phase["value"] = 2
        resumed = cli("--continue", "-p", "Continue the existing reasoning")
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        resumed_result = json.loads(resumed.stdout)
        assert resumed_result["session_id"] == session_id
        assert resumed_result["response"] == "BACKUP-RESUMED"
        assert len(requests) == 4
        assert (requests[3]["model"], requests[3]["profile"], requests[3]["phase"]) == (
            "backup",
            "REPLAYTEST_OWNER",
            2,
        )
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
