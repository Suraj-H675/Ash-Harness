"""Token counting adapters for Ash provider models."""

from __future__ import annotations

import os
import re
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
    """OpenAI token counter with an offline-safe approximation fallback."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.using_approximation = True
        self._encoder: object = _ApproximateEncoder()
        if os.environ.get("ASH_ENABLE_TIKTOKEN_DOWNLOAD") == "1":
            try:
                self._encoder = tiktoken.get_encoding(DEFAULT_OPENAI_FALLBACK_ENCODING)
                self.using_approximation = False
            except Exception:
                # Token usage returned by the provider remains authoritative.
                self._encoder = _ApproximateEncoder()

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoder.encode(text))  # type: ignore[attr-defined]


class _ApproximateEncoder:
    """Small offline tokenizer used only for input-budget estimates."""

    _TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def encode(self, text: str) -> list[str]:
        return self._TOKEN_PATTERN.findall(text)


def get_token_counter(provider: str, model_name: str) -> TokenCounter:
    """Return the token counter adapter for the given provider/model pair."""

    if provider == "anthropic":
        return AnthropicTokenCounter()
    if provider == "openai":
        return OpenAITokenCounter(model_name)
    if provider == "ollama":
        return AnthropicTokenCounter()
    raise ValueError(f"Unsupported provider: {provider}")
