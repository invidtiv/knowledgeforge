"""JWT token creation, validation, and hashing."""

from __future__ import annotations

import hashlib
import time

import jwt


def create_token(
    session_id: str,
    client_ip: str,
    secret: str,
    algorithm: str = "HS256",
    ttl: int = 3600,
) -> str:
    """Create a signed JWT for an approved session."""
    now = time.time()
    payload = {
        "sub": session_id,
        "ip": client_ip,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def validate_token(
    token: str,
    secret: str,
    algorithm: str = "HS256",
) -> dict | None:
    """Validate and decode a JWT.  Returns claims dict or None."""
    try:
        return jwt.decode(token, secret, algorithms=[algorithm])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def token_hash(token: str) -> str:
    """SHA-256 hash of a token (for storage / revocation lookup)."""
    return hashlib.sha256(token.encode()).hexdigest()
