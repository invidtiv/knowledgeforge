"""MCP Authentication Gateway — FastAPI reverse proxy.

Sits in front of the MCP server (upstream on 127.0.0.1:8743).
Local connections bypass auth.  Remote connections require
Telegram-approved JWT tokens (1-hour TTL).

Run directly:  python -m knowledgeforge.auth.gateway
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

    config = AuthGatewayConfig()
    config.ensure_jwt_secret()

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


def _is_local(ip: str) -> bool:
    """Check if an IP is a local/loopback address."""
    return ip in config.local_ips or ip.startswith("127.") or ip == "::1"


def _get_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


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

    # ── Local bypass ────────────────────────────────────────
    if _is_local(client_ip):
        return await _proxy_request(request, path)

    # ── Remote: check token ─────────────────────────────────
    token = _get_bearer_token(request)

    if token:
        session = await store.validate_token(token)
        if session:
            # Valid token — optionally check IP binding
            claims = tm.validate_token(token, config.jwt_secret, config.jwt_algorithm)
            if claims and claims.get("ip") != client_ip:
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

    # ── No token: initiate approval flow ────────────────────

    # Rate limiting
    if await store.recent_request_from_ip(client_ip, config.request_cooldown_seconds):
        return JSONResponse(
            {"error": "rate_limited",
             "message": f"Please wait {config.request_cooldown_seconds}s between requests."},
            status_code=429,
        )

    if await store.total_pending() >= config.max_pending_requests:
        return JSONResponse(
            {"error": "too_many_pending",
             "message": "Too many pending requests. Try again later."},
            status_code=429,
        )

    if not bot:
        return JSONResponse(
            {"error": "no_approval_channel",
             "message": "Remote auth is not configured (no Telegram bot token)."},
            status_code=503,
        )

    # Create pending session and send Telegram notification
    user_agent = request.headers.get("user-agent", "")
    session = await store.create_pending(client_ip, user_agent, f"/{path}")
    await bot.send_approval_request(session)

    return JSONResponse(
        {
            "error": "pending_approval",
            "request_id": session.request_id,
            "message": (
                "Connection pending owner approval via Telegram. "
                f"Poll GET /auth/status/{session.request_id} for updates."
            ),
        },
        status_code=401,
    )


# ── Proxy implementation ───────────────────────────────────

async def _proxy_request(request: Request, path: str) -> Response:
    """Forward a request to the upstream MCP server."""
    url = f"/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # Build headers (strip auth, hop-by-hop)
    headers = dict(request.headers)
    for h in ("host", "authorization", "connection", "transfer-encoding"):
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

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def _proxy_sse(method: str, url: str, headers: dict, body: bytes) -> StreamingResponse:
    """Proxy an SSE (Server-Sent Events) connection with streaming."""

    async def _stream():
        try:
            async with _upstream_client.stream(
                method=method, url=url, headers=headers, content=body,
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except httpx.ConnectError:
            yield b"event: error\ndata: MCP server unavailable\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Periodic cleanup ───────────────────────────────────────

async def _cleanup_loop():
    """Expire stale sessions every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            count = await store.cleanup_expired()
            if count:
                logger.info("Cleaned up %d expired sessions", count)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Cleanup error")


# ── Entrypoint ─────────────────────────────────────────────

def main():
    """Run the auth gateway with uvicorn."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    cfg = AuthGatewayConfig()
    uvicorn.run(
        "knowledgeforge.auth.gateway:app",
        host=cfg.gateway_host,
        port=cfg.gateway_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
