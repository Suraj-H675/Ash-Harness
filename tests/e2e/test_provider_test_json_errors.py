from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("endpoint_state", "expected_error"),
    [("malformed", "JSONDecodeError"), ("unreachable", "ConnectError")],
)
def test_provider_test_json_errors_are_written_to_stdout(
    tmp_path: Path,
    endpoint_state: str,
    expected_error: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            body = b'{"data": [}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    if endpoint_state == "malformed":
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
    else:
        server.server_close()
        worker = None

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "ASH_MODEL": "vllm/local-model",
        "VLLM_API_BASE": f"http://127.0.0.1:{port}/v1",
    }

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "providers",
                "test",
                "--json",
                "--timeout",
                "2",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout, (
            "--json provider errors must be written to stdout; "
            f"stderr was {result.stderr!r}"
        )
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert expected_error in payload["error"]
    finally:
        if worker is not None:
            server.shutdown()
            worker.join(timeout=2)
            server.server_close()
