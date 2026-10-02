"""DeepSeek chat completion provider for Ash.

API compatible with OpenAI SDK via custom base URL.
Base URL: https://api.deepseek.com/v1
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.base import (
    ProviderABC,
    StreamChunk,
    TokenCounterLike,
    managed_async_stream,
)
from ash.providers.capabilities import ProviderCapabilities, deepseek_capabilities
from ash.providers.messages import CanonicalToolCall, MessageInput
from ash.providers.openai import (
    _owned_openai_http_client,
    account_openai_compatible_stream_bytes,
    openai_compatible_cache_read_tokens,
    openai_compatible_reasoning_delta,
    prepare_openai_messages,
)
from ash.providers.replay_state import (
    ProviderReplayStateCipher,
    ProviderReplayStateError,
)
from ash.providers.readiness import (
    normalize_provider_base_url,
    redact_provider_error,
    require_secure_provider_transport,
)


class DeepSeekProvider(ProviderABC):
    provider_family = "deepseek"

    def __init__(
        self,
        model_name: str = "deepseek-flash",
        api_key: str = "",
        *,
        base_url: str | None = None,
        token_counter: TokenCounterLike | None = None,
        replay_state_directory: Path | None = None,
        replay_state_trusted_root: Path | None = None,
        replay_state_cipher: ProviderReplayStateCipher | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "DeepSeek API key is required. "
                "Set the DEEPSEEK_API_KEY environment variable or pass api_key."
            )
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = normalize_provider_base_url(
            base_url or "https://api.deepseek.com/v1", provider="deepseek"
        )
        require_secure_provider_transport(self._base_url, provider="deepseek")
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._client: Any | None = None
        self._replay_state_cipher: ProviderReplayStateCipher | None
        if replay_state_cipher is not None:
            self._replay_state_cipher = replay_state_cipher
        elif replay_state_directory is not None:
            trusted_root = replay_state_trusted_root or replay_state_directory.parent
            self._replay_state_cipher = ProviderReplayStateCipher(
                replay_state_directory,
                trusted_root=trusted_root,
            )
        else:
            self._replay_state_cipher = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return deepseek_capabilities(self._model_name)

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=0,
                http_client=_owned_openai_http_client(),
            )
        return self._client

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        def replay_fields(message: Any) -> dict[str, Any]:
            state_items = message.get("provider_state") or []
            matching = [
                item
                for item in state_items
                if isinstance(item, dict)
                and item.get("type") == "sealed_provider_state"
                and item.get("provider") == "deepseek"
                and item.get("kind") == "reasoning_content"
            ]
            if not matching:
                return {}
            if len(matching) != 1 or self._replay_state_cipher is None:
                raise ProviderReplayStateError(
                    "DeepSeek reasoning replay state is unavailable"
                )
            return {
                "reasoning_content": self._replay_state_cipher.open(
                    matching[0],
                    provider="deepseek",
                    kind="reasoning_content",
                )
            }

        try:
            prepared_messages = prepare_openai_messages(
                messages,
                assistant_state_fields=replay_fields,
            )
        except ProviderReplayStateError as exc:
            raise RuntimeError(
                "DeepSeek reasoning replay could not be recovered safely"
            ) from exc
        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": prepared_messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if hasattr(self, "_max_tokens"):
            kwargs["max_tokens"] = self._max_tokens
        try:
            client = self._resolve_client()
            stream = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            detail = redact_provider_error(str(exc), self._api_key)
            raise RuntimeError(f"DeepSeek API error: {detail}") from exc

        partials: dict[int, Any] = {}
        completed: list[CanonicalToolCall] = []
        stream_bytes = 0
        reasoning_parts: list[str] = []

        async with managed_async_stream(stream, label='DeepSeek') as managed_stream:
            async for chunk in managed_stream:
                    choices = getattr(chunk, "choices", None) or []
                    usage = getattr(chunk, "usage", None)
                    if not choices:
                        if usage is not None:
                            yield StreamChunk(
                                is_done=True,
                                model=self._model_name,
                                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                completion_tokens=(
                                    getattr(usage, "completion_tokens", 0) or 0
                                ),
                                cache_read_tokens=openai_compatible_cache_read_tokens(usage),
                                usage_source="provider",
                            )
                        continue

                    choice = choices[0]
                    delta = choice.delta
                    content = delta.content or ""
                    stream_bytes = account_openai_compatible_stream_bytes(
                        stream_bytes, content, provider="DeepSeek"
                    )
                    reasoning_delta = openai_compatible_reasoning_delta(
                        delta, provider="DeepSeek"
                    )
                    if reasoning_delta:
                        stream_bytes = account_openai_compatible_stream_bytes(
                            stream_bytes, reasoning_delta, provider="DeepSeek"
                        )
                        reasoning_parts.append(reasoning_delta)
                    is_done = choice.finish_reason is not None
                    prompt_tokens = 0
                    completion_tokens = 0
                    cache_read_tokens = 0
                    stop_reason = None

                    if hasattr(delta, "tool_calls") and delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in partials:
                                partials[idx] = {
                                    "id": tc.id or f"call_{idx}",
                                    "name": tc.function.name or "",
                                    "arguments": "",
                                }
                                stream_bytes = account_openai_compatible_stream_bytes(
                                    stream_bytes, partials[idx]["id"], provider="DeepSeek"
                                )
                                stream_bytes = account_openai_compatible_stream_bytes(
                                    stream_bytes, partials[idx]["name"], provider="DeepSeek"
                                )
                            if tc.function.arguments:
                                stream_bytes = account_openai_compatible_stream_bytes(
                                    stream_bytes,
                                    tc.function.arguments,
                                    provider="DeepSeek",
                                )
                                partials[idx]["arguments"] += tc.function.arguments

                    if is_done:
                        if usage is not None:
                            prompt_tokens = usage.prompt_tokens or 0
                            completion_tokens = usage.completion_tokens or 0
                            cache_read_tokens = openai_compatible_cache_read_tokens(usage)
                        stop_reason = choice.finish_reason
                        for partial in partials.values():
                            completed.append(CanonicalToolCall.model_validate(partial))
                        partials.clear()
                        provider_state = None
                        if reasoning_parts:
                            if self._replay_state_cipher is None and tools:
                                raise RuntimeError(
                                    "DeepSeek tool use in thinking mode requires durable "
                                    "reasoning replay state"
                                )
                            if self._replay_state_cipher is not None:
                                try:
                                    provider_state = [
                                        self._replay_state_cipher.seal(
                                            provider="deepseek",
                                            kind="reasoning_content",
                                            text="".join(reasoning_parts),
                                        )
                                    ]
                                except ProviderReplayStateError as exc:
                                    raise RuntimeError(
                                        "DeepSeek reasoning replay could not be persisted safely"
                                    ) from exc
                    else:
                        provider_state = None

                    yield StreamChunk(
                        content=content,
                        is_done=is_done,
                        model=self._model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cache_read_tokens=cache_read_tokens,
                        usage_source=(
                            "provider"
                            if is_done and usage is not None
                            else "unavailable"
                        ),
                        stop_reason=stop_reason,
                        native_tool_calls=list(completed) if completed else None,
                        reasoning=(
                            [{"type": "thinking", "thinking": "".join(reasoning_parts)}]
                            if is_done and reasoning_parts
                            else None
                        ),
                        provider_state=provider_state,
                    )
                    completed.clear()
                    if is_done:
                        reasoning_parts.clear()

    async def aclose(self) -> None:
        client = self._client
        if client is not None:
            await client.close()
            if self._client is client:
                self._client = None
