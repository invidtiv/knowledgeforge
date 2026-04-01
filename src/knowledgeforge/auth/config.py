"""Auth gateway configuration."""

import os
import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthGatewayConfig(BaseSettings):
    """Configuration for the MCP auth gateway.

    All settings can be overridden via environment variables with the
    KNOWLEDGEFORGE_AUTH_ prefix (e.g. KNOWLEDGEFORGE_AUTH_JWT_SECRET).
    """

    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGEFORGE_AUTH_",
        case_sensitive=False,
    )

    # Gateway networking
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8744
    upstream_url: str = "http://127.0.0.1:8743"

    # JWT
    jwt_secret: str = ""  # Generated at startup if empty
    jwt_algorithm: str = "HS256"
    session_ttl_seconds: int = 3600  # 1 hour

    # Telegram bot
    telegram_bot_token: str = ""  # REQUIRED for remote auth
    telegram_owner_chat_id: str = "1082729605"

    # SQLite
    db_path: str = "~/.local/share/knowledgeforge/auth_sessions.sqlite3"

    # Rate limiting
    max_pending_requests: int = 10
    request_cooldown_seconds: int = 30

    # Local bypass — IPs that skip authentication
    local_ips: list[str] = [
        "127.0.0.1",
        "::1",
        "localhost",
    ]

    def ensure_jwt_secret(self) -> "AuthGatewayConfig":
        """Generate a JWT secret if none was provided."""
        if not self.jwt_secret:
            self.jwt_secret = secrets.token_urlsafe(32)
        return self

    @property
    def db_path_expanded(self) -> str:
        return os.path.expanduser(self.db_path)
