"""OpenAI GPT provider adapter for Ash."""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator
import openai  # type: ignore[import-not-found]

from context.tokens import OpenAITokenCounter
from providers.base import ProviderABC, StreamChunk, TokenCounterLike


class _PartialToolCall:
    """Accumulates a streaming tool call's name + arguments until complete."""

    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name
        self.arguments = ""


def prepare_openai_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Translate Ash's canonical tool-call history to OpenAI chat messages."""

    prepared: list[dict[str, Any]] = []
    for message in messages:
        item = {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "name", "tool_call_id"}
        }
        canonical_calls = message.get("tool_calls")
        if canonical_calls:
            item["tool_calls"] = [
                {
                    "id": call["call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments", {})),
                    },
                }
                for call in canonical_calls
            ]
        prepared.append(item)
    return prepared


class OpenAIProvider(ProviderABC):
    provider_family = "openai"
    def __init__(
        self,
        model_name: str = "gpt-4o",
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "OpenAI API key is required. "
                "Set the OPENAI_API_KEY environment variable or pass api_key."
            )
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": prepare_openai_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if hasattr(self, "_max_tokens"):
            kwargs["max_tokens"] = self._max_tokens
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"OpenAI API error: {exc}") from exc

        # Buffer for accumulating streaming tool calls.
        partials: dict[int, _PartialToolCall] = {}
        # Completed native tool calls ready to emit.
        completed: list[dict[str, Any]] = []

        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            is_done = chunk.choices[0].finish_reason is not None
            prompt_tokens = 0
            completion_tokens = 0
            stop_reason = None

            # Process native tool_calls from the delta.
            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in partials:
                        # New tool call — capture name immediately, buffer args.
                        partials[idx] = _PartialToolCall(
                            id=tc.id or f"call_{idx}",
                            name=tc.function.name or "",
                        )
                    partial = partials[idx]
                    if tc.function.arguments:
                        partial.arguments += tc.function.arguments

            # On terminal chunk, finalise every partial tool call.
            if is_done:
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                stop_reason = chunk.choices[0].finish_reason
                for partial in partials.values():
                    completed.append(
                        {
                            "id": partial.id,
                            "name": partial.name,
                            "arguments": partial.arguments,
                        }
                    )
                partials.clear()

            yield StreamChunk(
                content=content,
                is_done=is_done,
                model=self._model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                stop_reason=stop_reason,
                # Surface complete native tool calls so the loop can use their
                # real IDs instead of generating random UUIDs.
                # Yield a COPY so completed.clear() after yield doesn't affect
                # the StreamChunk's reference.
                native_tool_calls=list(completed) if completed else None,
            )
            # Clear emitted calls so they are not yielded again.
            completed.clear()

    async def aclose(self) -> None:
        await self._client.close()
