"""OpenAI Responses adapter backed by an eligible ChatGPT plan."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import (
    ProviderABC,
    ProviderTerminalError,
    StreamChunk,
    TokenCounterLike,
)
from ash.providers.messages import MessageInput
from ash.providers.openai import _owned_openai_http_client
from ash.providers.openai_chatgpt_auth import (
    CHATGPT_RESOURCE,
    ChatGPTAuthSession,
)
from ash.providers.openai_responses import (
    prepare_openai_responses_input,
    stream_openai_responses,
)


_RETRYABLE_CHATGPT_RESPONSE_FAILURES = frozenset(
    {
        "subscription_sharing_usage_unavailable",
        "subscription_sharing_user_unavailable",
    }
)


class ChatGPTResponsesTerminalError(ProviderTerminalError):
    """Responses failure with preview-specific safe retry classification."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.retriable = code in _RETRYABLE_CHATGPT_RESPONSE_FAILURES


prepare_chatgpt_responses_input = prepare_openai_responses_input


class OpenAIChatGPTProvider(ProviderABC):
    """Stateless Responses provider using OAuth plan credentials."""

    provider_family = "openai"

    def __init__(
        self,
        model_name: str,
        *,
        auth_session: ChatGPTAuthSession | None = None,
        token_counter: TokenCounterLike | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not model_name.strip():
            raise ValueError("ChatGPT plan model name cannot be empty")
        self._model_name = model_name
        self._auth_session = auth_session or ChatGPTAuthSession()
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client_factory = client_factory

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        # ChatGPT-plan preview currently rejects max_output_tokens. Retain
        # validation because the Ash loop calls this provider-neutral hook.
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")

    def configure_prompt_cache(
        self,
        *,
        enabled: bool,
        cache_key: str = "",
        retention: str = "memory",
    ) -> None:
        del enabled, cache_key
        if retention not in {"memory", "extended"}:
            raise ValueError("prompt cache retention must be memory or extended")

    def _new_client(self, access_token: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(access_token)
        return openai.AsyncOpenAI(
            api_key=access_token,
            base_url=CHATGPT_RESOURCE,
            max_retries=0,
            http_client=_owned_openai_http_client(),
        )

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del temperature
        access_token = await self._auth_session.access_token()
        client = self._new_client(access_token)
        owns_client = self._client_factory is None
        request_options: dict[str, Any] = {}
        if self.configured_reasoning_effort is not None:
            request_options["reasoning"] = {
                "effort": self.configured_reasoning_effort
            }
        try:
            async for chunk in stream_openai_responses(
                client,
                model_name=self._model_name,
                messages=messages,
                tools=tools,
                request_options=request_options,
                error_label="OpenAI ChatGPT Responses",
                error_secrets=(access_token,),
                terminal_error_factory=ChatGPTResponsesTerminalError,
            ):
                yield chunk
        finally:
            if owns_client:
                close = getattr(client, "close", None)
                aclose = getattr(client, "aclose", None)
                if callable(aclose):
                    await aclose()
                elif callable(close):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
