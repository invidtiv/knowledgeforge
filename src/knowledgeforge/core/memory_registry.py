"""SQLite registry for structured extracted memory cards."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from knowledgeforge.core.models import MemoryCard


class MemoryRegistry:
    """Structured registry for filtering and lifecycle operations.

    ChromaDB stores embeddings for semantic retrieval. This SQLite registry
    stores the same cards with explicit project/type/status/confidence fields
    so agents and review tools can filter without relying on vector search.
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
            CREATE TABLE IF NOT EXISTS memory_cards (
                card_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                project TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                why TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                confidence TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_conversation TEXT NOT NULL DEFAULT '',
                source_date TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                source_lines TEXT NOT NULL DEFAULT '',
                current_truth INTEGER NOT NULL DEFAULT 0,
                needs_repo_confirmation INTEGER NOT NULL DEFAULT 1,
                tags_json TEXT NOT NULL DEFAULT '[]',
                supersedes_json TEXT NOT NULL DEFAULT '[]',
                superseded_by_json TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_project ON memory_cards(project);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_cards(type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_cards(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_source_date ON memory_cards(source_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_truth ON memory_cards(current_truth);")
        conn.commit()

    def upsert_card(self, card: MemoryCard) -> MemoryCard:
        """Insert or replace a card, deduping by stable content hash."""
        content_hash = card.content_hash()
        existing = self.get_by_content_hash(content_hash)
        if existing and existing.card_id != card.card_id:
            card.card_id = existing.card_id
            card.created_at = existing.created_at

        row = self._card_to_row(card, content_hash)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO memory_cards (
                    card_id, type, project, title, body, why, status, confidence,
                    source_type, source_conversation, source_date, source_path,
                    source_lines, current_truth, needs_repo_confirmation,
                    tags_json, supersedes_json, superseded_by_json, content_hash,
                    created_at, updated_at
                ) VALUES (
                    :card_id, :type, :project, :title, :body, :why, :status,
                    :confidence, :source_type, :source_conversation,
                    :source_date, :source_path, :source_lines, :current_truth,
                    :needs_repo_confirmation, :tags_json, :supersedes_json,
                    :superseded_by_json, :content_hash, :created_at, :updated_at
                )
                ON CONFLICT(card_id) DO UPDATE SET
                    type = excluded.type,
                    project = excluded.project,
                    title = excluded.title,
                    body = excluded.body,
                    why = excluded.why,
                    status = excluded.status,
                    confidence = excluded.confidence,
                    source_type = excluded.source_type,
                    source_conversation = excluded.source_conversation,
                    source_date = excluded.source_date,
                    source_path = excluded.source_path,
                    source_lines = excluded.source_lines,
                    current_truth = excluded.current_truth,
                    needs_repo_confirmation = excluded.needs_repo_confirmation,
                    tags_json = excluded.tags_json,
                    supersedes_json = excluded.supersedes_json,
                    superseded_by_json = excluded.superseded_by_json,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                row,
            )
        return card

    def get_card(self, card_id: str) -> MemoryCard | None:
        row = self.conn.execute(
            "SELECT * FROM memory_cards WHERE card_id = ?", (card_id,)
        ).fetchone()
        return self._row_to_card(row) if row else None

    def get_by_content_hash(self, content_hash: str) -> MemoryCard | None:
        row = self.conn.execute(
            "SELECT * FROM memory_cards WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return self._row_to_card(row) if row else None

    def list_cards(
        self,
        project: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        current_truth: bool | None = None,
        limit: int = 100,
    ) -> list[MemoryCard]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if memory_type:
            clauses.append("type = ?")
            params.append(memory_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if current_truth is not None:
            clauses.append("current_truth = ?")
            params.append(1 if current_truth else 0)

        sql = "SELECT * FROM memory_cards"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_date DESC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_card(row) for row in rows]

    def update_status(
        self,
        card_id: str,
        status: str,
        current_truth: bool | None = None,
    ) -> MemoryCard | None:
        card = self.get_card(card_id)
        if not card:
            return None
        card.status = status
        if current_truth is not None:
            card.current_truth = current_truth
            if current_truth:
                card.needs_repo_confirmation = False
        return self.upsert_card(card)

    def audit(self) -> dict[str, Any]:
        """Return count summaries for review dashboards and MCP tools."""
        total = self.conn.execute("SELECT COUNT(*) AS count FROM memory_cards").fetchone()["count"]
        current_truth = self.conn.execute(
            "SELECT COUNT(*) AS count FROM memory_cards WHERE current_truth = 1"
        ).fetchone()["count"]
        needs_repo = self.conn.execute(
            "SELECT COUNT(*) AS count FROM memory_cards WHERE needs_repo_confirmation = 1"
        ).fetchone()["count"]

        return {
            "total_cards": int(total),
            "current_truth_cards": int(current_truth),
            "needs_repo_confirmation": int(needs_repo),
            "by_status": self._counts_by("status"),
            "by_type": self._counts_by("type"),
            "by_project": self._counts_by("project"),
        }

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM memory_cards").fetchone()
        return int(row["count"] if row else 0)

    def _counts_by(self, column: str) -> dict[str, int]:
        rows = self.conn.execute(
            f"SELECT {column} AS key, COUNT(*) AS count FROM memory_cards GROUP BY {column}"
        ).fetchall()
        return {str(row["key"] or "-"): int(row["count"]) for row in rows}

    @staticmethod
    def _card_to_row(card: MemoryCard, content_hash: str) -> dict[str, Any]:
        return {
            "card_id": card.card_id,
            "type": card.type,
            "project": card.project,
            "title": card.title,
            "body": card.body,
            "why": card.why,
            "status": card.status,
            "confidence": card.confidence,
            "source_type": card.source_type,
            "source_conversation": card.source_conversation,
            "source_date": card.source_date,
            "source_path": card.source_path,
            "source_lines": card.source_lines,
            "current_truth": 1 if card.current_truth else 0,
            "needs_repo_confirmation": 1 if card.needs_repo_confirmation else 0,
            "tags_json": json.dumps(card.tags, ensure_ascii=True),
            "supersedes_json": json.dumps(card.supersedes, ensure_ascii=True),
            "superseded_by_json": json.dumps(card.superseded_by, ensure_ascii=True),
            "content_hash": content_hash,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        }

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> MemoryCard:
        def _loads(value: str) -> list[str]:
            try:
                parsed = json.loads(value or "[]")
            except json.JSONDecodeError:
                return []
            return [str(v) for v in parsed if str(v).strip()]

        return MemoryCard(
            card_id=row["card_id"],
            type=row["type"],
            project=row["project"],
            title=row["title"],
            body=row["body"],
            why=row["why"],
            status=row["status"],
            confidence=row["confidence"],
            source_type=row["source_type"],
            source_conversation=row["source_conversation"],
            source_date=row["source_date"],
            source_path=row["source_path"],
            source_lines=row["source_lines"],
            current_truth=bool(row["current_truth"]),
            needs_repo_confirmation=bool(row["needs_repo_confirmation"]),
            tags=_loads(row["tags_json"]),
            supersedes=_loads(row["supersedes_json"]),
            superseded_by=_loads(row["superseded_by_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
