"""Token counting adapters for Ash provider models."""

from __future__ import annotations

from typing import Protocol

import tiktoken


CHARS_PER_TOKEN_HEURISTIC = 4
DEFAULT_OPENAI_FALLBACK_ENCODING = "cl100k_base"


class TokenCounter(Protocol):
    """Strategy for counting tokens in a text string."""

    def count(self, text: str) -> int:
        """Return the number of tokens the text consumes."""


class AnthropicTokenCounter:
    """
    Approximate token counter for Anthropic Claude models.

    Anthropic does not publish an exact public tokenizer, and exact counts
    are sourced from response headers at runtime. Until those headers are
    wired through the provider stream, fall back to a character heuristic
    (~4 characters per token) as a conservative upper-bound estimate.
    """

    CHARS_PER_TOKEN = CHARS_PER_TOKEN_HEURISTIC

    def count(self, text: str) -> int:
        if not text:
            return 0
        return (len(text) + self.CHARS_PER_TOKEN - 1) // self.CHARS_PER_TOKEN


class OpenAITokenCounter:
    """Exact token counter for OpenAI models via tiktoken."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        # Use cl100k_base directly to avoid hanging on remote model downloads.
        # This encoding is used by gpt-4o, gpt-4-turbo, gpt-3.5-turbo, etc.
        self._encoder = tiktoken.get_encoding(DEFAULT_OPENAI_FALLBACK_ENCODING)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoder.encode(text))


def get_token_counter(provider: str, model_name: str) -> TokenCounter:
    """Return the token counter adapter for the given provider/model pair."""

    if provider == "anthropic":
        return AnthropicTokenCounter()
    if provider == "openai":
        return OpenAITokenCounter(model_name)
    if provider == "ollama":
        return AnthropicTokenCounter()
    raise ValueError(f"Unsupported provider: {provider}")
