"""Transactional SQLite storage for Ash workspace memory."""

from __future__ import annotations

import heapq
import math
import os
import re
import sqlite3
import stat
import struct
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from ash.context.compaction import Chunk
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError


MEMORY_SCHEMA_VERSION = 1
EMBEDDING_FORMAT = "float32-le-v1"
MAX_QUERY_TERMS = 32
MAX_QUERY_TERM_CHARS = 128
MAX_VECTOR_SCAN_RECORDS = 100_000
MAX_DOCUMENTS = 10_000
MAX_MEMORY_SEARCH_RESULTS = 100


class MemoryIndexError(RuntimeError):
    """Raised when the durable memory index cannot be used safely."""


@dataclass(frozen=True)
class MemoryCandidate:
    """One lexical or vector retrieval candidate from the durable index."""

    chunk_key: str
    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float


@dataclass(frozen=True)
class MemoryDocument:
    """One document replacement prepared for atomic publication."""

    file_path: str
    chunks: Sequence[Chunk]
    embeddings: Sequence[Sequence[float]] | None = None
    sha256: str | None = None


class SQLiteMemoryIndex:
    """One SQLite store for lexical rows, vectors, and index identity.

    The database is derived state.  Every document replacement publishes its
    text, FTS rows, and embeddings in one SQLite transaction so search never
    observes a mixed generation for that document.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        workspace_root: str | Path,
        chunking_version: str,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.db_path = Path(db_path).expanduser().absolute()
        if not self.db_path.name:
            raise ValueError("memory database path must name a file")
        self._parent = self.db_path.parent
        self._name = self.db_path.name
        self._workspace = Path(workspace_root).expanduser().resolve()
        self._chunking_version = chunking_version
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self._parent_identity: os.stat_result
        self._database_identity: os.stat_result

        try:
            with AnchoredDirectory.open(
                self._parent,
                create=True,
                private=True,
                pin_path=True,
            ) as directory:
                self._parent_identity = os.fstat(directory.descriptor)
                with self._connect_in(directory, initialize=True) as connection:
                    self._initialize_schema(connection)
                    self._initialize_identity(connection)
                database = directory.stat(self._name)
                if database is None or not stat.S_ISREG(database.st_mode):
                    raise MemoryIndexError(
                        f"memory database is not a regular file: {self.db_path}"
                    )
                self._database_identity = database
                self._restrict_storage_permissions(directory)
        except (OSError, sqlite3.Error, AnchoredFilesystemError) as exc:
            if isinstance(exc, MemoryIndexError):
                raise
            raise MemoryIndexError(
                f"cannot initialize memory database {self.db_path}: {exc}"
            ) from exc

    @contextmanager
    def _directory(self) -> Iterator[AnchoredDirectory]:
        try:
            with AnchoredDirectory.open(
                self._parent,
                create=False,
                private=True,
                expected=self._parent_identity,
                pin_path=True,
            ) as directory:
                directory.validation_path()
                database = directory.stat(self._name)
                if database is None or not _same_identity(
                    database, self._database_identity
                ):
                    raise MemoryIndexError(
                        "memory database identity changed; restart Ash and rebuild memory"
                    )
                yield directory
                directory.validation_path()
        except (OSError, AnchoredFilesystemError) as exc:
            raise MemoryIndexError(
                f"memory storage identity changed or became unavailable: {exc}"
            ) from exc

    @contextmanager
    def _connect_in(
        self,
        directory: AnchoredDirectory,
        *,
        initialize: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        pin = -1
        connection: sqlite3.Connection | None = None
        try:
            existing = directory.stat(self._name)
            if existing is not None:
                if not stat.S_ISREG(existing.st_mode):
                    raise MemoryIndexError(
                        f"memory database is not a regular file: {self.db_path}"
                    )
                pin = directory.open_file(
                    self._name,
                    os.O_RDWR,
                    expected=existing,
                    expected_type=stat.S_IFREG,
                )
            sqlite_path = self._sqlite_path(directory)
            connection = sqlite3.connect(
                sqlite_path,
                check_same_thread=False,
                timeout=self._busy_timeout_ms / 1000,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            if initialize:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            if pin >= 0 and not directory.same_entry(self._name, pin):
                raise MemoryIndexError(
                    "memory database changed while opening; restart Ash and rebuild memory"
                )
            yield connection
            if pin >= 0 and not directory.same_entry(self._name, pin):
                raise MemoryIndexError(
                    "memory database changed during use; restart Ash and rebuild memory"
                )
        finally:
            if connection is not None:
                connection.close()
            if pin >= 0:
                os.close(pin)

    def _sqlite_path(self, directory: AnchoredDirectory) -> str:
        if sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
            return str(Path(f"/proc/self/fd/{directory.descriptor}") / self._name)
        directory.validation_path()
        return str(self.db_path)

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        with connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > MEMORY_SCHEMA_VERSION:
                raise MemoryIndexError(
                    "memory database schema "
                    f"v{version} is newer than supported v{MEMORY_SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_documents (
                    file_path TEXT PRIMARY KEY,
                    sha256 TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chunk_key TEXT NOT NULL UNIQUE,
                    file_path TEXT NOT NULL REFERENCES memory_documents(file_path)
                        ON DELETE CASCADE,
                    start_line INTEGER NOT NULL CHECK(start_line >= 1),
                    end_line INTEGER NOT NULL CHECK(end_line >= start_line),
                    content TEXT NOT NULL,
                    symbol_tags TEXT NOT NULL DEFAULT '',
                    embedding BLOB,
                    embedding_dimension INTEGER,
                    CHECK(
                        (embedding IS NULL AND embedding_dimension IS NULL)
                        OR
                        (embedding IS NOT NULL AND embedding_dimension > 0)
                    )
                );

                CREATE INDEX IF NOT EXISTS memory_chunks_file_path
                    ON memory_chunks(file_path);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    chunk_key UNINDEXED,
                    file_path UNINDEXED,
                    content,
                    symbol_tags,
                    tokenize="unicode61"
                );
                """
            )
            connection.execute(f"PRAGMA user_version={MEMORY_SCHEMA_VERSION}")

    def _initialize_identity(self, connection: sqlite3.Connection) -> None:
        current_workspace = _workspace_identity(self._workspace)
        current = {
            "workspace_path": str(self._workspace),
            "workspace_device": str(current_workspace[0]) if current_workspace else "",
            "workspace_inode": str(current_workspace[1]) if current_workspace else "",
            "chunking_version": self._chunking_version,
            "embedding_format": EMBEDDING_FORMAT,
        }
        stored = _read_meta(connection)
        reset = False
        if stored:
            if stored.get("workspace_path") != current["workspace_path"]:
                reset = True
            elif current_workspace is not None:
                device = stored.get("workspace_device", "")
                inode = stored.get("workspace_inode", "")
                if device and inode and (device, inode) != (
                    current["workspace_device"],
                    current["workspace_inode"],
                ):
                    reset = True
            if stored.get("chunking_version") != self._chunking_version:
                reset = True
            if stored.get("embedding_format") not in {None, EMBEDDING_FORMAT}:
                reset = True
        with connection:
            if reset:
                self._clear_content(connection)
                connection.execute(
                    "DELETE FROM memory_meta WHERE key IN "
                    "('embedding_identity', 'embedding_dimension')"
                )
            for key, value in current.items():
                _write_meta(connection, key, value)

    def configure_embedding(self, identity: str, dimension: int) -> bool:
        """Bind stored vectors to one embedding contract.

        Returns ``True`` when incompatible old vectors were discarded.  Lexical
        chunks remain searchable and can be re-embedded incrementally.
        """

        normalized = identity.strip()
        if not normalized:
            raise ValueError("embedding identity must be non-empty")
        if dimension < 1 or dimension > 65_536:
            raise ValueError("embedding dimension is out of bounds")
        with self._lock, self._directory() as directory, directory.lock(
            "memory-index"
        ):
            with self._connect_in(directory) as connection, connection:
                meta = _read_meta(connection)
                previous_identity = meta.get("embedding_identity")
                previous_dimension = meta.get("embedding_dimension")
                changed = bool(previous_identity) and (
                    previous_identity != normalized
                    or previous_dimension != str(dimension)
                )
                if changed:
                    connection.execute(
                        "UPDATE memory_chunks SET embedding=NULL, "
                        "embedding_dimension=NULL"
                    )
                _write_meta(connection, "embedding_identity", normalized)
                _write_meta(connection, "embedding_dimension", str(dimension))
            self._restrict_storage_permissions(directory)
            return changed

    def replace_documents(self, documents: Sequence[MemoryDocument]) -> int:
        """Atomically replace a batch of documents across text, FTS, and vectors."""

        paths = [document.file_path for document in documents]
        if len(paths) != len(set(paths)):
            raise ValueError("documents must contain unique file paths")
        with self._lock, self._directory() as directory, directory.lock(
            "memory-index"
        ):
            with self._connect_in(directory) as connection:
                expected_dimension = _read_embedding_dimension(connection)
                prepared = [
                    self._prepare_document(
                        document,
                        expected_dimension=expected_dimension,
                    )
                    for document in documents
                ]
                with connection:
                    now = datetime.now(timezone.utc).isoformat()
                    for document, rows in prepared:
                        connection.execute(
                            "DELETE FROM memory_fts WHERE file_path = ?",
                            (document.file_path,),
                        )
                        connection.execute(
                            "DELETE FROM memory_documents WHERE file_path = ?",
                            (document.file_path,),
                        )
                        if not rows:
                            continue
                        connection.execute(
                            "INSERT INTO memory_documents(file_path, sha256, updated_at) "
                            "VALUES (?, ?, ?)",
                            (document.file_path, document.sha256, now),
                        )
                        connection.executemany(
                            """
                            INSERT INTO memory_chunks(
                                chunk_key, file_path, start_line, end_line, content,
                                symbol_tags, embedding, embedding_dimension
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            rows,
                        )
                        connection.executemany(
                            """
                            INSERT INTO memory_fts(
                                chunk_key, file_path, content, symbol_tags
                            ) VALUES (?, ?, ?, ?)
                            """,
                            [(row[0], row[1], row[4], row[5]) for row in rows],
                        )
            self._restrict_storage_permissions(directory)
        return sum(len(document.chunks) for document in documents)

    def _prepare_document(
        self,
        document: MemoryDocument,
        *,
        expected_dimension: int | None,
    ) -> tuple[MemoryDocument, list[tuple[object, ...]]]:
        chunks = list(document.chunks)
        embeddings = (
            None if document.embeddings is None else list(document.embeddings)
        )
        if embeddings is not None and len(embeddings) != len(chunks):
            raise ValueError("embeddings must match chunks length")
        if embeddings is not None and expected_dimension is None:
            raise MemoryIndexError(
                "embedding storage is not configured for this memory index"
            )
        rows: list[tuple[object, ...]] = []
        for index, chunk in enumerate(chunks):
            chunk_key = (
                f"{document.file_path}:{int(chunk.start_line)}-{int(chunk.end_line)}"
            )
            blob: bytes | None = None
            dimension: int | None = None
            if embeddings is not None:
                assert expected_dimension is not None
                blob = _pack_embedding(embeddings[index], expected_dimension)
                dimension = expected_dimension
            rows.append(
                (
                    chunk_key,
                    document.file_path,
                    int(chunk.start_line),
                    int(chunk.end_line),
                    chunk.content,
                    "",
                    blob,
                    dimension,
                )
            )
        return document, rows

    def embedding_dimension(self) -> int | None:
        with self._lock, self._directory() as directory:
            with self._connect_in(directory) as connection:
                return _read_embedding_dimension(connection)

    def lexical_search(self, query: str, *, limit: int) -> list[MemoryCandidate]:
        if type(limit) is not int or not 1 <= limit <= MAX_MEMORY_SEARCH_RESULTS:
            raise ValueError(
                "memory search limit must be between 1 and "
                f"{MAX_MEMORY_SEARCH_RESULTS}"
            )
        terms = _literal_fts5_terms(query)
        if not terms:
            return []
        with self._lock, self._directory() as directory:
            with self._connect_in(directory) as connection:
                rows = self._lexical_rows(
                    connection,
                    " AND ".join(f'"{term}"' for term in terms),
                    limit,
                )
                if not rows and len(terms) > 1:
                    rows = self._lexical_rows(
                        connection,
                        " OR ".join(f'"{term}"' for term in terms),
                        limit,
                    )
        return [
            MemoryCandidate(
                chunk_key=str(row["chunk_key"]),
                file_path=str(row["file_path"]),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                content=str(row["content"]),
                score=_bm25_rank_to_score(float(row["rank"])),
            )
            for row in rows
        ]

    @staticmethod
    def _lexical_rows(
        connection: sqlite3.Connection,
        expression: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT f.chunk_key, f.file_path, f.content,
                   c.start_line, c.end_line, bm25(memory_fts) AS rank
            FROM memory_fts AS f
            JOIN memory_chunks AS c ON c.chunk_key = f.chunk_key
            WHERE memory_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()

    def vector_search(
        self,
        query_embedding: Sequence[float],
        *,
        limit: int,
    ) -> list[MemoryCandidate]:
        if type(limit) is not int or not 1 <= limit <= MAX_MEMORY_SEARCH_RESULTS:
            raise ValueError(
                "memory search limit must be between 1 and "
                f"{MAX_MEMORY_SEARCH_RESULTS}"
            )
        query = [float(value) for value in query_embedding]
        if any(not math.isfinite(value) for value in query):
            raise ValueError("query embedding contains a non-finite value")
        with self._lock, self._directory() as directory:
            with self._connect_in(directory) as connection:
                connection.execute("BEGIN")
                try:
                    dimension = _read_embedding_dimension(connection)
                    if dimension is None:
                        return []
                    if len(query) != dimension:
                        raise MemoryIndexError(
                            "query embedding dimension "
                            f"{len(query)} does not match stored {dimension}"
                        )
                    count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM memory_chunks "
                            "WHERE embedding IS NOT NULL"
                        ).fetchone()[0]
                    )
                    if count > MAX_VECTOR_SCAN_RECORDS:
                        raise MemoryIndexError(
                            "memory vector index exceeds the bounded software scan limit"
                        )
                    rows = connection.execute(
                        """
                        SELECT chunk_key, file_path, start_line, end_line, content,
                               embedding, embedding_dimension
                        FROM memory_chunks
                        WHERE embedding IS NOT NULL
                        """
                    ).fetchall()
                finally:
                    connection.rollback()

        scored: list[tuple[float, int, sqlite3.Row]] = []
        for ordinal, row in enumerate(rows):
            row_dimension = int(row["embedding_dimension"])
            if row_dimension != dimension:
                raise MemoryIndexError("stored memory embedding dimension is inconsistent")
            vector = _unpack_embedding(bytes(row["embedding"]), dimension)
            score = _cosine_similarity(query, vector)
            item = (score, -ordinal, row)
            if len(scored) < limit:
                heapq.heappush(scored, item)
            elif item > scored[0]:
                heapq.heapreplace(scored, item)
        scored.sort(reverse=True)
        return [
            MemoryCandidate(
                chunk_key=str(row["chunk_key"]),
                file_path=str(row["file_path"]),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                content=str(row["content"]),
                score=float(score),
            )
            for score, _ordinal, row in scored
        ]

    def delete_document(self, file_path: str) -> int:
        with self._lock, self._directory() as directory, directory.lock(
            "memory-index"
        ):
            with self._connect_in(directory) as connection, connection:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_chunks WHERE file_path = ?",
                        (file_path,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "DELETE FROM memory_fts WHERE file_path = ?", (file_path,)
                )
                connection.execute(
                    "DELETE FROM memory_documents WHERE file_path = ?", (file_path,)
                )
            self._restrict_storage_permissions(directory)
            return count

    def document_paths(self, *, limit: int = MAX_DOCUMENTS) -> set[str]:
        if limit < 1 or limit > MAX_DOCUMENTS:
            raise ValueError(f"limit must be between 1 and {MAX_DOCUMENTS}")
        with self._lock, self._directory() as directory:
            with self._connect_in(directory) as connection:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0]
                )
                if count > limit:
                    raise ValueError(
                        f"memory index contains {count} documents; inventory limit is {limit}"
                    )
                rows = connection.execute(
                    "SELECT file_path FROM memory_documents ORDER BY file_path"
                ).fetchall()
        return {str(row["file_path"]) for row in rows}

    def clear(self) -> None:
        with self._lock, self._directory() as directory, directory.lock(
            "memory-index"
        ):
            with self._connect_in(directory) as connection, connection:
                self._clear_content(connection)
            self._restrict_storage_permissions(directory)

    @staticmethod
    def _clear_content(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM memory_fts")
        connection.execute("DELETE FROM memory_chunks")
        connection.execute("DELETE FROM memory_documents")

    def export_records(self, *, limit: int) -> list[dict[str, object]]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock, self._directory() as directory:
            with self._connect_in(directory) as connection:
                rows = connection.execute(
                    """
                    SELECT chunk_key, file_path, start_line, end_line, content,
                           embedding IS NOT NULL AS has_embedding
                    FROM memory_chunks
                    ORDER BY chunk_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def _restrict_storage_permissions(self, directory: AnchoredDirectory) -> None:
        if os.name == "nt":
            return
        for name in (self._name, f"{self._name}-wal", f"{self._name}-shm"):
            metadata = directory.stat(name)
            if metadata is None:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise MemoryIndexError(
                    f"memory SQLite state is not a regular file: {self._parent / name}"
                )
            descriptor = directory.open_file(
                name,
                os.O_RDWR,
                expected=metadata,
                expected_type=stat.S_IFREG,
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)


def _workspace_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_ino == 0:
        return None
    return metadata.st_dev, metadata.st_ino


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    if left.st_ino == 0 or right.st_ino == 0:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key, value FROM memory_meta")
    }


