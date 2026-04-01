"""SQLite-backed session store for auth gateway."""

from __future__ import annotations

import os
import time
import uuid
import secrets
import string
import logging
from pathlib import Path

import aiosqlite

from .models import Session, ActiveSessionInfo
from . import token_manager as tm

logger = logging.getLogger(__name__)

_SHORT_ID_CHARS = string.ascii_lowercase + string.digits
_SHORT_ID_LEN = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE NOT NULL,
    client_ip TEXT NOT NULL,
    user_agent TEXT DEFAULT '',
    requested_path TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    token_hash TEXT,
    telegram_message_id INTEGER,
    created_at REAL NOT NULL,
    approved_at REAL,
    expires_at REAL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_request_id ON sessions(request_id);
"""


def _short_id() -> str:
    return "".join(secrets.choice(_SHORT_ID_CHARS) for _ in range(_SHORT_ID_LEN))


class SessionStore:
    """Async SQLite session store."""

    def __init__(self, db_path: str, jwt_secret: str, jwt_algorithm: str = "HS256", session_ttl: int = 3600):
        self.db_path = db_path
        self.jwt_secret = jwt_secret
        self.jwt_algorithm = jwt_algorithm
        self.session_ttl = session_ttl
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Create ──────────────────────────────────────────────

    async def create_pending(
        self, client_ip: str, user_agent: str, path: str
    ) -> Session:
        session_id = str(uuid.uuid4())
        request_id = _short_id()
        now = time.time()

        await self._db.execute(
            """INSERT INTO sessions
               (id, request_id, client_ip, user_agent, requested_path, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (session_id, request_id, client_ip, user_agent, path, now),
        )
        await self._db.commit()

        return Session(
            id=session_id,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            requested_path=path,
            status="pending",
            created_at=now,
        )

    # ── Approve / Deny ──────────────────────────────────────

    async def approve(self, request_id: str) -> tuple[Session, str]:
        """Approve a pending request.  Returns (session, jwt_token)."""
        row = await self._get_by_request_id(request_id)
        if not row:
            raise ValueError(f"Unknown request_id: {request_id}")
        if row["status"] != "pending":
            raise ValueError(f"Session not pending (status={row['status']})")

        now = time.time()
        expires = now + self.session_ttl
        token = tm.create_token(
            row["id"], row["client_ip"],
            self.jwt_secret, self.jwt_algorithm, self.session_ttl,
        )
        thash = tm.token_hash(token)

        await self._db.execute(
            """UPDATE sessions
               SET status='approved', approved_at=?, expires_at=?, token_hash=?
               WHERE request_id=?""",
            (now, expires, thash, request_id),
        )
        await self._db.commit()

        session = self._row_to_session(row)
        session.status = "approved"
        session.approved_at = now
        session.expires_at = expires
        session.token_hash = thash
        return session, token

    async def deny(self, request_id: str) -> Session:
        row = await self._get_by_request_id(request_id)
        if not row:
            raise ValueError(f"Unknown request_id: {request_id}")

        await self._db.execute(
            "UPDATE sessions SET status='denied' WHERE request_id=?",
            (request_id,),
        )
        await self._db.commit()

        session = self._row_to_session(row)
        session.status = "denied"
        return session

    # ── Token validation ────────────────────────────────────

    async def validate_token(self, token: str) -> Session | None:
        """Validate a JWT and check it hasn't been revoked."""
        claims = tm.validate_token(token, self.jwt_secret, self.jwt_algorithm)
        if not claims:
            return None

        thash = tm.token_hash(token)
        async with self._db.execute(
            "SELECT * FROM sessions WHERE token_hash=?", (thash,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        session = self._row_to_session(row)
        if session.status != "approved":
            return None

        # Check expiry at DB level too
        if session.expires_at and time.time() > session.expires_at:
            await self._expire(session.id)
            return None

        return session

    # ── Revocation ──────────────────────────────────────────

    async def revoke(self, session_id: str) -> bool:
        now = time.time()
        cur = await self._db.execute(
            "UPDATE sessions SET status='revoked', revoked_at=? WHERE id=? AND status='approved'",
            (now, session_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def revoke_all(self) -> int:
        now = time.time()
        cur = await self._db.execute(
            "UPDATE sessions SET status='revoked', revoked_at=? WHERE status='approved'",
            (now,),
        )
        await self._db.commit()
        return cur.rowcount

    # ── Queries ─────────────────────────────────────────────

    async def get_by_request_id(self, request_id: str) -> Session | None:
        row = await self._get_by_request_id(request_id)
        return self._row_to_session(row) if row else None

    async def list_active(self) -> list[ActiveSessionInfo]:
        now = time.time()
        async with self._db.execute(
            "SELECT * FROM sessions WHERE status='approved' AND expires_at > ?",
            (now,),
        ) as cur:
            rows = await cur.fetchall()

        return [
            ActiveSessionInfo(
                session_id=r["id"],
                request_id=r["request_id"],
                client_ip=r["client_ip"],
                user_agent=r["user_agent"],
                approved_at=r["approved_at"],
                expires_at=r["expires_at"],
                remaining_seconds=max(0, int(r["expires_at"] - now)),
            )
            for r in rows
        ]

    async def pending_count_for_ip(self, ip: str) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE client_ip=? AND status='pending'",
            (ip,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def recent_request_from_ip(self, ip: str, cooldown: int) -> bool:
        cutoff = time.time() - cooldown
        async with self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE client_ip=? AND created_at > ?",
            (ip, cutoff),
        ) as cur:
            row = await cur.fetchone()
            return (row[0] if row else 0) > 0

    async def total_pending(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status='pending'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def cleanup_expired(self) -> int:
        now = time.time()
        cur = await self._db.execute(
            "UPDATE sessions SET status='expired' WHERE status='approved' AND expires_at < ?",
            (now,),
        )
        # Also expire old pending requests (> 10 min)
        await self._db.execute(
            "UPDATE sessions SET status='expired' WHERE status='pending' AND created_at < ?",
            (now - 600,),
        )
        await self._db.commit()
        return cur.rowcount

    # ── Helpers ─────────────────────────────────────────────

    async def _get_by_request_id(self, request_id: str):
        async with self._db.execute(
            "SELECT * FROM sessions WHERE request_id=?", (request_id,)
        ) as cur:
            return await cur.fetchone()

    async def _expire(self, session_id: str) -> None:
        await self._db.execute(
            "UPDATE sessions SET status='expired' WHERE id=?", (session_id,)
        )
        await self._db.commit()

    @staticmethod
    def _row_to_session(row) -> Session:
        return Session(
            id=row["id"],
            request_id=row["request_id"],
            client_ip=row["client_ip"],
            user_agent=row["user_agent"],
            requested_path=row["requested_path"],
            status=row["status"],
            token_hash=row["token_hash"],
            telegram_message_id=row["telegram_message_id"],
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )
