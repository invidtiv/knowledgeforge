# KnowledgeForge Tailscale/Funnel Auth Gateway

## Purpose
Expose KnowledgeForge MCP safely to remote clients through a Tailscale/Funnel-facing auth gateway while keeping the localhost MCP backend private.

## Network model

### Private backend MCP
- `127.0.0.1:8743`
- local-only
- not intended for direct remote exposure

### Remote auth gateway
- `0.0.0.0:8744` (listens on all interfaces)
- Tailscale Funnel routes `https://vmi2916953.tail652dda.ts.net/` to `http://127.0.0.1:8744`
- proxies authenticated requests to `http://127.0.0.1:8743`

**Critical**: The gateway MUST bind to `0.0.0.0` (not `100.115.155.120`).
Tailscale Funnel connects to `127.0.0.1`, so binding only to the Tailscale IP
causes 502 errors on all Funnel traffic.

## Security model

### Funnel local-bypass protection
Tailscale Funnel traffic arrives from `127.0.0.1` but carries the header
`Tailscale-Funnel: true`. The gateway detects this header and treats the
request as remote (requiring full auth), preventing public internet traffic
from exploiting the local bypass.

### Auth requirements
Remote requests must satisfy **both**:
1. valid `X-Auth` shared secret
2. Telegram approval granting a temporary JWT session

### Approval duration
- 6 hours

### Telegram destination
- owner direct chat only (`1082729605`)

## Request flow
1. Remote client connects to gateway.
2. Gateway checks `X-Auth`.
3. If `X-Auth` is missing/invalid -> deny immediately.
4. If `X-Auth` is valid but no Bearer token is present:
   - create pending request
   - send Telegram approval request
   - return pending response with `/auth/status/{request_id}` polling path
5. When approved in Telegram:
   - issue JWT token
   - client polls `/auth/status/{request_id}` and receives token
6. Subsequent requests must include:
   - `X-Auth: <shared-secret>`
   - `Authorization: Bearer <issued-jwt>`

## Systemd service
Service file:
- `~/.config/systemd/user/knowledgeforge-auth-gateway.service`

Important variable:
- `KNOWLEDGEFORGE_AUTH_X_AUTH_SECRET`

Set this to a real secret before exposing the service.

## Telegram token handling
Bot token is loaded from:
- `/home/bsdev/knowledgeforge/bot_token`

Do not print or embed the token in repo files.

## Tailscale Funnel configuration

Current Funnel config (set once, persists across reboots):
```
tailscale funnel --bg 8744
```

Verify with: `tailscale funnel status`

Expected output:
```
https://vmi2916953.tail652dda.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8744
```

## Superseded service (must stay disabled)

`knowledgeforge-mcp-tailscale.service` was the raw MCP server on port 8744
before the auth gateway was created. It is **disabled** and must never be
re-enabled -- it would conflict on the same port and expose MCP without auth.

## Setup / recovery runbook

1. Ensure secrets are in `~/.config/knowledgeforge/secrets.env`:
   ```
   KNOWLEDGEFORGE_AUTH_X_AUTH_SECRET=<strong-secret>
   KNOWLEDGEFORGE_AUTH_TELEGRAM_OWNER_CHAT_ID=1082729605
   ```
2. Verify the systemd unit has `GATEWAY_HOST=0.0.0.0` (NOT the Tailscale IP):
   ```bash
   grep GATEWAY_HOST ~/.config/systemd/user/knowledgeforge-auth-gateway.service
   # Must show: Environment=KNOWLEDGEFORGE_AUTH_GATEWAY_HOST=0.0.0.0
   ```
3. Reload and start services:
   ```bash
   systemctl --user daemon-reload
   systemctl --user restart knowledgeforge-mcp.service           # upstream on :8743
   systemctl --user restart knowledgeforge-auth-gateway.service   # gateway on :8744
   ```
4. Verify bind addresses:
   ```bash
   ss -tlnp | grep -E '8743|8744'
   # 8743 should be on 127.0.0.1 (local MCP)
   # 8744 should be on 0.0.0.0 (gateway)
   ```
5. Verify Funnel is active:
   ```bash
   tailscale funnel status
   ```
