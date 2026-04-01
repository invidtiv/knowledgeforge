"""SQLite FTS5 keyword index for hybrid semantic+lexical search."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class KeywordIndex:
    """Persistent BM25-backed keyword index.

    Stores the same chunks indexed in ChromaDB and supports keyword retrieval
    through SQLite FTS5 `bm25()`.
    """

    def __init__(self, db_path: str):
        self.db_path = os.path.expanduser(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self.conn
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS keyword_chunks USING fts5(
                chunk_id UNINDEXED,
                collection UNINDEXED,
                file_path UNINDEXED,
                project_name UNINDEXED,
                language UNINDEXED,
                category UNINDEXED,
                confirmed UNINDEXED,
                frontmatter_tags UNINDEXED,
                content,
                metadata_json UNINDEXED,
                tokenize='unicode61'
            );
            """
        )
        conn.commit()

    def upsert_chunks(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if not ids:
            return

        rows = []
        deletes = []
        for chunk_id, content, metadata in zip(ids, documents, metadatas):
            meta = metadata or {}
            file_path = str(meta.get("file_path") or meta.get("source_file") or "")
            project_name = str(
                meta.get("project_name")
                or meta.get("project")
                or meta.get("frontmatter_project")
                or ""
            )
            language = str(meta.get("language") or "")
            category = str(meta.get("category") or "")
            confirmed = "1" if bool(meta.get("confirmed", False)) else "0"
            frontmatter_tags = str(meta.get("frontmatter_tags") or "")

            deletes.append((chunk_id, collection))
            rows.append(
                (
                    chunk_id,
                    collection,
                    file_path,
                    project_name,
                    language,
                    category,
                    confirmed,
                    frontmatter_tags,
                    content,
                    json.dumps(meta, ensure_ascii=True, sort_keys=True),
                )
            )

        with self.conn:
            self.conn.executemany(
                "DELETE FROM keyword_chunks WHERE chunk_id = ? AND collection = ?",
                deletes,
            )
            self.conn.executemany(
                """
                INSERT INTO keyword_chunks (
                    chunk_id, collection, file_path, project_name, language,
                    category, confirmed, frontmatter_tags, content, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def search(
        self,
        query: str,
        collection: str,
        limit: int = 24,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        match_query = self._build_match_query(query)
        if not match_query:
            return []

        where_clauses = ["keyword_chunks MATCH ?", "collection = ?"]
        params: list[Any] = [match_query, collection]

        filter_values = filters or {}
        project_name = filter_values.get("project_name") or filter_values.get("project")
        if project_name:
            where_clauses.append("project_name = ?")
            params.append(str(project_name))

        language = filter_values.get("language")
        if language:
            where_clauses.append("language = ?")
            params.append(str(language))

        category = filter_values.get("category")
        if category:
            where_clauses.append("category = ?")
            params.append(str(category))

        confirmed = filter_values.get("confirmed")
        if confirmed is True:
            where_clauses.append("confirmed = '1'")

        tags = filter_values.get("tags") or []
        if tags:
            where_clauses.append("frontmatter_tags LIKE ?")
            params.append(f"%{tags[0]}%")

        params.append(max(1, int(limit)))
        sql = f"""
            SELECT chunk_id, collection, file_path, content, metadata_json,
                   bm25(keyword_chunks) AS bm25_score
            FROM keyword_chunks
            WHERE {' AND '.join(where_clauses)}
            ORDER BY bm25_score ASC
            LIMIT ?
        """

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("Keyword search failed for query=%r: %s", query, exc)
            return []

        results: list[dict[str, Any]] = []
        for row in rows:
            metadata_json = row["metadata_json"] or "{}"
            try:
                metadata = json.loads(metadata_json)
            except json.JSONDecodeError:
                metadata = {}
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "collection": row["collection"],
                    "content": row["content"],
                    "metadata": metadata,
                    "bm25_score": float(row["bm25_score"]),
                }
            )
        return results

    def clear_collection(self, collection: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM keyword_chunks WHERE collection = ?", (collection,)
            )

    def count(self, collection: str | None = None) -> int:
        if collection:
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM keyword_chunks WHERE collection = ?",
                (collection,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM keyword_chunks"
            ).fetchone()
        return int(row["cnt"] if row else 0)

    def delete_by_file_path(self, collection: str, file_path: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM keyword_chunks WHERE collection = ? AND file_path = ?",
                (collection, file_path),
            )

    def delete_by_project(self, collection: str, project_name: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM keyword_chunks WHERE collection = ? AND project_name = ?",
                (collection, project_name),
            )

    @staticmethod
    def _build_match_query(query: str) -> str:
        # Keep symbols commonly used in function names, paths, and error codes.
        tokens = re.findall(r"[A-Za-z0-9_#./:-]+", query)
        cleaned = [t.replace('"', "").strip() for t in tokens if t.strip()]
        if not cleaned:
            return ""
        return " OR ".join(f'"{token}"' for token in cleaned)
