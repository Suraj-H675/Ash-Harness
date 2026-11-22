"""First-party OpenAI Responses server-side compaction contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest


def test_repository_config_cannot_enable_server_side_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.config import AshConfig, PROJECT_CONFIG_FIELDS

    workspace = tmp_path / "workspace"
    user_config = tmp_path / "user"
    (workspace / ".ash").mkdir(parents=True)
    user_config.mkdir()
    (workspace / ".ash" / "config.toml").write_text(
        "openai_server_side_compaction = true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ASH_OPENAI_SERVER_SIDE_COMPACTION", raising=False)

    config = AshConfig.load(
        _ash_dir=user_config,
        workspace_root=workspace,
        db_directory=tmp_path / "db",
    )

    assert config.openai_server_side_compaction is False
    assert "openai_server_side_compaction" not in PROJECT_CONFIG_FIELDS


@pytest.mark.parametrize(
    ("model", "auth_mode", "fallback_models"),
    [
        ("google/gemini-2.5-pro", "api_key", []),
        ("azure/gpt-6-luna", "api_key", []),
        ("ollama/gpt-6-luna", "api_key", []),
        ("openai-compatible/gpt-6-luna", "api_key", []),
        ("openai/not-known", "api_key", []),
        ("openai/gpt-4o", "api_key", []),
        ("openai/gpt-6-luna", "chatgpt", []),
        ("openai/gpt-6-luna", "api_key", ["openai/gpt-6.1-sol"]),
    ],
)
def test_opt_in_rejects_other_routes_before_provider_creation(
    tmp_path: Path,
    model: str,
    auth_mode: str,
    fallback_models: list[str],
) -> None:
    from ash.config import AshConfig
    from ash.providers.registry import get_provider_registry

    config = AshConfig(
        model=model,
        openai_auth_mode=auth_mode,
        openai_server_side_compaction=True,
        fallback_models=fallback_models,
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )

    with pytest.raises(
        ValueError,
        match="first-party API-key Responses model|supported first-party Responses model|fallback",
    ):
        get_provider_registry().build(config)


def test_opt_in_rejects_openai_compatible_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.config import AshConfig
    from ash.providers.registry import get_provider_registry

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://compatible.example/v1")
    config = AshConfig(
        model="openai/gpt-6-luna",
        openai_auth_mode="api_key",
        openai_server_side_compaction=True,
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )

    with pytest.raises(ValueError, match="default first-party.*endpoint"):
        get_provider_registry().build(config)


@pytest.mark.asyncio
async def test_user_config_opt_in_sends_and_replays_sdk_compaction_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.config import AshConfig
    from ash.context.history import resolve_request_token_budget
    from ash.providers.openai_responses import OpenAIResponsesProvider
    from ash.providers.registry import get_provider_registry

    workspace = tmp_path / "work"
    workspace.mkdir()
    user_config = tmp_path / "home" / ".ash"
    user_config.mkdir(parents=True)
    (user_config / "ash.toml").write_text(
        'model = "openai/gpt-6-luna"\n'
        'openai_auth_mode = "api_key"\n'
        "openai_server_side_compaction = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    # Ash's environment override intentionally takes precedence over user
    # config. Other tests may set legacy ASH_MODEL in the same pytest process.
    monkeypatch.delenv("ASH_MODEL", raising=False)
    monkeypatch.delenv("ASH_MODEL_NAME", raising=False)
    monkeypatch.delenv("ASH_PROVIDER", raising=False)

    config = AshConfig.load(
        _ash_dir=user_config,
        workspace_root=workspace,
        db_directory=tmp_path / "db",
    )
    assert config.openai_server_side_compaction is True
    provider = get_provider_registry().build(config)
    provider.configure_server_side_compaction_threshold(80_000)
    assert isinstance(provider, OpenAIResponsesProvider)

    compaction = {
        "type": "compaction",
        "id": "cmp_synthetic",
        "encrypted_content": "synthetic-opaque-state",
    }
    post_anchor = {
        "type": "message",
        "id": "msg_after_compaction",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [
            {
                "type": "output_text",
                "text": "post-anchor output",
                "annotations": [],
            }
        ],
    }
    first_output = [compaction, post_anchor]
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        output = first_output if len(requests) == 1 else []
        events: list[dict[str, Any]] = []
        sequence = 1
        for index, item in enumerate(output):
            if item["type"] == "message":
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": sequence,
                        "output_index": index,
                        "content_index": 0,
                        "item_id": item["id"],
                        "delta": item["content"][0]["text"],
                    }
                )
                sequence += 1
            events.append(
                {
                    "type": "response.output_item.done",
                    "sequence_number": sequence,
                    "output_index": index,
                    "item": item,
                }
            )
            sequence += 1
        events.append(
            {
                "type": "response.completed",
                "sequence_number": sequence,
                "response": {
                    "id": f"resp_synthetic_{len(requests)}",
                    "object": "response",
                    "created_at": 1_765_000_000,
                    "status": "completed",
                    "model": "gpt-6-luna",
                    "output": output,
                    "usage": {
                        "input_tokens": 12,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 3,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 15,
                    },
                },
            }
        )
        body = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            for event in events
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = openai.AsyncOpenAI(
            api_key="synthetic-test-key",
            http_client=http,
            max_retries=0,
        )
        provider._client = client
        provider._owns_client = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5

        def keep_local_transport_responses_waking() -> None:
            if loop.time() < deadline:
                loop.call_later(0.05, keep_local_transport_responses_waking)

        loop.call_later(0.05, keep_local_transport_responses_waking)
        try:
            first = [
                chunk
                async for chunk in provider.stream_chat(
                    [{"role": "user", "content": "Start a long coding task"}]
                )
            ]
            state = first[-1].provider_state
            assert state

            [
                chunk
                async for chunk in provider.stream_chat(
                    [
                        {
                            "role": "assistant",
                            "content": "post-anchor output",
                            "provider_state": state,
                        },
                        {"role": "user", "content": "Continue"},
                    ]
                )
            ]
        finally:
            await client.close()

    assert requests[0]["store"] is False
    assert requests[0]["stream"] is True
    assert requests[0]["context_management"][0]["type"] == "compaction"
    budget = resolve_request_token_budget(
        configured_context_tokens=config.max_context_tokens,
        configured_response_tokens=config.max_completion_tokens,
        provider_context_window=provider.capabilities.context_window,
        provider_max_input_tokens=provider.capabilities.max_input_tokens,
        provider_max_output_tokens=provider.capabilities.max_output_tokens,
    )
    compact_threshold = requests[0]["context_management"][0]["compact_threshold"]
    assert type(compact_threshold) is int
    assert 0 < compact_threshold < budget.input_limit
    assert requests[1]["store"] is False
    assert requests[1]["context_management"] == requests[0]["context_management"]
    replayed = requests[1]["input"]
    assert len(replayed) == 3
    assert replayed[0].get("type") == "compaction"
    assert replayed[0].get("id") == compaction["id"]
    assert replayed[0].get("encrypted_content") == compaction["encrypted_content"]
    assert replayed[1].get("id") == post_anchor["id"]
    assert replayed[1].get("content", [])[0].get("text") == "post-anchor output"
    assert replayed[1].get("phase") == "final_answer"
    assert replayed[2] == {"role": "user", "content": "Continue"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["malformed", "malicious", "oversized", "incomplete", "failed"],
)
async def test_invalid_compaction_stream_fails_closed_without_opaque_leak(
    failure_mode: str,
) -> None:
    from ash.providers.base import ProviderIncompleteStreamError
    from ash.providers.openai_responses import OpenAIResponsesProvider
    from ash.providers.messages import MAX_PROVIDER_STATE_BYTES

    opaque_secret = "synthetic-opaque-secret-marker"
    compaction: dict[str, Any] = {
        "type": "compaction",
        "id": "cmp_negative_fixture",
        "encrypted_content": opaque_secret,
        "created_by": "auto",
    }
    if failure_mode == "malformed":
        compaction.pop("id")
    elif failure_mode == "malicious":
        compaction["created_by"] = "untrusted-caller"
    elif failure_mode == "oversized":
        compaction["encrypted_content"] = opaque_secret + (
            "x" * MAX_PROVIDER_STATE_BYTES
        )

    output_event = {
        "type": "response.output_item.done",
        "sequence_number": 1,
        "output_index": 0,
        "item": compaction,
    }
    events: list[dict[str, Any]] = [output_event]
    if failure_mode == "failed":
        events.append(
            {
                "type": "response.failed",
                "sequence_number": 2,
                "response": {
                    "id": "resp_negative_fixture",
                    "object": "response",
                    "created_at": 1_765_000_000,
                    "status": "failed",
                    "model": "gpt-6-luna",
                    "output": [],
                    "error": {"code": "server_error", "message": opaque_secret},
                },
            }
        )
    elif failure_mode != "incomplete":
        events.append(
            {
                "type": "response.completed",
                "sequence_number": 2,
                "response": {
                    "id": "resp_negative_fixture",
                    "object": "response",
                    "created_at": 1_765_000_000,
                    "status": "completed",
                    "model": "gpt-6-luna",
                    "output": [compaction],
                    "usage": {
                        "input_tokens": 10,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 1,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 11,
                    },
                },
            }
        )
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = openai.AsyncOpenAI(
            api_key="synthetic-test-key",
            http_client=http,
            max_retries=0,
        )
        provider = OpenAIResponsesProvider("gpt-6-luna", "synthetic-test-key", client=client)
        provider.enable_server_side_compaction()
        provider.configure_server_side_compaction_threshold(80_000)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5

        def keep_local_transport_responses_waking() -> None:
            if loop.time() < deadline:
                loop.call_later(0.05, keep_local_transport_responses_waking)

        loop.call_later(0.05, keep_local_transport_responses_waking)
        try:
            expected = (
                ProviderIncompleteStreamError
                if failure_mode == "incomplete"
                else RuntimeError
            )
            with pytest.raises(expected) as failure:
                [
                    chunk
                    async for chunk in provider.stream_chat(
                        [{"role": "user", "content": "Continue safely"}]
                    )
                ]
        finally:
            await client.close()

    assert opaque_secret not in str(failure.value)
    import traceback

    assert opaque_secret not in "".join(traceback.format_exception(failure.value))
