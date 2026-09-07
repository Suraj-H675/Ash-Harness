from __future__ import annotations

import io
from pathlib import Path

import pytest

from ash.safe_io import read_bounded_text, strict_json_loads, validate_unlinked_path


def test_strict_json_loads_accepts_standard_json() -> None:
    assert strict_json_loads(b'{"name":"ash","enabled":true}') == {
        "name": "ash",
        "enabled": True,
    }


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_strict_json_loads_rejects_ambiguous_or_nonstandard_json(payload: str) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(payload)


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


def test_validate_unlinked_path_rejects_link_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink or junction"):
        validate_unlinked_path(
            linked / "state.json",
            trusted_root=tmp_path,
            label="test state",
        )
