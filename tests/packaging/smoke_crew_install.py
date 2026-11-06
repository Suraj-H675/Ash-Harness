"""Exercise the installed Crew CLI with two isolated profiles and a loopback model."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def main() -> None:
    executable = Path(sys.executable).with_name("ash")
    assert executable.is_file(), "Ash's installed console entry point is missing"
    seen: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - HTTP handler interface
            assert self.path == "/v1/chat/completions"
            size = int(self.headers["Content-Length"])
            assert 0 < size < 40_000
            payload = json.loads(self.rfile.read(size))
            model = payload["model"]
            assert model in {"smoke-atlas", "smoke-scout"}
            assert payload.get("stream") is True
            assert not payload.get("tools")
            name = model.split("-")[-1]
            authorization = self.headers.get("Authorization", "")
            assert authorization == f"Bearer fixture-{name}-key"
            request_text = json.dumps(payload, ensure_ascii=False)
            assert f"fixture-{'scout' if name == 'atlas' else 'atlas'}-key" not in request_text
            with lock:
                seen.append((model, authorization, request_text))
            answer = f"CREW-{name.upper()}-SMOKE"
            data = {
                "id": f"{name}-completion",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": "stop"}],
            }
            body = ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ash-crew-wheel-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(mode=0o700)
            ash_dir = home / ".ash"
            profiles = ash_dir / "profiles"
            profiles.mkdir(parents=True, mode=0o700)
            env = {
                name: os.environ[name]
                for name in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")
                if name in os.environ
            }
            env.update({
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "NO_COLOR": "1",
                "ASH_PROFILE": "scout",
                "OPENAI_API_KEY": "untrusted-ambient-key",
            })

            def run(*args: str, success: bool = True) -> str:
                result = subprocess.run(
                    [str(executable), "crew", *args],
                    env=env, cwd=root, capture_output=True, text=True,
                    timeout=45, check=False,
                )
                assert (result.returncode == 0) is success, (
                    args, result.returncode, result.stdout, result.stderr,
                )
                return result.stdout + result.stderr

            identities: dict[str, str] = {}
            for name in ("atlas", "scout"):
                profile = profiles / name
                profile.mkdir(mode=0o700)
                (profile / "ash.toml").write_text(
                    f'model = "openai/smoke-{name}"\nopenai_auth_mode = "api_key"\n',
                    encoding="utf-8",
                )
                credentials = profile / ".env"
                credentials.write_text(
                    f"OPENAI_API_KEY=fixture-{name}-key\n"
                    f"OPENAI_API_BASE=http://127.0.0.1:{server.server_port}/v1\n",
                    encoding="utf-8",
                )
                credentials.chmod(0o600)
                workspace = root / f"{name}-workspace"
                workspace.mkdir(mode=0o700)
                (workspace / "build.log").write_text(
                    f"{name}: test fixture reported a failed build\n", encoding="utf-8",
                )
                output = run(
                    "create", "--owner", "smoke", "--name", name.title(),
                    "--profile", name, "--workspace", str(workspace),
                )
                identities[name] = next(
                    line.removeprefix("ID: ") for line in output.splitlines()
                    if line.startswith("ID: ")
                )
                assert "Lifecycle: active" in output

            assert identities["atlas"] != identities["scout"]
            assert all(name.title() in run("list", "--owner", "smoke") for name in identities)
            turn_ids = {name: str(uuid4()) for name in identities}

            def ask(name: str) -> str:
                return run(
                    "ask", "--owner", "smoke", "--coworker-id", identities[name],
                    "--turn-id", turn_ids[name],
                    "--question", f"Inspect {name}'s isolated fixture",
                    "--evidence", "build.log",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = tuple(pool.map(ask, ("atlas", "scout")))
            for name, result in zip(("atlas", "scout"), results, strict=True):
                assert f"CREW-{name.upper()}-SMOKE" in result, result
                other = "scout" if name == "atlas" else "atlas"
                assert f"CREW-{other.upper()}-SMOKE" not in result
                history = run(
                    "history", "--owner", "smoke",
                    "--coworker-id", identities[name],
                )
                assert "completed" in history, history
                assert f"smoke-{name}" in history, history

            # A deterministic claim survives the process exit and cannot be reused.
            run(
                "ask", "--owner", "smoke", "--coworker-id", identities["atlas"],
                "--turn-id", turn_ids["atlas"], "--question", "Replay blocked",
                success=False,
            )
            assert len(seen) == 2

            atlas = identities["atlas"]
            responsibility = str(uuid4())
            run(
                "responsibility", "create", "--owner", "smoke",
                "--coworker-id", atlas, "--responsibility-id", responsibility,
                "--scope", "Fixture CI", "--objective", "Review build evidence",
                "--success-criterion", "Identify the failing test",
                "--evidence", "build.log",
            )
            outcome = run(
                "responsibility", "run", "--owner", "smoke",
                "--coworker-id", atlas, "--responsibility-id", responsibility,
                "--event-id", "fixture-build-failure-1",
            )
            assert "CREW-ATLAS-SMOKE" in outcome, outcome
            runs = run(
                "responsibility", "runs", "--owner", "smoke",
                "--coworker-id", atlas, "--responsibility-id", responsibility,
            )
            assert "completed" in runs.casefold(), runs
            # A signed event is an independently authenticated, durable wake-up.
            key = os.urandom(32)
            key_path = root / "event.key"  # Outside either coworker workspace.
            key_path.write_text(key.hex(), encoding="ascii")
            key_path.chmod(0o600)
            signed_fields = {
                "version": 1,
                "source": "ci",
                "id": "fixture-signed-event-1",
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "responsibility_id": responsibility,
                "payload": "The fixture build has failed.",
            }
            canonical = json.dumps(
                signed_fields, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode()
            signed_fields["signature"] = hmac.new(
                key, canonical, hashlib.sha256,
            ).hexdigest()
            event = root / "signed-event.json"
            event.write_text(json.dumps(signed_fields), encoding="utf-8")
            event_args = (
                "event", "--owner", "smoke", "--coworker-id", atlas,
                "--responsibility-id", responsibility,
                "--key-file", str(key_path), "--input", str(event),
            )
            assert "CREW-ATLAS-SMOKE" in run(*event_args)
            before_archive = len(seen)
            assert before_archive == 4, seen
            run(*event_args, success=False)
            assert len(seen) == before_archive  # No replay after restart.
            assert "completed" in run(
                "responsibility", "timeline", "--owner", "smoke",
                "--coworker-id", atlas, "--responsibility-id", responsibility,
            ).casefold()
            assert "Lifecycle: archived" in run(
                "archive", "--owner", "smoke", "--coworker-id", atlas,
            )
            run(
                "ask", "--owner", "smoke", "--coworker-id", atlas,
                "--turn-id", str(uuid4()), "--question", "This must not reach the provider",
                success=False,
            )
            assert len(seen) == before_archive
            assert "Lifecycle: archived" in run(
                "show", "--owner", "smoke", "--coworker-id", atlas,
            )
            assert "no coworker" in run(
                "show", "--owner", "foreign", "--coworker-id", atlas,
                success=False,
            ).casefold()
            assert "Lifecycle: active" in run(
                "show", "--owner", "smoke", "--coworker-id", identities["scout"],
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print("installed Crew two-coworker smoke passed")


if __name__ == "__main__":
    main()
