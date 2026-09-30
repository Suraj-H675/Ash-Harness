from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

import ash.memory.sqlite_index as memory_index_module
from ash.context.compaction import Chunk
from ash.memory.sqlite_index import (
    MemoryDocument,
    MemoryIndexError,
    SQLiteMemoryIndex,
)
from ash.sqlite_utils import preferred_sqlite_journal_mode


def _chunk(path: str, content: str, *, start: int = 1, end: int = 1) -> Chunk:
    return Chunk(
        file_path=path,
        start_line=start,
        end_line=end,
        content=content,
    )


def _index(tmp_path: Path, *, chunking_version: str = "lines-30-overlap-5-v1") -> SQLiteMemoryIndex:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SQLiteMemoryIndex(
        tmp_path / "state" / "memory.db",
        workspace_root=workspace,
        chunking_version=chunking_version,
    )


def test_sqlite_memory_index_persists_lexical_rows(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="notes.py",
                chunks=[_chunk("/absolute/notes.py", "unique lexical marker")],
            )
        ]
    )

    reopened = SQLiteMemoryIndex(
        index.db_path,
        workspace_root=tmp_path / "workspace",
        chunking_version="lines-30-overlap-5-v1",
    )
    hits = reopened.lexical_search("unique lexical marker", limit=5)

    assert [hit.file_path for hit in hits] == ["notes.py"]
    assert hits[0].chunk_key == "notes.py:1-1"
    assert hits[0].content == "unique lexical marker"


def test_sqlite_memory_index_persists_and_ranks_vectors(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assert index.configure_embedding("test/model@v1", 3) is False
    index.replace_documents(
        [
            MemoryDocument(
                file_path="a.py",
                chunks=[_chunk("a.py", "alpha")],
                embeddings=[[1.0, 0.0, 0.0]],
            ),
            MemoryDocument(
                file_path="b.py",
                chunks=[_chunk("b.py", "beta")],
                embeddings=[[0.0, 1.0, 0.0]],
            ),
        ]
    )

    reopened = SQLiteMemoryIndex(
        index.db_path,
        workspace_root=tmp_path / "workspace",
        chunking_version="lines-30-overlap-5-v1",
    )
    hits = reopened.vector_search([0.9, 0.1, 0.0], limit=2)

    assert [hit.file_path for hit in hits] == ["a.py", "b.py"]
    assert hits[0].score > hits[1].score


def test_embedding_identity_change_drops_vectors_but_keeps_lexical_memory(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    index.configure_embedding("provider/model-a@v1", 2)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="memory.py",
                chunks=[_chunk("memory.py", "retain lexical evidence")],
                embeddings=[[1.0, 0.0]],
            )
        ]
    )

    assert index.configure_embedding("provider/model-b@v1", 3) is True

    assert index.vector_search([1.0, 0.0, 0.0], limit=5) == []
    hits = index.lexical_search("retain lexical evidence", limit=5)
    assert [hit.file_path for hit in hits] == ["memory.py"]


