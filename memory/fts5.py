"""SQLite FTS5 lexical search index for Ash semantic memory."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ash.context.compaction import Chunk
from ash.core.session import get_db_connection


DEFAULT_QUERY_LIMIT = 5
CHUNK_TOKENIZE = "unicode61"


class FTS5Index:
    """Manage an FTS5 virtual table that indexes chunked workspace documents."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        self._init_db()

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

        with closing(get_db_connection(self.db_path)) as conn, conn:
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

    def query(
        self, query_str: str, limit: int = DEFAULT_QUERY_LIMIT
    ) -> list[dict[str, Any]]:
        """Run a BM25-ranked FTS5 query and return matching chunks as dicts."""

        with closing(get_db_connection(self.db_path)) as conn:
            return query_lexical_fallback(conn, query_str, limit=limit)


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

    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT file_path, content, symbol_tags, bm25(fts_index) as rank
        FROM fts_index
        WHERE fts_index MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query_str, limit),
    )
    return [dict(row) for row in cursor.fetchall()]
