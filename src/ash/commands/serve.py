"""Lifecycle-managed Ash HTTP server command."""

from __future__ import annotations

import os
from typing import Any

from ash.sdk import AshClient
from ash.exceptions import AshError, ErrorCategory


_uvicorn: Any
try:
    import uvicorn as _uvicorn
except ModuleNotFoundError as exc:
    if exc.name != "uvicorn":
        raise
    _uvicorn = None
uvicorn: Any = _uvicorn


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


async def serve_http(args) -> int:
    token = os.environ.get(args.token_env, "")
    if len(token) < 16:
        raise ValueError(
            f"Set {args.token_env} to a bearer token containing at least 16 characters"
        )
    if args.host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise ValueError("Non-loopback binding requires --allow-remote")
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if args.rate_limit < 1:
        raise ValueError("Rate limit must be positive")
    if uvicorn is None:
        raise _server_dependency_error()
    try:
        from ash.server.http import create_app
    except ModuleNotFoundError as exc:
        if exc.name is None or not exc.name.startswith("fastapi"):
            raise
        raise _server_dependency_error() from exc
    client = await AshClient.create()
    try:
        app = create_app(
            client,
            bearer_token=token,
            requests_per_minute=args.rate_limit,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
            )
        )
        await server.serve()
        return 0
    finally:
        await client.close()


def _server_dependency_error() -> AshError:
    return AshError(
        "The optional HTTP server dependencies are not installed.",
        category=ErrorCategory.CONFIG,
        remedy="Install them with `pip install 'ash[server]'`, then rerun `ash serve`.",
        exit_code=2,
    )
