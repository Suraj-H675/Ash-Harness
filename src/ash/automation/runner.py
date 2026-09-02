"""Private child-process entry point for one killable automation turn."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ash.config import AshConfig
from ash.core.redaction import redact_text
from ash.safety.trust import is_workspace_trusted
from ash.sdk import AshClient
from ash.safe_io import read_bounded_text


_RESULT_PREFIX = "ASH_AUTOMATION_RESULT="
MAX_AUTOMATION_REQUEST_BYTES = 4 * 1024 * 1024
MAX_AUTOMATION_PROMPT_BYTES = 64 * 1024


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
