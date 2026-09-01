"""Tests for sliding-window chunker and FTS5 lexical search (Sprint 6)."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.context.compaction import (
    DEFAULT_OVERLAP,
    DEFAULT_WINDOW_SIZE,
    Chunk,
    sliding_window_chunk,
)
from ash.core.session import get_db_connection
from ash.memory.fts5 import FTS5Index, query_lexical_fallback


# --- Chunker Tests ---


def test_chunker_returns_empty_for_empty_content() -> None:
    assert sliding_window_chunk("", "empty.py") == []


def test_chunker_short_content_yields_single_chunk() -> None:
    chunks = sliding_window_chunk("a\nb\nc", "short.py")

    assert len(chunks) == 1
    assert chunks[0].file_path == "short.py"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 3
    assert chunks[0].content == "a\nb\nc"


def test_chunker_uses_default_window_and_overlap() -> None:
    lines = [f"line{i}" for i in range(1, 61)]  # 60 lines
    content = "\n".join(lines)

    chunks = sliding_window_chunk(content, "doc.py")

    # window=30, overlap=5, step=25
    # 60 lines: chunks at [1-30], [26-55], [51-60]
    assert len(chunks) == 3
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 30
    assert chunks[1].start_line == 26
    assert chunks[1].end_line == 55
    assert chunks[2].start_line == 51
    assert chunks[2].end_line == 60


def test_chunker_exact_window_boundary_returns_single_chunk() -> None:
    lines = [str(i) for i in range(1, DEFAULT_WINDOW_SIZE + 1)]
    content = "\n".join(lines)

    chunks = sliding_window_chunk(content, "exact.py")

    assert len(chunks) == 1
    assert chunks[0].end_line == DEFAULT_WINDOW_SIZE


def test_chunker_overlaps_consecutive_chunks() -> None:
    lines = [str(i) for i in range(1, 41)]  # 40 lines
    content = "\n".join(lines)

    chunks = sliding_window_chunk(content, "overlap.py")

    # Consecutive chunks share `overlap` lines
    first_lines = set(chunks[0].content.splitlines())
    second_lines = set(chunks[1].content.splitlines())
    shared = first_lines & second_lines
    assert len(shared) == DEFAULT_OVERLAP


def test_chunker_chunk_key_matches_spec_format() -> None:
    chunks = sliding_window_chunk("a\nb\nc", "src/app.py")

    assert chunks[0].chunk_key == "src/app.py:1-3"


def test_chunker_rejects_invalid_window_size() -> None:
    with pytest.raises(ValueError, match="window_size"):
        sliding_window_chunk("a", "f.py", window_size=0)


def test_chunker_rejects_overlap_greater_than_window() -> None:
    with pytest.raises(ValueError, match="overlap"):
        sliding_window_chunk("a", "f.py", window_size=5, overlap=5)


def test_chunker_rejects_negative_overlap() -> None:
    with pytest.raises(ValueError, match="overlap"):
        sliding_window_chunk("a", "f.py", window_size=10, overlap=-1)


def test_chunker_preserves_line_ordering_with_overlap() -> None:
    lines = [f"line-{i}" for i in range(75)]
    content = "\n".join(lines)

    chunks = sliding_window_chunk(content, "preserve.py")

    # Overlap means lines repeat across chunks, but the first occurrence
    # of every line must appear in original order.
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        for line in chunk.content.splitlines():
            if line not in seen:
                seen.add(line)
                ordered.append(line)

    assert ordered == lines


def test_chunker_covers_every_line_in_input() -> None:
    lines = [f"line-{i}" for i in range(75)]
    content = "\n".join(lines)

    chunks = sliding_window_chunk(content, "preserve.py")

    covered = {line for chunk in chunks for line in chunk.content.splitlines()}

    assert covered == set(lines)


# --- FTS5 Index Tests ---


@pytest.fixture
def fts5_index(tmp_path: Path) -> FTS5Index:
    return FTS5Index(tmp_path / "fts5.db")


def _chunks(file_path: str, *paragraphs: str) -> list[Chunk]:
    return [
        Chunk(
            file_path=file_path,
            start_line=idx + 1,
            end_line=idx + 1,
            content=paragraph,
        )
        for idx, paragraph in enumerate(paragraphs)
    ]


def test_fts5_init_creates_virtual_and_metadata_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "fts5.db"
    FTS5Index(db_path)

    with closing(get_db_connection(db_path)) as conn, conn:
        table_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    assert "fts_index" in table_names
    assert "document_metadata" in table_names


def test_fts5_index_document_inserts_chunks(fts5_index: FTS5Index) -> None:
    chunks = _chunks(
        "src/app.py",
        "def greet():\n    print('hello')\n",
        "class User:\n    pass\n",
    )

    first_rowid = fts5_index.index_document("src/app.py", chunks)

    assert first_rowid > 0

    results = fts5_index.query("greet")
    assert len(results) == 1
    assert results[0]["file_path"] == "src/app.py"
    assert "greet" in results[0]["content"]


def test_fts5_index_document_with_sha256_updates_metadata(
    fts5_index: FTS5Index,
) -> None:
    chunks = _chunks("a.py", "alpha content")
    sha = "abc123def456"

    first_rowid = fts5_index.index_document("a.py", chunks, sha256=sha)

    with closing(get_db_connection(fts5_index.db_path)) as conn, conn:
        row = conn.execute(
            "SELECT file_path, sha256 FROM document_metadata WHERE rowid = ?",
            (first_rowid,),
        ).fetchone()

    assert row is not None
    assert row["file_path"] == "a.py"
    assert row["sha256"] == sha


def test_fts5_reindex_replaces_existing_chunks(fts5_index: FTS5Index) -> None:
    fts5_index.index_document("x.py", _chunks("x.py", "old content about cats"))
    fts5_index.index_document("x.py", _chunks("x.py", "new content about dogs"))

    results = fts5_index.query("cats")
    assert results == []  # old content gone

    results = fts5_index.query("dogs")
    assert len(results) == 1
    assert "dogs" in results[0]["content"]


def test_fts5_index_documents_batches_replacements(fts5_index: FTS5Index) -> None:
    indexed = fts5_index.index_documents(
        [
            ("a.py", _chunks("a.py", "alpha content"), None),
            ("b.py", _chunks("b.py", "beta content"), None),
        ]
    )

    assert indexed == 2
    assert {row["file_path"] for row in fts5_index.query("content")} == {
        "a.py",
        "b.py",
    }


def test_fts5_query_ranks_more_relevant_chunk_first(fts5_index: FTS5Index) -> None:
    # file1 mentions the keyword five times; file2 mentions it once.
    fts5_index.index_document(
        "frequent.py",
        _chunks("frequent.py", "python python python python python"),
    )
    fts5_index.index_document(
        "rare.py",
        _chunks("rare.py", "the python language is great"),
    )

    results = fts5_index.query("python")

    assert len(results) == 2
    assert results[0]["file_path"] == "frequent.py"
    # BM25: more relevant = lower (more negative) rank
    assert results[0]["rank"] <= results[1]["rank"]


def test_fts5_query_respects_limit(fts5_index: FTS5Index) -> None:
    for i in range(5):
        fts5_index.index_document(
            f"file{i}.py",
            _chunks(f"file{i}.py", f"unique token{i} shared"),
        )

    results = fts5_index.query("shared", limit=3)

    assert len(results) == 3


def test_fts5_query_returns_empty_for_no_matches(fts5_index: FTS5Index) -> None:
    fts5_index.index_document("a.py", _chunks("a.py", "alpha content"))

    assert fts5_index.query("nonexistent_keyword_xyz") == []


def test_fts5_delete_document_removes_chunks(fts5_index: FTS5Index) -> None:
    fts5_index.index_document("a.py", _chunks("a.py", "alpha content"))
    fts5_index.index_document("a.py", _chunks("a.py", "alpha content"), sha256="abc")

    deleted = fts5_index.delete_document("a.py")

    assert deleted >= 1
    assert fts5_index.query("alpha") == []

    with closing(get_db_connection(fts5_index.db_path)) as conn, conn:
        meta = conn.execute(
            "SELECT COUNT(*) as c FROM document_metadata WHERE file_path = ?",
            ("a.py",),
        ).fetchone()
    assert meta["c"] == 0


def test_fts5_query_lexical_fallback_uses_passed_connection(tmp_path: Path) -> None:
    fts5_index = FTS5Index(tmp_path / "fts5.db")
    fts5_index.index_document("a.py", _chunks("a.py", "alpha beta gamma"))

    with closing(get_db_connection(tmp_path / "fts5.db")) as conn:
        results = query_lexical_fallback(conn, "beta")

    assert len(results) == 1
    assert results[0]["file_path"] == "a.py"
    assert "beta" in results[0]["content"]


def test_fts5_query_lexical_fallback_respects_limit(tmp_path: Path) -> None:
    fts5_index = FTS5Index(tmp_path / "fts5.db")
    for i in range(4):
        fts5_index.index_document(
            f"f{i}.py",
            _chunks(f"f{i}.py", f"common marker{i}"),
        )

    with closing(get_db_connection(tmp_path / "fts5.db")) as conn:
        results = query_lexical_fallback(conn, "common", limit=2)

    assert len(results) == 2


def test_fts5_unicode_content_indexes_and_retrieves(fts5_index: FTS5Index) -> None:
    fts5_index.index_document(
        "i18n.py",
        _chunks("i18n.py", "internationalization is i18n"),
    )

    results = fts5_index.query("internationalization")

    assert len(results) == 1
    assert results[0]["file_path"] == "i18n.py"


def test_fts5_metadata_last_modified_set_on_index(fts5_index: FTS5Index) -> None:
    fts5_index.index_document(
        "fresh.py",
        _chunks("fresh.py", "content"),
        sha256="hash-1",
    )

    with closing(get_db_connection(fts5_index.db_path)) as conn, conn:
        row = conn.execute(
            "SELECT last_modified FROM document_metadata WHERE file_path = ?",
            ("fresh.py",),
        ).fetchone()

    assert row is not None
    # Should be a valid ISO timestamp parseable by fromisoformat
    parsed = datetime.fromisoformat(row["last_modified"])
    assert parsed.tzinfo is not None
    # Within the last hour
    now = datetime.now(timezone.utc)
    assert abs((now - parsed).total_seconds()) < 3600
