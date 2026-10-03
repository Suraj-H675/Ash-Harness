"""Lifecycle-managed Ash HTTP server command."""

from __future__ import annotations

import os
from typing import Any

from ash.core.redaction import redact_text
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
HTTP_SERVER_LIMIT_CONCURRENCY = 128
HTTP_SERVER_BACKLOG = 128
HTTP_SERVER_KEEP_ALIVE_SECONDS = 5


async def serve_http(args) -> int:
    token = os.environ.get(args.token_env, "")
    if (
        len(token) < 16
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError(
            f"Set {args.token_env} to a bearer token containing at least 16 "
            "non-whitespace ASCII characters"
        )
    remote = args.host not in LOOPBACK_HOSTS
    if remote and not args.allow_remote:
        raise ValueError("Non-loopback binding requires --allow-remote")
    ssl_certfile = getattr(args, "ssl_certfile", None)
    ssl_keyfile = getattr(args, "ssl_keyfile", None)
    if bool(ssl_certfile) != bool(ssl_keyfile):
        raise ValueError("TLS requires both --ssl-certfile and --ssl-keyfile")
    if remote and not ssl_certfile:
        raise ValueError(
            "Non-loopback binding requires TLS via --ssl-certfile and --ssl-keyfile"
        )
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if args.rate_limit < 1:
        raise ValueError("Rate limit must be positive")
    approval_timeout = float(getattr(args, "approval_timeout", 300.0))
    if not 1 <= approval_timeout <= 3600:
        raise ValueError("Approval timeout must be between 1 and 3600 seconds")
    if uvicorn is None:
        raise _server_dependency_error()
    try:
        from ash.server.http import HTTPApprovalBroker, create_app
    except ModuleNotFoundError as exc:
        if exc.name is None or not exc.name.startswith("fastapi"):
            raise
        raise _server_dependency_error() from exc
    approval_broker = HTTPApprovalBroker(timeout_seconds=approval_timeout)
    client = await AshClient.create(approval_callback=approval_broker.request)
    approval_broker.set_tool_provider(lambda name: client.loop.tools.get(name))
    primary_error: BaseException | None = None
    try:
        app = create_app(
            client,
            bearer_token=token,
            requests_per_minute=args.rate_limit,
            approval_broker=approval_broker,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
                limit_concurrency=HTTP_SERVER_LIMIT_CONCURRENCY,
                backlog=HTTP_SERVER_BACKLOG,
                timeout_keep_alive=HTTP_SERVER_KEEP_ALIVE_SECONDS,
            )
        )
        await server.serve()
        return 0
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        await approval_broker.close()
        try:
            await client.close()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "HTTP server client cleanup failed: "
                + redact_text(str(cleanup_error))
            )


def _server_dependency_error() -> AshError:
    from ash.install import pipx_install_command

    return AshError(
        "The optional HTTP server dependencies are not installed.",
        category=ErrorCategory.CONFIG,
        remedy=(
            f"Run `{pipx_install_command('server')}`, then rerun `ash serve`."
        ),
        exit_code=2,
    )