def _read_embedding_dimension(connection: sqlite3.Connection) -> int | None:
    value = _read_meta(connection).get("embedding_dimension")
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise MemoryIndexError("stored embedding dimension is invalid") from exc


def _write_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO memory_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def _pack_embedding(values: Sequence[float], dimension: int) -> bytes:
    vector = [float(value) for value in values]
    if len(vector) != dimension:
        raise ValueError(
            f"embedding dimension {len(vector)} does not match configured {dimension}"
        )
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding contains a non-finite value")
    return struct.pack(f"<{dimension}f", *vector)


def _unpack_embedding(blob: bytes, dimension: int) -> tuple[float, ...]:
    expected = dimension * 4
    if len(blob) != expected:
        raise MemoryIndexError(
            f"stored embedding has {len(blob)} bytes; expected {expected}"
        )
    return struct.unpack(f"<{dimension}f", blob)


def _cosine_similarity(query: Sequence[float], document: Sequence[float]) -> float:
    dot = 0.0
    q_sq = 0.0
    d_sq = 0.0
    for query_value, document_value in zip(query, document, strict=True):
        q = float(query_value)
        d = float(document_value)
        dot += q * d
        q_sq += q * q
        d_sq += d * d
    if q_sq == 0.0 or d_sq == 0.0:
        return 0.0
    return dot / math.sqrt(q_sq * d_sq)


def _literal_fts5_terms(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"\w+", value, flags=re.UNICODE):
        term = raw[:MAX_QUERY_TERM_CHARS]
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def _bm25_rank_to_score(rank: float) -> float:
    if not math.isfinite(rank):
        return 1.0 / 1000.0
    if rank < 0:
        relevance = -rank
        return relevance / (1.0 + relevance)
    return 1.0 / (1.0 + rank)
