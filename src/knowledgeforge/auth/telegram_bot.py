"""Telegram bot for MCP auth approval using raw Bot API via httpx.

Handles:
  - Sending approval requests with inline Approve/Deny buttons
  - Processing button callbacks
  - /sessions, /revoke, /revokeall, /status commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from .models import Session
from .session_store import SessionStore

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramAuthBot:
    """Lightweight Telegram bot for connection approval."""

    def __init__(
        self,
        bot_token: str,
        owner_chat_id: str,
        session_store: SessionStore,
    ):
        self.token = bot_token
        self.owner_chat_id = owner_chat_id
        self.store = session_store
        self._client: httpx.AsyncClient | None = None
        self._offset: int = 0
        self._running = False

    # ── HTTP helpers ────────────────────────────────────────

    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    async def _request(self, method: str, **kwargs) -> dict | None:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=60)
        try:
            resp = await self._client.post(self._url(method), json=kwargs)
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API error: %s %s", method, data)
                return None
            return data.get("result")
        except Exception:
            logger.exception("Telegram request failed: %s", method)
            return None

    # ── Send approval request ───────────────────────────────

    async def send_approval_request(self, session: Session) -> int | None:
        """Send an inline-button message to the owner.  Returns message_id."""
        ts = datetime.fromtimestamp(session.created_at, tz=timezone.utc)
        text = (
            f"🔐 *KnowledgeForge MCP Access Request*\n\n"
            f"*IP:* `{session.client_ip}`\n"
            f"*User-Agent:* `{_trunc(session.user_agent, 80)}`\n"
            f"*Path:* `{session.requested_path}`\n"
            f"*Time:* {ts:%Y-%m-%d %H:%M:%S UTC}\n"
            f"*Request ID:* `{session.request_id}`\n"
            f"*Grant:* 6 hours"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve (6h)", "callback_data": f"approve:{session.request_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{session.request_id}"},
            ]]
        }

        result = await self._request(
            "sendMessage",
            chat_id=self.owner_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        if result:
            msg_id = result["message_id"]
            # Store the telegram message ID on the session
            if self.store._db:
                await self.store._db.execute(
                    "UPDATE sessions SET telegram_message_id=? WHERE id=?",
                    (msg_id, session.id),
                )
                await self.store._db.commit()
            return msg_id
        return None

    # ── Edit message after decision ─────────────────────────

    async def _edit_message(self, message_id: int, text: str) -> None:
        await self._request(
            "editMessageText",
            chat_id=self.owner_chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
        )

    async def _mark_approved(self, session: Session) -> None:
        if not session.telegram_message_id:
            return
        ts = datetime.fromtimestamp(session.approved_at or time.time(), tz=timezone.utc)
        text = (
            f"✅ *KnowledgeForge MCP Access APPROVED*\n\n"
            f"*IP:* `{session.client_ip}`\n"
            f"*Request ID:* `{session.request_id}`\n"
            f"*Approved at:* {ts:%H:%M:%S UTC}\n"
            f"*Expires:* 6 hours"
        )
        await self._edit_message(session.telegram_message_id, text)

    async def _mark_denied(self, session: Session) -> None:
        if not session.telegram_message_id:
            return
        text = (
            f"❌ *MCP Connection DENIED*\n\n"
            f"*IP:* `{session.client_ip}`\n"
            f"*Request ID:* `{session.request_id}`"
        )
        await self._edit_message(session.telegram_message_id, text)

    # ── X-Auth connection alert ─────────────────────────────

    async def send_connection_alert(
        self,
        connection_id: str,
        client_ip: str,
        user_agent: str,
        path: str,
    ) -> int | None:
        """Send a new-connection alert with a Terminate button.

        Returns the Telegram message_id, or None on failure.
        """
        text = (
            f"🔌 *New MCP Connection*\n\n"
            f"*IP:* `{client_ip}`\n"
            f"*Agent:* `{_trunc(user_agent or '-', 80)}`\n"
            f"*Path:* `{path}`\n"
            f"*ID:* `{connection_id}`"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "❌ Terminate", "callback_data": f"terminate:{connection_id}"},
            ]]
        }
        result = await self._request(
            "sendMessage",
            chat_id=self.owner_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        if result:
            return result["message_id"]
        return None

    # ── Notification helpers ────────────────────────────────

    async def notify(self, text: str) -> None:
        """Send a plain notification to the owner."""
        await self._request(
            "sendMessage",
            chat_id=self.owner_chat_id,
            text=text,
            parse_mode="Markdown",
        )

    # ── Polling loop ────────────────────────────────────────

    async def start_polling(self) -> None:
        """Start the long-polling loop as a background task."""
        self._running = True
        logger.info("Telegram auth bot polling started")

        # Register commands
        await self._request(
            "setMyCommands",
            commands=[
                {"command": "sessions", "description": "List active MCP sessions"},
                {"command": "pending", "description": "List pending approval requests"},
                {"command": "revoke", "description": "Revoke a session: /revoke <id>"},
                {"command": "revokeall", "description": "Revoke all active sessions"},
                {"command": "audit", "description": "Recent audit log: /audit [count]"},
                {"command": "connections", "description": "List active X-Auth connections"},
                {"command": "status", "description": "Gateway health check"},
                {"command": "help", "description": "Show available commands"},
            ],
        )

        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Polling error")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _poll_once(self) -> None:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=60)

        try:
            resp = await self._client.post(
                self._url("getUpdates"),
                json={"offset": self._offset, "timeout": 30},
                timeout=40,
            )
            data = resp.json()
        except httpx.TimeoutException:
            return

        if not data.get("ok"):
            logger.warning("getUpdates failed: %s", data)
            await asyncio.sleep(2)
            return

        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            await self._handle_update(update)

    async def _handle_update(self, update: dict) -> None:
        # Callback queries (button presses)
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        # Text commands
        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Only accept commands from the owner
        if chat_id != self.owner_chat_id:
            return

        if text.startswith("/sessions"):
            await self._cmd_sessions()
        elif text.startswith("/pending"):
            await self._cmd_pending()
        elif text.startswith("/revoke "):
            arg = text.split(maxsplit=1)[1].strip()
            await self._cmd_revoke(arg)
        elif text.startswith("/revokeall"):
            await self._cmd_revokeall()
        elif text.startswith("/audit"):
            parts = text.split(maxsplit=1)
            count = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 15
            await self._cmd_audit(count)
        elif text.startswith("/connections"):
            await self._cmd_connections()
        elif text.startswith("/status"):
            await self._cmd_status()
        elif text.startswith("/help"):
            await self._cmd_help()

    # ── Callback handler ────────────────────────────────────

    async def _handle_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        cq_id = cq.get("id")
        from_id = str(cq.get("from", {}).get("id", ""))

        # Only owner can approve
        if from_id != self.owner_chat_id:
            await self._answer_callback(cq_id, "Not authorized")
            return

        if data.startswith("approve:"):
            request_id = data.split(":", 1)[1]
            try:
                session, _token = await self.store.approve(request_id)
                await self._mark_approved(session)
                await self._answer_callback(cq_id, "Approved for 6 hours")
            except ValueError as e:
                await self._answer_callback(cq_id, str(e))

        elif data.startswith("deny:"):
            request_id = data.split(":", 1)[1]
            try:
                session = await self.store.deny(request_id)
                await self._mark_denied(session)
                await self._answer_callback(cq_id, "Denied")
            except ValueError as e:
                await self._answer_callback(cq_id, str(e))

        elif data.startswith("terminate:"):
            connection_id = data.split(":", 1)[1]
            found = await self.store.terminate_x_auth_connection(connection_id)
            if found:
                msg_id = cq.get("message", {}).get("message_id")
                if msg_id:
                    await self._edit_message(
                        msg_id,
                        f"🚫 Connection `{connection_id}` terminated by operator",
                    )
                await self._answer_callback(cq_id, f"Terminated {connection_id}")
            else:
                await self._answer_callback(cq_id, f"Connection {connection_id} not found")

    async def _answer_callback(self, cq_id: str, text: str) -> None:
        await self._request("answerCallbackQuery", callback_query_id=cq_id, text=text)

    # ── Bot commands ────────────────────────────────────────

    async def _cmd_sessions(self) -> None:
        active = await self.store.list_active()
        if not active:
            await self.notify("No active MCP sessions.")
            return

        lines = [f"*Active MCP Sessions ({len(active)}):*\n"]
        for s in active:
            mins = s.remaining_seconds // 60
            lines.append(
                f"• `{s.request_id}` — `{s.client_ip}` "
                f"({mins}m remaining)\n"
                f"  UA: `{_trunc(s.user_agent, 50)}`"
            )
        await self.notify("\n".join(lines))

    async def _cmd_revoke(self, arg: str) -> None:
        # arg can be a request_id or session_id
        session = await self.store.get_by_request_id(arg)
        if session:
            ok = await self.store.revoke(session.id)
        else:
            ok = await self.store.revoke(arg)

        if ok:
            await self.notify(f"✅ Session `{arg}` revoked.")
        else:
            await self.notify(f"❌ No active session found for `{arg}`.")

    async def _cmd_revokeall(self) -> None:
        count = await self.store.revoke_all()
        await self.notify(f"✅ Revoked {count} active session(s).")

    async def _cmd_pending(self) -> None:
        pending = await self.store.list_pending()
        if not pending:
            await self.notify("No pending approval requests.")
            return

        lines = [f"*Pending Requests ({len(pending)}):*\n"]
        for s in pending:
            age_s = int(time.time() - s.created_at)
            age_m = age_s // 60
            ts = datetime.fromtimestamp(s.created_at, tz=timezone.utc)
            lines.append(
                f"• `{s.request_id}` — `{s.client_ip}`\n"
                f"  Path: `{s.requested_path}`\n"
                f"  Age: {age_m}m {age_s % 60}s | {ts:%H:%M:%S UTC}"
            )
        await self.notify("\n".join(lines))

    async def _cmd_audit(self, count: int = 15) -> None:
        count = min(count, 50)  # cap at 50
        entries = await self.store.recent_audit(limit=count)
        if not entries:
            await self.notify("No audit log entries.")
            return

        lines = [f"*Audit Log (last {len(entries)}):*\n"]
        for e in entries:
            ts = datetime.fromtimestamp(e["ts"], tz=timezone.utc)
            rid = e["request_id"] or "-"
            ip = e["client_ip"] or "-"
            detail = e["detail"] or ""
            lines.append(
                f"• `{ts:%m-%d %H:%M:%S}` *{e['event']}* "
                f"rid=`{rid}` ip=`{ip}` {detail}"
            )
        await self.notify("\n".join(lines))

    async def _cmd_connections(self) -> None:
        conns = await self.store.get_active_x_auth_connections()
        if not conns:
            await self.notify("No active X-Auth connections.")
            return

        lines = [f"*Active X-Auth Connections ({len(conns)}):*\n"]
        for c in conns:
            first = datetime.fromtimestamp(c["first_seen"], tz=timezone.utc)
            lines.append(
                f"• `{c['id']}` — `{c['client_ip']}`\n"
                f"  Requests: {c['request_count']} | First: {first:%H:%M:%S UTC}\n"
                f"  UA: `{_trunc(c['user_agent'] or '-', 50)}`"
            )
        await self.notify("\n".join(lines))

    async def _cmd_status(self) -> None:
        active = await self.store.list_active()
        pending = await self.store.total_pending()
        x_auth_conns = await self.store.get_active_x_auth_connections()
        await self.notify(
            f"*MCP Auth Gateway Status*\n\n"
            f"Active sessions: {len(active)}\n"
            f"Pending requests: {pending}\n"
            f"Active X-Auth connections: {len(x_auth_conns)}\n"
            f"Uptime: running"
        )

    async def _cmd_help(self) -> None:
        await self.notify(
            "*MCP Auth Gateway Commands*\n\n"
            "/sessions — List active approved sessions\n"
            "/pending — List pending approval requests\n"
            "/revoke <id> — Revoke a specific session (request\\_id or session\\_id)\n"
            "/revokeall — Revoke all active sessions\n"
            "/audit \\[N] — Show last N audit log entries (default 15, max 50)\n"
            "/connections — List active X-Auth connections\n"
            "/status — Gateway health summary\n"
            "/help — This message"
        )


def _trunc(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s