6. Test from public internet (force public DNS, not tailnet):
   ```bash
   PUBLIC_IP=$(dig +short vmi2916953.tail652dda.ts.net @1.1.1.1 | head -1)
   curl -s --resolve "vmi2916953.tail652dda.ts.net:443:$PUBLIC_IP" \
     https://vmi2916953.tail652dda.ts.net/auth/health
   # Expected: {"status":"healthy",...}
   ```
7. Verify auth enforcement:
   ```bash
   curl -s --resolve "vmi2916953.tail652dda.ts.net:443:$PUBLIC_IP" \
     https://vmi2916953.tail652dda.ts.net/api/v1/search
   # Expected: {"error":"invalid_x_auth",...}
   ```

### Common 502 causes
| Symptom | Cause | Fix |
|---------|-------|-----|
| All Funnel requests return 502 | Gateway binds to Tailscale IP instead of 0.0.0.0 | Fix `GATEWAY_HOST` in systemd unit, daemon-reload, restart |
| All Funnel requests return 502 | Gateway not running | `systemctl --user start knowledgeforge-auth-gateway` |
| Intermittent 502 after restart | Funnel connection cache to old backend | Self-resolves within ~10 seconds |
| Port 8744 conflict | `knowledgeforge-mcp-tailscale.service` running | `systemctl --user stop knowledgeforge-mcp-tailscale` |
| Auth bypassed on Funnel traffic | Missing Tailscale-Funnel header check | Ensure gateway code checks `Tailscale-Funnel: true` header |
| First request after idle returns 000/SSL error | Tailscale Funnel relay warmup after idle | Normal behavior; subsequent requests succeed. Clients should retry once. |

## Operator commands (Telegram bot)

| Command | Description |
|---------|-------------|
| `/sessions` | List active approved sessions (IP, TTL remaining) |
| `/pending` | List pending approval requests awaiting decision |
| `/revoke <id>` | Revoke a specific session by request\_id or session\_id |
| `/revokeall` | Revoke all active sessions immediately |
| `/audit [N]` | Show last N audit log entries (default 15, max 50) |
| `/status` | Gateway health summary (active/pending counts) |
| `/help` | Show available commands |

Approval/deny buttons appear inline on each access request notification.

## HTTP management endpoints (local-only)

| Endpoint | Description |
|----------|-------------|
| `GET /auth/health` | Health check (active sessions, pending, bot status) |
| `GET /auth/audit?limit=N` | Recent audit log entries (max 100) |
| `GET /auth/sessions` | List active sessions with TTL |
| `GET /auth/status/{request_id}` | Poll approval status (used by remote clients) |

`/auth/audit` and `/auth/sessions` are restricted to local IPs only.

## Audit log

All security-relevant events are written to an append-only `audit_log` SQLite table:

| Event | Meaning |
|-------|---------|
| `request_created` | New pending approval request |
| `approved` | Session approved via Telegram |
| `denied` | Session denied via Telegram |
| `revoked` | Session manually revoked |
| `session_expired` | Approved session TTL reached |
| `pending_expired` | Pending request unanswered >10 min |
| `invalid_x_auth` | Remote request with wrong/missing X-Auth |
| `ip_mismatch` | Token used from different IP than issued |

## Session cleanup

- Runs every 60 seconds automatically
- Approved sessions expire after 6 hours (JWT TTL)
- Pending requests expire after 10 minutes if unanswered
- Owner receives Telegram notification when sessions/requests expire
- JWT secret is persisted to `~/.local/share/knowledgeforge/jwt_secret` (survives restarts)

## Secret rotation

1. Edit `~/.config/knowledgeforge/secrets.env` with the new `KNOWLEDGEFORGE_AUTH_X_AUTH_SECRET`
2. `systemctl --user daemon-reload`
3. `systemctl --user restart knowledgeforge-auth-gateway`
4. Update all remote clients with the new X-Auth value
5. To rotate the JWT secret: delete `~/.local/share/knowledgeforge/jwt_secret` and restart (invalidates all active tokens)

## Client requirement
Remote client must send both:
- `X-Auth`
- `Authorization: Bearer <jwt>` after approval

## Safety note
This setup is meaningfully safer than exposing raw MCP, but it still depends on:
- strong X-Auth secret management (stored in `secrets.env` with 600 permissions)
- Telegram bot security
- short-lived approvals (6h)
- reviewing who gets approved
- httpx log suppression (bot token no longer appears in journald)
- X-Auth header stripped before forwarding to upstream MCP
- Tailscale-Funnel header detection prevents local bypass abuse
