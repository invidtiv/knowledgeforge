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

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    request_id TEXT,
    session_id TEXT,
    client_ip TEXT,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS x_auth_connections (
    id TEXT PRIMARY KEY,
    client_ip TEXT NOT NULL,
    user_agent TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    request_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active',
    telegram_message_id INTEGER,
    terminated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_xauth_ip_status ON x_auth_connections(client_ip, status);
"""

# Deduplication window: if an X-Auth connection from the same IP was seen
# within this many seconds, it is treated as the same connection (not new).
_XAUTH_DEDUP_WINDOW = 300  # 5 minutes

# How long a terminated-IP block remains in effect.
_XAUTH_TERMINATION_TTL = 21600  # 6 hours


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

    # ── Audit ──────────────────────────────────────────────

    async def _audit(
        self,
        event: str,
        request_id: str | None = None,
        session_id: str | None = None,
        client_ip: str | None = None,
        detail: str = "",
    ) -> None:
        """Write a row to the append-only audit log."""
        try:
            await self._db.execute(
                "INSERT INTO audit_log (ts, event, request_id, session_id, client_ip, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), event, request_id, session_id, client_ip, detail),
            )
            await self._db.commit()
        except Exception:
            logger.exception("Failed to write audit log entry: %s", event)

    async def recent_audit(self, limit: int = 20) -> list[dict]:
        """Return the most recent audit log entries."""
        async with self._db.execute(
            "SELECT ts, event, request_id, session_id, client_ip, detail "
            "FROM audit_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "ts": r[0],
                "event": r[1],
                "request_id": r[2],
                "session_id": r[3],
                "client_ip": r[4],
                "detail": r[5],
            }
            for r in rows
        ]

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

        session = Session(
            id=session_id,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
            requested_path=path,
            status="pending",
            created_at=now,
        )
        await self._audit("request_created", request_id, session_id, client_ip,
                          f"path={path}")
        return session

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
        await self._audit("approved", request_id, row["id"], row["client_ip"],
                          f"expires_at={expires:.0f}")
        logger.info("Session approved: request_id=%s ip=%s expires_in=%ds",
                     request_id, row["client_ip"], self.session_ttl)
        return session, token

    async def deny(self, request_id: str) -> Session:
        row = await self._get_by_request_id(request_id)
        if not row:
            raise ValueError(f"Unknown request_id: {request_id}")
        if row["status"] != "pending":
            raise ValueError(f"Session not pending (status={row['status']})")

        await self._db.execute(
            "UPDATE sessions SET status='denied' WHERE request_id=?",
            (request_id,),
        )
        await self._db.commit()

        session = self._row_to_session(row)
        session.status = "denied"
        await self._audit("denied", request_id, row["id"], row["client_ip"])
        logger.info("Session denied: request_id=%s ip=%s", request_id, row["client_ip"])
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
        revoked = cur.rowcount > 0
        if revoked:
            await self._audit("revoked", session_id=session_id,
                              detail="manual revocation")
            logger.info("Session revoked: session_id=%s", session_id)
        return revoked

    async def revoke_all(self) -> int:
        now = time.time()
        # Collect IDs before revoking for audit
        async with self._db.execute(
            "SELECT id, request_id, client_ip FROM sessions WHERE status='approved'"
        ) as cur:
            to_revoke = await cur.fetchall()

        cur = await self._db.execute(
            "UPDATE sessions SET status='revoked', revoked_at=? WHERE status='approved'",
            (now,),
        )
        await self._db.commit()
        count = cur.rowcount
        if count:
            for r in to_revoke:
                await self._audit("revoked", r[1], r[0], r[2], "bulk revoke_all")
            logger.info("Revoked %d active sessions (revoke_all)", count)
        return count

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

    async def get_pending_for_ip(self, ip: str):
        """Return the most recent pending session for an IP, if any."""
        async with self._db.execute(
            "SELECT * FROM sessions WHERE client_ip=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (ip,),
        ) as cur:
            row = await cur.fetchone()
            return row if row else None

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

    async def cleanup_expired(self) -> tuple[int, int]:
        """Expire stale sessions.  Returns (expired_approved, expired_pending)."""
        now = time.time()

        # Collect soon-to-expire approved sessions for audit
        async with self._db.execute(
            "SELECT id, request_id, client_ip FROM sessions "
            "WHERE status='approved' AND expires_at < ?",
            (now,),
        ) as cur:
            expired_approved_rows = await cur.fetchall()

        cur_a = await self._db.execute(
            "UPDATE sessions SET status='expired' WHERE status='approved' AND expires_at < ?",
            (now,),
        )
        approved_count = cur_a.rowcount

        # Collect stale pending sessions for audit
        pending_cutoff = now - 600
        async with self._db.execute(
            "SELECT id, request_id, client_ip FROM sessions "
            "WHERE status='pending' AND created_at < ?",
            (pending_cutoff,),
        ) as cur:
            expired_pending_rows = await cur.fetchall()

        cur_p = await self._db.execute(
            "UPDATE sessions SET status='expired' WHERE status='pending' AND created_at < ?",
            (pending_cutoff,),
        )
        pending_count = cur_p.rowcount
        await self._db.commit()

        # Audit each expiry
        for r in expired_approved_rows:
            await self._audit("session_expired", r[1], r[0], r[2], "ttl reached")
        for r in expired_pending_rows:
            await self._audit("pending_expired", r[1], r[0], r[2],
                              "stale pending (>10 min)")

        return approved_count, pending_count

    async def list_pending(self) -> list[Session]:
        """Return all currently pending sessions."""
        async with self._db.execute(
            "SELECT * FROM sessions WHERE status='pending' ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_session(r) for r in rows]

    # ── X-Auth connection tracking ──────────────────────────

    async def track_x_auth_connection(
        self, client_ip: str, user_agent: str
    ) -> tuple[str, bool]:
        """Record an X-Auth connection from client_ip.

        Returns (connection_id, is_new).
        is_new is True when no active connection from this IP was seen within
        the last _XAUTH_DEDUP_WINDOW seconds.  If a recent record exists,
        last_seen and request_count are updated and is_new is False.
        """
        now = time.time()
        cutoff = now - _XAUTH_DEDUP_WINDOW

        # Look for an existing active record from this IP within the window.
        async with self._db.execute(
            "SELECT id FROM x_auth_connections "
            "WHERE client_ip=? AND status='active' AND last_seen >= ? "
            "ORDER BY last_seen DESC LIMIT 1",
            (client_ip, cutoff),
        ) as cur:
            row = await cur.fetchone()

        if row:
            conn_id = row[0]
            await self._db.execute(
                "UPDATE x_auth_connections "
                "SET last_seen=?, request_count=request_count+1 "
                "WHERE id=?",
                (now, conn_id),
            )
            await self._db.commit()
            return conn_id, False

        # New connection.
        conn_id = uuid.uuid4().hex[:12]
        await self._db.execute(
            "INSERT INTO x_auth_connections "
            "(id, client_ip, user_agent, first_seen, last_seen, request_count, status) "
            "VALUES (?, ?, ?, ?, ?, 1, 'active')",
            (conn_id, client_ip, user_agent or "", now, now),
        )
        await self._db.commit()
        await self._audit(
            "x_auth_new_connection",
            client_ip=client_ip,
            detail=f"conn_id={conn_id} ua={str(user_agent or '')[:80]}",
        )
        return conn_id, True

    async def terminate_x_auth_connection(self, connection_id: str) -> bool:
        """Mark a connection as terminated.  Returns True if a record was found."""
        now = time.time()
        cur = await self._db.execute(
            "UPDATE x_auth_connections SET status='terminated', terminated_at=? "
            "WHERE id=?",
            (now, connection_id),
        )
        await self._db.commit()
        found = cur.rowcount > 0
        if found:
            # Fetch IP for audit log.
            async with self._db.execute(
                "SELECT client_ip FROM x_auth_connections WHERE id=?",
                (connection_id,),
            ) as cur2:
                r = await cur2.fetchone()
            client_ip = r[0] if r else None
            await self._audit(
                "x_auth_terminated",
                client_ip=client_ip,
                detail=f"conn_id={connection_id}",
            )
        return found

    async def is_ip_terminated(self, client_ip: str) -> bool:
        """Return True if client_ip has been terminated within the block TTL."""
        cutoff = time.time() - _XAUTH_TERMINATION_TTL
        async with self._db.execute(
            "SELECT COUNT(*) FROM x_auth_connections "
            "WHERE client_ip=? AND status='terminated' AND terminated_at >= ?",
            (client_ip, cutoff),
        ) as cur:
            row = await cur.fetchone()
        return (row[0] if row else 0) > 0

    async def get_active_x_auth_connections(self) -> list[dict]:
        """Return all non-terminated X-Auth connection records."""
        async with self._db.execute(
            "SELECT id, client_ip, user_agent, first_seen, last_seen, "
            "request_count, telegram_message_id "
            "FROM x_auth_connections WHERE status='active' "
            "ORDER BY last_seen DESC",
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "client_ip": r[1],
                "user_agent": r[2],
                "first_seen": r[3],
                "last_seen": r[4],
                "request_count": r[5],
                "telegram_message_id": r[6],
            }
            for r in rows
        ]

    async def set_x_auth_telegram_message_id(
        self, connection_id: str, message_id: int
    ) -> None:
        """Store the Telegram message_id for a connection record."""
        await self._db.execute(
            "UPDATE x_auth_connections SET telegram_message_id=? WHERE id=?",
            (message_id, connection_id),
        )
        await self._db.commit()

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