def test_document_replace_serializes_with_embedding_reconfiguration(
    tmp_path: Path,
) -> None:
    first = _index(tmp_path)
    second = SQLiteMemoryIndex(
        first.db_path,
        workspace_root=tmp_path / "workspace",
        chunking_version="lines-30-overlap-5-v1",
    )
    first.configure_embedding("provider/model-a@v1", 2)
    prepared = threading.Event()
    release = threading.Event()
    original_prepare = first._prepare_document

    def pause_after_prepare(document, *, expected_dimension):
        result = original_prepare(
            document,
            expected_dimension=expected_dimension,
        )
        prepared.set()
        assert release.wait(5)
        return result

    first._prepare_document = pause_after_prepare  # type: ignore[method-assign]
    document = MemoryDocument(
        file_path="race.py",
        chunks=[_chunk("race.py", "serialized generation")],
        embeddings=[[1.0, 0.0]],
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement = pool.submit(first.replace_documents, [document])
        assert prepared.wait(5)
        reconfiguration = pool.submit(
            second.configure_embedding,
            "provider/model-b@v1",
            3,
        )
        try:
            with pytest.raises(FutureTimeoutError):
                reconfiguration.result(timeout=0.1)
        finally:
            release.set()
        assert replacement.result(timeout=5) == 1
        assert reconfiguration.result(timeout=5) is True

    assert first.vector_search([1.0, 0.0, 0.0], limit=5) == []
    assert [
        hit.file_path
        for hit in first.lexical_search("serialized generation", limit=5)
    ] == ["race.py"]


def test_vector_search_reads_one_embedding_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if preferred_sqlite_journal_mode() != "WAL":
        pytest.skip(
            "snapshot-with-concurrent-writer behavior requires safe SQLite WAL mode"
        )
    reader = _index(tmp_path)
    writer = SQLiteMemoryIndex(
        reader.db_path,
        workspace_root=tmp_path / "workspace",
        chunking_version="lines-30-overlap-5-v1",
    )
    reader.configure_embedding("provider/model-a@v1", 2)
    reader.replace_documents(
        [
            MemoryDocument(
                file_path="old.py",
                chunks=[_chunk("old.py", "old generation")],
                embeddings=[[1.0, 0.0]],
            )
        ]
    )
    dimension_read = threading.Event()
    release = threading.Event()
    first_read = True
    real_read_dimension = memory_index_module._read_embedding_dimension

    def pause_first_dimension_read(connection: sqlite3.Connection) -> int | None:
        nonlocal first_read
        value = real_read_dimension(connection)
        if first_read:
            first_read = False
            dimension_read.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(
        memory_index_module,
        "_read_embedding_dimension",
        pause_first_dimension_read,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        search = pool.submit(reader.vector_search, [1.0, 0.0], limit=5)
        assert dimension_read.wait(5)
        writer.configure_embedding("provider/model-b@v1", 3)
        writer.replace_documents(
            [
                MemoryDocument(
                    file_path="new.py",
                    chunks=[_chunk("new.py", "new generation")],
                    embeddings=[[1.0, 0.0, 0.0]],
                )
            ]
        )
        release.set()
        hits = search.result(timeout=5)

    assert [hit.file_path for hit in hits] == ["old.py"]
    assert [
        hit.file_path for hit in reader.vector_search([1.0, 0.0, 0.0], limit=5)
    ] == ["new.py"]


def test_failed_replacement_preserves_previous_document_generation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.configure_embedding("test/model@v1", 2)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="stable.py",
                chunks=[_chunk("stable.py", "old generation marker")],
                embeddings=[[1.0, 0.0]],
            )
        ]
    )

    with pytest.raises(ValueError, match="embedding dimension"):
        index.replace_documents(
            [
                MemoryDocument(
                    file_path="stable.py",
                    chunks=[_chunk("stable.py", "new generation marker")],
                    embeddings=[[1.0, 0.0, 0.0]],
                )
            ]
        )

    old_hits = index.lexical_search("old generation marker", limit=5)
    assert [hit.file_path for hit in old_hits] == ["stable.py"]
    assert [record["content"] for record in index.export_records(limit=10)] == [
        "old generation marker"
    ]
    assert [hit.file_path for hit in index.vector_search([1.0, 0.0], limit=5)] == [
        "stable.py"
    ]


def test_lexical_search_uses_and_first_then_relaxes_to_or(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="exact.py",
                chunks=[_chunk("exact.py", "alpha beta exact")],
            ),
            MemoryDocument(
                file_path="partial.py",
                chunks=[_chunk("partial.py", "alpha partial")],
            ),
        ]
    )

    strict = index.lexical_search("alpha beta", limit=5)
    relaxed = index.lexical_search("alpha missing", limit=5)

    assert [hit.file_path for hit in strict] == ["exact.py"]
    assert {hit.file_path for hit in relaxed} == {"exact.py", "partial.py"}


@pytest.mark.parametrize("limit", [0, 101])
def test_memory_search_result_limit_is_bounded(tmp_path: Path, limit: int) -> None:
    index = _index(tmp_path)

    with pytest.raises(ValueError, match="memory search limit must be between 1 and 100"):
        index.lexical_search("needle", limit=limit)
    with pytest.raises(ValueError, match="memory search limit must be between 1 and 100"):
        index.vector_search([1.0, 0.0], limit=limit)


