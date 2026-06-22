"""Lifecycle-managed Ash HTTP server command."""

from __future__ import annotations

import os

import uvicorn

from ash.sdk import AshClient
from server.http import create_app


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
