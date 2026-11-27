"""Token counting adapters for Ash provider models."""

from __future__ import annotations

import os
from typing import Protocol


CHARS_PER_TOKEN_HEURISTIC = 4
ASCII_CHARS_PER_TOKEN_ESTIMATE = 3
DEFAULT_OPENAI_FALLBACK_ENCODING = "cl100k_base"
_NON_ASCII_BYTES = bytes(range(128, 256))


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
        self._encoder: object | None = None
        if os.environ.get("ASH_ENABLE_TIKTOKEN_DOWNLOAD") == "1":
            try:
                import tiktoken  # type: ignore[import-not-found]

                self._encoder = tiktoken.get_encoding(DEFAULT_OPENAI_FALLBACK_ENCODING)
                self.using_approximation = False
            except Exception:
                # Token usage returned by the provider remains authoritative.
                self._encoder = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoder is not None:
            return len(self._encoder.encode(text))  # type: ignore[attr-defined]
        return estimate_text_tokens(text)


def token_count_mode(counter: object) -> str:
    """Name only built-in counter modes whose implementation is known."""

    if type(counter) is AnthropicTokenCounter:
        return "chars_per_token_4"
    if type(counter) is OpenAITokenCounter:
        if counter._encoder is not None:
            return f"tiktoken_{DEFAULT_OPENAI_FALLBACK_ENCODING}"
        if counter.using_approximation:
            return "offline_text_heuristic"
    return "unknown"


def estimate_text_tokens(text: str) -> int:
    """Return a conservative offline estimate for mixed code and Unicode text.

    ASCII-heavy text is estimated at three characters per token rather than the
    common English prose average of four. Non-ASCII input is charged by UTF-8
    byte length so CJK, emoji, and other multi-byte text cannot collapse into a
    severe character-count underestimate.
    """

    # str.isascii() uses the string's native representation; ordinary source
    # code and serialized ASCII history need neither a per-character scan nor
    # a UTF-8 allocation. In mixed text, each ASCII character is exactly one
    # byte, and every byte of a non-ASCII UTF-8 sequence is >= 128.
    if text.isascii():
        return (len(text) + ASCII_CHARS_PER_TOKEN_ESTIMATE - 1) // ASCII_CHARS_PER_TOKEN_ESTIMATE
    encoded = text.encode("utf-8")
    ascii_chars = len(encoded.translate(None, _NON_ASCII_BYTES))
    non_ascii_bytes = len(encoded) - ascii_chars
    ascii_tokens = (
        ascii_chars + ASCII_CHARS_PER_TOKEN_ESTIMATE - 1
    ) // ASCII_CHARS_PER_TOKEN_ESTIMATE
    return ascii_tokens + non_ascii_bytes


def get_token_counter(provider: str, model_name: str) -> TokenCounter:
    """Return the token counter adapter for the given provider/model pair."""

    if provider == "anthropic":
        return AnthropicTokenCounter()
    if provider == "openai":
        return OpenAITokenCounter(model_name)
    if provider == "ollama":
        return AnthropicTokenCounter()
    raise ValueError(f"Unsupported provider: {provider}")
