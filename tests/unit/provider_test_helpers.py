from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


def patch_catalog_client(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Callable[[httpx.Request], httpx.Response],
) -> list[tuple[httpx.Request, float]]:
    """Route readiness requests through a real streaming HTTPX client."""

    calls: list[tuple[httpx.Request, float]] = []
    real_client = httpx.Client

    def make_client(*, timeout: float, **_: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request, timeout))
            return response_factory(request)

        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )

    monkeypatch.setattr("ash.providers.readiness.httpx.Client", make_client)
    return calls
