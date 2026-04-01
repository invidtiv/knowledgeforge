# KnowledgeForge Tailscale/Funnel Auth Gateway

## Purpose
Expose KnowledgeForge MCP safely to remote clients through a Tailscale/Funnel-facing auth gateway while keeping the localhost MCP backend private.

## Network model

### Private backend MCP
- `127.0.0.1:8743`
- local-only
- not intended for direct remote exposure

### Remote auth gateway
- `100.115.155.120:8744`
- intended Tailscale/Funnel-facing entrypoint
- proxies to `http://127.0.0.1:8743`

## Security model
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

## Suggested rollout
1. Set a strong `KNOWLEDGEFORGE_AUTH_X_AUTH_SECRET` in the systemd unit.
2. `systemctl --user daemon-reload`
3. `systemctl --user enable --now knowledgeforge-auth-gateway.service`
4. Verify listener on `100.115.155.120:8744`
5. Only then point Tailscale Funnel at the gateway.
6. Do not expose raw MCP directly.

## Client requirement
Remote client must send both:
- `X-Auth`
- `Authorization: Bearer <jwt>` after approval

## Safety note
This setup is meaningfully safer than exposing raw MCP, but it still depends on:
- strong X-Auth secret management
- Telegram bot security
- short-lived approvals
- reviewing who gets approved
