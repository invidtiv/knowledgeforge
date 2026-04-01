"""Pydantic models for auth gateway sessions and API responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


SessionStatus = Literal["pending", "approved", "denied", "expired", "revoked"]


class Session(BaseModel):
    """A tracked connection session."""

    id: str
    request_id: str
    client_ip: str
    user_agent: str
    requested_path: str
    status: SessionStatus
    token_hash: str | None = None
    telegram_message_id: int | None = None
    created_at: float
    approved_at: float | None = None
    expires_at: float | None = None
    revoked_at: float | None = None


class AuthStatusResponse(BaseModel):
    """Response for GET /auth/status/{request_id}."""

    status: str
    token: str | None = None
    message: str | None = None
    expires_at: float | None = None


class ActiveSessionInfo(BaseModel):
    """Summary of an active session for the /sessions Telegram command."""

    session_id: str
    request_id: str
    client_ip: str
    user_agent: str
    approved_at: float
    expires_at: float
    remaining_seconds: int