def test_empty_replacement_forgets_document_across_all_indices(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.configure_embedding("test/model@v1", 2)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="remove.py",
                chunks=[_chunk("remove.py", "remove me")],
                embeddings=[[1.0, 0.0]],
            )
        ]
    )

    index.replace_documents([MemoryDocument(file_path="remove.py", chunks=[])])

    assert index.document_paths() == set()
    assert index.lexical_search("remove me", limit=5) == []
    assert index.vector_search([1.0, 0.0], limit=5) == []


def test_chunking_identity_change_rebuilds_derived_memory(tmp_path: Path) -> None:
    index = _index(tmp_path, chunking_version="chunker-v1")
    index.replace_documents(
        [
            MemoryDocument(
                file_path="old.py",
                chunks=[_chunk("old.py", "old chunking marker")],
            )
        ]
    )

    reopened = SQLiteMemoryIndex(
        index.db_path,
        workspace_root=tmp_path / "workspace",
        chunking_version="chunker-v2",
    )

    assert reopened.document_paths() == set()
    assert reopened.lexical_search("old chunking marker", limit=5) == []


def test_workspace_identity_change_rebuilds_derived_memory(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    database = tmp_path / "state" / "memory.db"
    index = SQLiteMemoryIndex(
        database,
        workspace_root=first,
        chunking_version="chunker-v1",
    )
    index.replace_documents(
        [MemoryDocument(file_path="secret.py", chunks=[_chunk("secret.py", "first")])]
    )

    reopened = SQLiteMemoryIndex(
        database,
        workspace_root=second,
        chunking_version="chunker-v1",
    )

    assert reopened.document_paths() == set()


def test_parent_directory_substitution_is_rejected_without_touching_replacement(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    state = index.db_path.parent
    saved = tmp_path / "saved-state"
    replacement = tmp_path / "replacement-state"
    replacement.mkdir()
    state.rename(saved)
    replacement.rename(state)

    with pytest.raises(MemoryIndexError, match="identity"):
        index.replace_documents(
            [MemoryDocument(file_path="evil.py", chunks=[_chunk("evil.py", "evil")])]
        )

    assert list(state.iterdir()) == []
    original = SQLiteMemoryIndex(
        saved / "memory.db",
        workspace_root=tmp_path / "workspace",
        chunking_version="lines-30-overlap-5-v1",
    )
    assert original.document_paths() == set()


def test_database_file_substitution_is_rejected(tmp_path: Path) -> None:
    index = _index(tmp_path)
    original = index.db_path
    saved = original.with_name("memory-original.db")
    original.rename(saved)
    sqlite3.connect(original).close()

    with pytest.raises(MemoryIndexError, match="identity changed"):
        index.document_paths()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not authoritative")
def test_memory_sqlite_files_are_private(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.replace_documents(
        [MemoryDocument(file_path="private.py", chunks=[_chunk("private.py", "private")])]
    )

    assert stat.S_IMODE(index.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(index.db_path.parent.stat().st_mode) == 0o700
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{index.db_path}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_corrupt_vector_blob_fails_closed_without_losing_lexical_rows(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    index.configure_embedding("test/model@v1", 2)
    index.replace_documents(
        [
            MemoryDocument(
                file_path="corrupt.py",
                chunks=[_chunk("corrupt.py", "lexical survives")],
                embeddings=[[1.0, 0.0]],
            )
        ]
    )
    with sqlite3.connect(index.db_path) as connection:
        connection.execute(
            "UPDATE memory_chunks SET embedding=? WHERE file_path=?",
            (b"bad", "corrupt.py"),
        )

    with pytest.raises(MemoryIndexError, match="stored embedding"):
        index.vector_search([1.0, 0.0], limit=5)
    assert [
        hit.file_path for hit in index.lexical_search("lexical survives", limit=5)
    ] == ["corrupt.py"]
