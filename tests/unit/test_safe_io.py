from __future__ import annotations

import io

import pytest

from ash.safe_io import read_bounded_text


def test_bounded_text_reads_text_streams_and_preserves_utf8() -> None:
    assert read_bounded_text(io.StringIO("café"), 5, label="prompt") == "café"


def test_bounded_text_rejects_text_streams_over_byte_limit() -> None:
    with pytest.raises(ValueError, match="prompt exceeds 4 bytes"):
        read_bounded_text(io.StringIO("café"), 4, label="prompt")


def test_bounded_text_reads_binary_buffer_streams() -> None:
    class TextWrapper:
        def __init__(self, data: bytes) -> None:
            self.buffer = io.BytesIO(data)

    assert read_bounded_text(TextWrapper(b"hello"), 5, label="request") == "hello"


def test_bounded_text_rejects_invalid_utf8_from_binary_stream() -> None:
    class TextWrapper:
        def __init__(self) -> None:
            self.buffer = io.BytesIO(b"\xff")

    with pytest.raises(ValueError, match="request is not valid UTF-8"):
        read_bounded_text(TextWrapper(), 10, label="request")
