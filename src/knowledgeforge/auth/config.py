"""Auth gateway configuration."""

import os
import secrets
from pathlib import Path

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
    gateway_host: str = "100.115.155.120"
    gateway_port: int = 8744
    upstream_url: str = "http://127.0.0.1:8743"

    # JWT
    jwt_secret: str = ""  # Generated at startup if empty
    jwt_algorithm: str = "HS256"
    session_ttl_seconds: int = 21600  # 6 hours

    # X-Auth shared secret gate
    x_auth_secret: str = ""

    # Telegram bot
    telegram_bot_token: str = ""  # REQUIRED for remote auth
    telegram_bot_token_file: str = "/home/bsdev/knowledgeforge/bot_token"
    telegram_owner_chat_id: str = "1082729605"

    # SQLite
    db_path: str = "~/.local/share/knowledgeforge/auth_sessions.sqlite3"

    # Rate limiting
    max_pending_requests: int = 10
    request_cooldown_seconds: int = 2

    # Local bypass — IPs that skip authentication
    local_ips: list[str] = [
        "127.0.0.1",
        "::1",
        "localhost",
    ]

    def ensure_jwt_secret(self) -> "AuthGatewayConfig":
        """Load or generate a JWT secret, persisting it for restart survival.

        If no secret was provided via env var, we look for a persisted one at
        ``<db_dir>/jwt_secret``.  If that doesn't exist either, we generate a
        new one and write it to disk so tokens survive service restarts.
        """
        if self.jwt_secret:
            return self

        secret_path = Path(os.path.expanduser(self.db_path)).parent / "jwt_secret"
        if secret_path.exists():
            stored = secret_path.read_text(encoding="utf-8").strip()
            if stored:
                self.jwt_secret = stored
                return self

        # Generate and persist
        self.jwt_secret = secrets.token_urlsafe(32)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(self.jwt_secret, encoding="utf-8")
        # Restrict permissions to owner-only
        secret_path.chmod(0o600)
        return self

    def load_telegram_token(self) -> "AuthGatewayConfig":
        """Load Telegram bot token from file when not provided directly."""
        if self.telegram_bot_token:
            return self
        token_file = Path(os.path.expanduser(self.telegram_bot_token_file))
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                self.telegram_bot_token = token
        return self

    @property
    def db_path_expanded(self) -> str:
        return os.path.expanduser(self.db_path)
