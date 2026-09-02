"""Safe Ollama local-model operations."""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import shutil
import sys

from ash.sandbox.process_utils import terminate_process_tree


MAX_PULL_OUTPUT_CHARS = 20_000
PULL_READ_CHUNK_BYTES = 4096
DEFAULT_PULL_TIMEOUT_SECONDS = 1800
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}(?::[A-Za-z0-9._-]{1,64})?$")


def validate_ollama_model(model: str) -> str:
    """Validate a model reference without interpreting it as a shell command."""

    normalized = model.strip()
    if not normalized or not _MODEL_NAME.fullmatch(normalized):
        raise ValueError("model must be an Ollama model name such as qwen3-coder:7b")
    if ".." in normalized:
        raise ValueError("model must not contain path traversal")
    return normalized


def _scrubbed_environment() -> dict[str, str]:
    from ash.safety.environment import build_scrubbed_environment

    environment = build_scrubbed_environment()
    for name in ("OLLAMA_API_BASE", "OLLAMA_HOST"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


async def pull_model(
    model: str,
    *,
    timeout_seconds: int = DEFAULT_PULL_TIMEOUT_SECONDS,
) -> int:
    """Pull one validated model with bounded output and process cleanup."""

    normalized = validate_ollama_model(model)
    executable = shutil.which("ollama")
    if executable is None:
        print(
            "Error: ollama executable not found. Install Ollama first.",
            file=sys.stderr,
        )
        return 2
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "pull",
            normalized,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_scrubbed_environment(),
        )
    except OSError as exc:
        print(f"Error: could not start ollama pull: {exc}", file=sys.stderr)
        return 2

    async def drain_output() -> None:
        emitted = 0
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(PULL_READ_CHUNK_BYTES)
            if not chunk:
                break
            text = decoder.decode(chunk)
            remaining = MAX_PULL_OUTPUT_CHARS - emitted
            if remaining > 0 and text:
                visible = text[:remaining]
                sys.stdout.write(visible)
                sys.stdout.flush()
                emitted += len(visible)
        trailing = decoder.decode(b"", final=True)
        remaining = MAX_PULL_OUTPUT_CHARS - emitted
        if remaining > 0 and trailing:
            sys.stdout.write(trailing[:remaining])
            sys.stdout.flush()
        await process.wait()

    try:
        await asyncio.wait_for(drain_output(), timeout_seconds)
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        print(f"\nError: ollama pull timed out after {timeout_seconds} seconds.", file=sys.stderr)
        return 124
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
    if process.returncode == 0:
        print(f"Pulled {normalized}.")
    else:
        print(f"ollama pull exited {process.returncode}.", file=sys.stderr)
    return process.returncode or 0
