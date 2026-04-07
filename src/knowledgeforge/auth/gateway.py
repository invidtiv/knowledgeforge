"""MCP Authentication Gateway — FastAPI reverse proxy.

Sits in front of the MCP server (upstream on 127.0.0.1:8743).
Local connections bypass auth. Remote connections require:
1) valid X-Auth shared secret
2) Telegram-approved JWT token (6-hour TTL)

Run directly: python -m knowledgeforge.auth.gateway
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AuthGatewayConfig
from .session_store import SessionStore
from .telegram_bot import TelegramAuthBot
from . import token_manager as tm

logger = logging.getLogger(__name__)

# ── Globals (initialized in lifespan) ──────────────────────

config: AuthGatewayConfig
store: SessionStore
bot: TelegramAuthBot | None = None
_upstream_client: httpx.AsyncClient
_cleanup_task: asyncio.Task | None = None
_bot_task: asyncio.Task | None = None


# ── Lifespan ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, store, bot, _upstream_client, _cleanup_task, _bot_task

    _suppress_httpx_token_logging()

    config = AuthGatewayConfig()
    config.ensure_jwt_secret().load_telegram_token()

    # Session store
    store = SessionStore(
        db_path=config.db_path_expanded,
        jwt_secret=config.jwt_secret,
        jwt_algorithm=config.jwt_algorithm,
        session_ttl=config.session_ttl_seconds,
    )
    await store.open()

    # Upstream HTTP client (long timeout for SSE)
    _upstream_client = httpx.AsyncClient(
        base_url=config.upstream_url,
        timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
    )

    # Telegram bot (only if token provided)
    if config.telegram_bot_token:
        bot = TelegramAuthBot(
            bot_token=config.telegram_bot_token,
            owner_chat_id=config.telegram_owner_chat_id,
            session_store=store,
        )
        _bot_task = asyncio.create_task(bot.start_polling())
        logger.info("Telegram auth bot started")
    else:
        logger.warning(
            "No KNOWLEDGEFORGE_AUTH_TELEGRAM_BOT_TOKEN set — "
            "remote auth will deny all requests (no approval channel)"
        )

    # Periodic cleanup
    _cleanup_task = asyncio.create_task(_cleanup_loop())

    logger.info(
        "Auth gateway started on %s:%d → upstream %s",
        config.gateway_host, config.gateway_port, config.upstream_url,
    )

    yield

    # Shutdown
    if _cleanup_task:
        _cleanup_task.cancel()
    if _bot_task:
        _bot_task.cancel()
    if bot:
        await bot.stop()
    await _upstream_client.aclose()
    await store.close()
    logger.info("Auth gateway stopped")


app = FastAPI(title="KnowledgeForge MCP Auth Gateway", lifespan=lifespan)


# ── Helpers ─────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from nginx."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _is_local(ip: str, request: Request | None = None) -> bool:
    """Check if an IP is a local/loopback address.

    Tailscale Funnel traffic arrives from 127.0.0.1 but carries the
    ``Tailscale-Funnel: true`` header -- that traffic is public and must
    NOT bypass auth.
    """
    if request and request.headers.get("tailscale-funnel") == "true":
        return False
    return ip in config.local_ips or ip.startswith("127.") or ip == "::1"


def _get_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _has_valid_x_auth(request: Request) -> bool:
    """Check required X-Auth shared secret for remote access."""
    if not config.x_auth_secret:
        return False
    return request.headers.get("x-auth", "") == config.x_auth_secret


# ── Auth endpoints (not proxied) ────────────────────────────

@app.get("/auth/status/{request_id}")
async def auth_status(request_id: str):
    """Poll this endpoint after receiving a 401 to check approval status."""
    session = await store.get_by_request_id(request_id)
    if not session:
        return JSONResponse({"status": "unknown", "message": "No such request"}, 404)

    if session.status == "approved" and session.token_hash:
        # Re-derive token?  No — the token was generated at approval time.
        # The client must have been polling and gotten it on the first approved response.
        # We return the token from the approval event (stored transiently).
        # Since we can't re-derive the JWT from its hash, we need to cache it briefly.
        # Implementation: the token is returned via _pending_tokens dict.
        token = _pending_tokens.pop(session.request_id, None)
        if token:
            return JSONResponse({
                "status": "approved",
                "token": token,
                "expires_at": session.expires_at,
                "message": "Connection approved. Use this token as Bearer auth.",
            })
        # Token already collected
        return JSONResponse({
            "status": "approved",
            "message": "Token already issued. If lost, request a new connection.",
            "expires_at": session.expires_at,
        })

    if session.status == "denied":
        return JSONResponse({"status": "denied", "message": "Connection denied by owner."}, 403)

    if session.status == "expired":
        return JSONResponse({"status": "expired", "message": "Request expired."}, 410)

    # Still pending
    return JSONResponse({"status": "pending", "message": "Waiting for owner approval via Telegram."})


@app.get("/auth/health")
async def auth_health():
    """Gateway health check."""
    active = await store.list_active()
    pending = await store.total_pending()
    return {
        "status": "healthy",
        "active_sessions": len(active),
        "pending_requests": pending,
        "telegram_bot": bot is not None,
    }


@app.get("/auth/audit")
async def auth_audit(request: Request, limit: int = 20):
    """Recent audit log entries (local access only)."""
    client_ip = _get_client_ip(request)
    if not _is_local(client_ip, request):
        return JSONResponse(
            {"error": "forbidden", "message": "Audit log is local-only."},
            status_code=403,
        )
    entries = await store.recent_audit(limit=min(limit, 100))
    return {"entries": entries, "count": len(entries)}


@app.get("/auth/sessions")
async def auth_sessions(request: Request):
    """List active sessions (local access only)."""
    client_ip = _get_client_ip(request)
    if not _is_local(client_ip, request):
        return JSONResponse(
            {"error": "forbidden", "message": "Sessions list is local-only."},
            status_code=403,
        )
    active = await store.list_active()
    return {
        "sessions": [s.model_dump() for s in active],
        "count": len(active),
    }


# ── Token cache for pending approvals ──────────────────────

_pending_tokens: dict[str, str] = {}
"""request_id → JWT token, populated when approval happens, consumed on first poll."""


# Monkey-patch the store's approve to also cache the token
_original_approve = SessionStore.approve


async def _approve_and_cache(self, request_id: str):
    session, token = await _original_approve(self, request_id)
    _pending_tokens[request_id] = token
    # Auto-evict after 5 minutes (client should poll within seconds)
    asyncio.get_event_loop().call_later(300, _pending_tokens.pop, request_id, None)
    return session, token


SessionStore.approve = _approve_and_cache  # type: ignore[assignment]


# ── Catch-all proxy ─────────────────────────────────────────

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"])
async def proxy(request: Request, path: str):
    """Proxy all requests to the upstream MCP server.

    Local connections pass through without auth.
    Remote connections require a valid Bearer token.
    """
    client_ip = _get_client_ip(request)

    # ── OAuth discovery — return 404 so mcp-remote skips OAuth ──
    if path.startswith(".well-known/") or path == "register":
        return JSONResponse({"error": "not_found"}, status_code=404)

    # ── Local bypass ────────────────────────────────────────
    if _is_local(client_ip, request):
        return await _proxy_request(request, path)

    # ── Terminated IP check ─────────────────────────────────
    # Must run before X-Auth validation so blocked IPs are rejected even
    # if they present the correct shared secret.
    try:
        if await store.is_ip_terminated(client_ip):
            logger.warning("Blocked terminated IP %s for /%s", client_ip, path)
            return JSONResponse(
                {
                    "error": "terminated",
                    "message": "Connection terminated by operator.",
                },
                status_code=403,
            )
    except Exception:
        logger.exception("Error checking terminated IP %s", client_ip)

    # ── Remote: require X-Auth before any approval/token flow ──
    if not _has_valid_x_auth(request):
        await store._audit("invalid_x_auth", client_ip=client_ip,
                           detail=f"path=/{path}")
        logger.warning("Invalid X-Auth from %s for /%s", client_ip, path)
        return JSONResponse(
            {
                "error": "invalid_x_auth",
                "message": "Missing or invalid X-Auth header.",
            },
            status_code=401,
        )

    # ── Remote: check token ─────────────────────────────────
    token = _get_bearer_token(request)

    if token:
        session = await store.validate_token(token)
        if session:
            # Valid token — optionally check IP binding
            claims = tm.validate_token(token, config.jwt_secret, config.jwt_algorithm)
            if claims and claims.get("ip") != client_ip:
                await store._audit("ip_mismatch", session_id=session.id,
                                   client_ip=client_ip,
                                   detail=f"token_ip={claims.get('ip')}")
                logger.warning("IP mismatch: token for %s used from %s",
                               claims.get("ip"), client_ip)
                return JSONResponse(
                    {"error": "ip_mismatch",
                     "message": "Token was issued for a different IP address."},
                    status_code=403,
                )
            return await _proxy_request(request, path)
        else:
            return JSONResponse(
                {"error": "invalid_token",
                 "message": "Token is invalid, expired, or revoked. Request a new connection."},
                status_code=401,
            )

    # ── No token but valid X-Auth: proxy through directly ────
    # X-Auth is a shared secret that already proves authorization.
    # mcp-remote cannot handle custom auth flows (Telegram approval),
    # so we treat a valid X-Auth as sufficient for access.
    logger.info("X-Auth bypass for %s on /%s (no Bearer token)", client_ip, path)

    # Track the connection and alert via Telegram if it is new.
    user_agent = request.headers.get("user-agent", "")
    try:
        conn_id, is_new = await store.track_x_auth_connection(client_ip, user_agent)
        if is_new and bot:
            try:
                msg_id = await bot.send_connection_alert(
                    conn_id, client_ip, user_agent, request.url.path
                )
                if msg_id:
                    await store.set_x_auth_telegram_message_id(conn_id, msg_id)
            except Exception:
                logger.exception("Failed to send X-Auth connection alert")
    except Exception:
        logger.exception("Failed to track X-Auth connection from %s", client_ip)

    return await _proxy_request(request, path)


# ── Proxy implementation ───────────────────────────────────

async def _proxy_request(request: Request, path: str) -> Response:
    """Forward a request to the upstream MCP server."""
    url = f"/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # Build headers (strip auth, hop-by-hop)
    headers = dict(request.headers)
    for h in ("host", "authorization", "connection", "transfer-encoding", "x-auth"):
        headers.pop(h, None)

    body = await request.body()

    # Check if this is an SSE request
    accept = request.headers.get("accept", "")
    is_sse = "text/event-stream" in accept

    method = request.method.upper()

    if is_sse:
        return await _proxy_sse(method, url, headers, body)

    # Standard request
    try:
        upstream_resp = await _upstream_client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        return JSONResponse(
            {"error": "upstream_unavailable", "message": "MCP server is not running."},
            status_code=502,
        )

    # Build response, preserving upstream headers
    resp_headers = dict(upstream_resp.headers)
    for h in ("transfer-encoding", "content-encoding", "content-length"):
        resp_headers.pop(h, None)

    # Explicitly preserve MCP session header for Streamable HTTP clients
    mcp_session_id = upstream_resp.headers.get("Mcp-Session-Id") or upstream_resp.headers.get("mcp-session-id")
    if mcp_session_id:
        resp_headers["Mcp-Session-Id"] = mcp_session_id

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def _proxy_sse(method: str, url: str, headers: dict, body: bytes) -> StreamingResponse:
    """Proxy an SSE (Server-Sent Events) connection with streaming and preserve MCP session headers."""
    try:
        upstream_resp = await _upstream_client.send(
            _upstream_client.build_request(method=method, url=url, headers=headers, content=body),
            stream=True,
        )
    except httpx.ConnectError:
        return StreamingResponse(
            iter([b"event: error\ndata: MCP server unavailable\n\n"]),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    mcp_session_id = upstream_resp.headers.get("Mcp-Session-Id") or upstream_resp.headers.get("mcp-session-id")
    if mcp_session_id:
        response_headers["Mcp-Session-Id"] = mcp_session_id

    async def _stream():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _stream(),
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        headers=response_headers,
    )


# ── Periodic cleanup ───────────────────────────────────────

async def _cleanup_loop():
    """Expire stale sessions every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            approved_expired, pending_expired = await store.cleanup_expired()
            total = approved_expired + pending_expired
            if total:
                logger.info(
                    "Cleanup: %d approved expired, %d pending expired",
                    approved_expired, pending_expired,
                )
                # Notify owner via Telegram about expired sessions
                if bot and approved_expired:
                    await bot.notify(
                        f"⏰ *Session Cleanup*\n\n"
                        f"{approved_expired} approved session(s) expired (TTL reached)."
                    )
                if bot and pending_expired:
                    await bot.notify(
                        f"⏰ *Stale Requests Cleaned*\n\n"
                        f"{pending_expired} pending request(s) expired (unanswered >10 min)."
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Cleanup error")


# ── Entrypoint ─────────────────────────────────────────────

def _suppress_httpx_token_logging():
    """Suppress httpx INFO logs that leak the bot token in URLs.

    httpx logs every request URL at INFO level, which includes the
    Telegram bot token in ``https://api.telegram.org/bot<TOKEN>/...``.
    We raise httpx's log level to WARNING so tokens stay out of journald.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    """Run the auth gateway with uvicorn."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    _suppress_httpx_token_logging()

    cfg = AuthGatewayConfig()
    uvicorn.run(
        "knowledgeforge.auth.gateway:app",
        host=cfg.gateway_host,
        port=cfg.gateway_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
