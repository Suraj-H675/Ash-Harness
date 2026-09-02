from __future__ import annotations

import pytest

from ash.memory.markdown_store import (
    MAX_MEMORY_CONTENT_BYTES,
    MarkdownMemoryStore,
)


def test_markdown_memory_store_round_trips_and_sorts_keys(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")

    store.save("z-last", "café")
    store.save("a-first", "remember this")

    assert store.load("z-last") == "café"
    assert store.load("missing") is None
    assert store.list_keys() == ["a-first", "z-last"]


@pytest.mark.parametrize("key", ["../escape", "nested/key", "", ".", ".."])
def test_markdown_memory_store_rejects_unsafe_keys(tmp_path, key: str) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")

    with pytest.raises(ValueError, match="safe filename component"):
        store.save(key, "content")
    with pytest.raises(ValueError, match="safe filename component"):
        store.load(key)


def test_markdown_memory_store_rejects_oversized_content(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")

    with pytest.raises(ValueError, match="memory content exceeds"):
        store.save("large", "x" * (MAX_MEMORY_CONTENT_BYTES + 1))


def test_markdown_memory_store_does_not_follow_file_symlinks(tmp_path) -> None:
    memory_dir = tmp_path / "memory"
    store = MarkdownMemoryStore(memory_dir)
    target = tmp_path / "private.txt"
    target.write_text("private", encoding="utf-8")
    linked = memory_dir / "linked.md"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlinked memory file"):
        store.load("linked")

    with pytest.raises(ValueError, match="memory file cannot be a symlink"):
        store.save("linked", "replacement")


def test_markdown_memory_store_bounds_key_inventory(tmp_path, monkeypatch) -> None:
    store = MarkdownMemoryStore(tmp_path / "memory")
    monkeypatch.setattr(
        "ash.memory.markdown_store.MAX_MEMORY_KEYS",
        2,
    )
    for key in ("one", "two", "three"):
        store.save(key, key)

    with pytest.raises(ValueError, match="exceeds 2 files"):
        store.list_keys()
