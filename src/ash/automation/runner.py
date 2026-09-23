"""Private child-process entry point for one killable automation turn."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ash.config import AshConfig
from ash.core.redaction import redact_text
from ash.safety.trust import is_workspace_trusted
from ash.sdk import AshClient
from ash.safe_io import read_bounded_text


_RESULT_PREFIX = "ASH_AUTOMATION_RESULT="
_PARENT_LIFELINE_FD_ENV = "ASH_INTERNAL_AUTOMATION_PARENT_LIFELINE_FD"
MAX_AUTOMATION_REQUEST_BYTES = 4 * 1024 * 1024
MAX_AUTOMATION_PROMPT_BYTES = 64 * 1024


def _arm_parent_lifeline() -> None:
    """Hard-stop this isolated POSIX process group if its worker disappears."""

    raw_descriptor = os.environ.pop(_PARENT_LIFELINE_FD_ENV, None)
    if raw_descriptor is None:
        return
    if os.name != "posix":
        raise RuntimeError("automation parent lifeline is unsupported on this platform")
    try:
        descriptor = int(raw_descriptor)
    except ValueError as exc:
        raise RuntimeError("automation parent lifeline descriptor is invalid") from exc
    if descriptor < 0:
        raise RuntimeError("automation parent lifeline descriptor is invalid")
    try:
        os.fstat(descriptor)
        os.set_inheritable(descriptor, False)
    except OSError as exc:
        raise RuntimeError("automation parent lifeline descriptor is unavailable") from exc
    getpgrp = getattr(os, "getpgrp", None)
    killpg = getattr(os, "killpg", None)
    if not callable(getpgrp) or not callable(killpg):
        raise RuntimeError("automation parent lifeline requires POSIX process groups")
    process_group = int(getpgrp())
    if process_group != os.getpid():
        raise RuntimeError("automation runner is not isolated in its own process group")

    def watch() -> None:
        try:
            while True:
                try:
                    chunk = os.read(descriptor, 1)
                except InterruptedError:
                    continue
                if not chunk:
                    break
        except OSError:
            pass
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            killpg(process_group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            os._exit(125)

    threading.Thread(
        target=watch,
        name="ash-automation-parent-lifeline",
        daemon=True,
    ).start()


async def _execute(request: dict[str, Any]) -> dict[str, Any]:
    raw_config = request.get("config")
    if not isinstance(raw_config, dict):
        raise ValueError("automation request config must be an object")
    config = AshConfig.model_validate(raw_config)
    workspace = Path(str(request.get("workspace", ""))).expanduser().resolve()
    if config.workspace_root.resolve() != workspace:
        raise ValueError("automation request workspace does not match its config")
    if not workspace.is_dir():
        raise ValueError(f"automation workspace is missing: {workspace}")
    if not is_workspace_trusted(workspace):
        raise ValueError(
            "automation workspace is not trusted; run `ash trust add` before retrying"
        )
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("automation request prompt must be non-empty")
    if len(prompt.encode("utf-8")) > MAX_AUTOMATION_PROMPT_BYTES:
        raise ValueError(
            f"automation request prompt exceeds {MAX_AUTOMATION_PROMPT_BYTES} bytes"
        )
    metadata = request.get("user_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("automation request metadata must be an object")

    client = await AshClient.create(
        config=config,
        workspace=workspace,
        workspace_trusted=True,
        run_maintenance=False,
    )
    try:
        result = await client.prompt(prompt, user_metadata=metadata)
        return {"ok": True, "result": asdict(result)}
    finally:
        await client.close()


def main() -> int:
    try:
        _arm_parent_lifeline()
        request = json.loads(
            read_bounded_text(
                sys.stdin,
                MAX_AUTOMATION_REQUEST_BYTES,
                label="automation request",
            )
        )
        if not isinstance(request, dict):
            raise ValueError("automation request must be an object")
        payload = asyncio.run(_execute(request))
        exit_code = 0
    except BaseException as exc:  # child must always answer the parent protocol
        payload = {"ok": False, "error": redact_text(str(exc))}
        exit_code = 1
    print(
        _RESULT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
