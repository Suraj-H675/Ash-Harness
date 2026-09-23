"""SQLite FTS5 lexical search index for Ash semantic memory."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ash.context.compaction import Chunk
from ash.core.session import get_db_connection
from ash.safe_io import validate_unlinked_file_path


DEFAULT_QUERY_LIMIT = 5
MAX_QUERY_TERMS = 32
MAX_QUERY_TERM_CHARS = 128
CHUNK_TOKENIZE = "unicode61"


class FTS5Index:
    """Manage an FTS5 virtual table that indexes chunked workspace documents."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(
            validate_unlinked_file_path(db_path, label="FTS5 memory database")
        )
        self._restrict_storage_permissions()
        self._init_db()
        self._restrict_storage_permissions()

    def _restrict_storage_permissions(self) -> None:
        """Keep persisted lexical memory private on POSIX hosts."""

        if os.name == "nt":
            return
        database = Path(self.db_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(database, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"FTS5 memory database is not a regular file: {database}")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            sidecar_descriptor = -1
            try:
                sidecar_descriptor = os.open(
                    sidecar,
                    os.O_RDONLY
                    | int(getattr(os, "O_CLOEXEC", 0))
                    | int(getattr(os, "O_NOFOLLOW", 0))
                    | int(getattr(os, "O_NONBLOCK", 0)),
                )
                metadata = os.fstat(sidecar_descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(
                        f"FTS5 memory sidecar is not a regular file: {sidecar}"
                    )
                os.fchmod(sidecar_descriptor, 0o600)
            except FileNotFoundError:
                continue
            finally:
                if sidecar_descriptor >= 0:
                    os.close(sidecar_descriptor)

    def _init_db(self) -> None:
        """Create the FTS5 virtual table and document metadata table if missing."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.executescript(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
                    file_path,
                    content,
                    symbol_tags,
                    tokenize="{CHUNK_TOKENIZE}"
                );

                CREATE TABLE IF NOT EXISTS document_metadata (
                    rowid INTEGER PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    last_modified TIMESTAMP,
                    sha256 TEXT UNIQUE
                );
                """
            )

    def index_document(
        self,
        file_path: str,
        chunks: list[Chunk],
        symbol_tags: str = "",
        sha256: str | None = None,
    ) -> int:
        """
        Replace any existing chunks for ``file_path`` and insert the new ones.

        Returns the rowid of the first inserted chunk (used as the metadata
        anchor in ``document_metadata``), or 0 when ``chunks`` is empty.
        """

        try:
            with closing(get_db_connection(self.db_path)) as conn, conn:
                return self._index_document(
                    conn,
                    file_path,
                    chunks,
                    symbol_tags=symbol_tags,
                    sha256=sha256,
                )
        finally:
            self._restrict_storage_permissions()

    def index_documents(
        self,
        documents: Iterable[tuple[str, Sequence[Chunk], str | None]],
        symbol_tags: str = "",
    ) -> int:
        """Replace and insert multiple documents in one SQLite transaction.

        Project indexing can touch hundreds of small files. Keeping the
        connection and transaction open for the batch avoids paying SQLite's
        WAL setup and commit cost once per file while preserving the
        replacement semantics of :meth:`index_document`.
        """

        indexed = 0
        try:
            with closing(get_db_connection(self.db_path)) as conn, conn:
                for file_path, chunks, sha256 in documents:
                    self._index_document(
                        conn,
                        file_path,
                        chunks,
                        symbol_tags=symbol_tags,
                        sha256=sha256,
                    )
                    indexed += 1
        finally:
            self._restrict_storage_permissions()
        return indexed

    @staticmethod
    def _index_document(
        conn: sqlite3.Connection,
        file_path: str,
        chunks: Sequence[Chunk],
        *,
        symbol_tags: str,
        sha256: str | None,
    ) -> int:
        """Index one document using an existing SQLite connection."""

        conn.execute("DELETE FROM fts_index WHERE file_path = ?", (file_path,))
        conn.execute(
            "DELETE FROM document_metadata WHERE file_path = ?",
            (file_path,),
        )

        first_rowid = 0
        for chunk in chunks:
            cursor = conn.execute(
                """
                INSERT INTO fts_index (file_path, content, symbol_tags)
                VALUES (?, ?, ?)
                """,
                (file_path, chunk.content, symbol_tags),
            )
            if first_rowid == 0 and cursor.lastrowid is not None:
                first_rowid = int(cursor.lastrowid)

        if first_rowid and sha256 is not None:
            conn.execute(
                """
                INSERT INTO document_metadata (rowid, file_path, last_modified, sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(rowid) DO UPDATE SET
                    file_path = excluded.file_path,
                    last_modified = excluded.last_modified,
                    sha256 = excluded.sha256
                """,
                (
                    first_rowid,
                    file_path,
                    datetime.now(timezone.utc).isoformat(),
                    sha256,
                ),
            )

        return first_rowid

    def delete_document(self, file_path: str) -> int:
        """Remove all indexed chunks and metadata for ``file_path``."""

        try:
            with closing(get_db_connection(self.db_path)) as conn, conn:
                fts_cursor = conn.execute(
                    "DELETE FROM fts_index WHERE file_path = ?",
                    (file_path,),
                )
                conn.execute(
                    "DELETE FROM document_metadata WHERE file_path = ?",
                    (file_path,),
                )
                return fts_cursor.rowcount
        finally:
            self._restrict_storage_permissions()

    def query(
        self, query_str: str, limit: int = DEFAULT_QUERY_LIMIT
    ) -> list[dict[str, Any]]:
        """Run a BM25-ranked FTS5 query and return matching chunks as dicts."""

        try:
            with closing(get_db_connection(self.db_path)) as conn:
                return query_lexical_fallback(conn, query_str, limit=limit)
        finally:
            self._restrict_storage_permissions()

    def document_paths(self, *, limit: int = 10_000) -> set[str]:
        """Return a bounded inventory of indexed document identities."""

        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        try:
            with closing(get_db_connection(self.db_path)) as conn:
                count = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT file_path) FROM fts_index"
                    ).fetchone()[0]
                )
                if count > limit:
                    raise ValueError(
                        f"memory index contains {count} documents; inventory limit is {limit}"
                    )
                rows = conn.execute(
                    "SELECT DISTINCT file_path FROM fts_index ORDER BY file_path LIMIT ?",
                    (limit,),
                ).fetchall()
        finally:
            self._restrict_storage_permissions()
        return {str(row[0]) for row in rows}

    def clear(self) -> None:
        try:
            with closing(get_db_connection(self.db_path)) as conn, conn:
                conn.execute("DELETE FROM fts_index")
                conn.execute("DELETE FROM document_metadata")
        finally:
            self._restrict_storage_permissions()


def query_lexical_fallback(
    db_conn: sqlite3.Connection,
    query_str: str,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> list[dict[str, Any]]:
    """
    Query the FTS5 virtual table using SQLite's built-in BM25 ranking.

    Mirrors spec section 5.4: lower ``bm25`` scores are more relevant, so
    the default ``ORDER BY rank`` ascending puts the best matches first.
    """

    literal_query = _literal_fts5_query(query_str)
    if not literal_query:
        return []
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT file_path, content, symbol_tags, bm25(fts_index) as rank
        FROM fts_index
        WHERE fts_index MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (literal_query, limit),
    )
    return [dict(row) for row in cursor.fetchall()]


def _literal_fts5_query(value: str) -> str:
    """Convert natural-language input into a bounded literal FTS5 OR query."""

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
    return " OR ".join(f'"{term}"' for term in terms)
