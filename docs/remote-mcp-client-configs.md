# Remote MCP Client Configurations for KnowledgeForge

Tested, known-good configurations for connecting remote MCP clients to the KnowledgeForge MCP server.

Last verified: 2026-04-04

---

## Network Architecture

```
Remote Client
    |
    |  HTTPS (TLS termination by Tailscale Funnel)
    v
https://vmi2916953.tail652dda.ts.net/mcp
    |
    |  Tailscale Funnel -> proxy http://127.0.0.1:8744
    v
Auth Gateway (FastAPI, port 8744, bound to 100.115.155.120)
    |
    |  Reverse proxy (strips auth headers)
    v
MCP Server (FastMCP, Streamable HTTP, port 8743, bound to 127.0.0.1)
```

### Endpoints

| Endpoint | Audience | Auth |
|----------|----------|------|
| `http://127.0.0.1:8743/mcp` | Local processes on the host | None (localhost bypass) |
| `http://100.115.155.120:8744/mcp` | Tailscale-connected devices (private tailnet) | X-Auth + JWT |
| `https://vmi2916953.tail652dda.ts.net/mcp` | Public internet via Tailscale Funnel | X-Auth + JWT |

---

## Authentication Flow

Remote clients must satisfy two requirements:

1. **X-Auth shared secret** -- a static header proving the client is authorized to even request access
2. **Bearer JWT token** -- issued after Telegram approval, valid for 6 hours

### Step-by-step sequence

```
1. Client sends request with header:
     X-Auth: <shared-secret>
   (no Bearer token yet)

2. Gateway returns 401 with:
     { "error": "pending_approval",
       "request_id": "abc123",
       "message": "...poll GET /auth/status/abc123..." }

3. Owner receives Telegram notification with Approve/Deny buttons

4. Client polls:  GET /auth/status/abc123
   Response while waiting: { "status": "pending" }

5. Owner taps "Approve" in Telegram

6. Client polls again:  GET /auth/status/abc123
   Response:
     { "status": "approved",
       "token": "eyJhbG...",
       "expires_at": "2026-04-04T21:30:00Z" }

7. All subsequent requests include BOTH headers:
     X-Auth: <shared-secret>
     Authorization: Bearer eyJhbG...

8. Token expires after 6 hours. Repeat from step 1.
```

### Important details

- The JWT is IP-bound. If your IP changes, the token becomes invalid (403 `ip_mismatch`).
- Only one token issuance per approval. If you miss the poll response, you must request a new connection.
- Rate limit: 30 seconds between unauthenticated requests from the same IP.
- Maximum 10 pending requests at a time.

---

## Client Configurations

### 1. Claude Code (local on the server)

This is the simplest case. The local MCP server on `127.0.0.1:8743` requires no authentication.

**`~/.mcp.json`:**

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8743/mcp",
        "--transport",
        "http-only"
      ],
      "env": {},
      "description": "KnowledgeForge RAG - semantic search across Obsidian vault, codebases, and agent discoveries"
    }
  }
}
```

No headers needed. `mcp-remote` bridges the Streamable HTTP MCP server to Claude Code's stdio transport.

---

### 2. Claude Code (remote, on a Tailscale-connected machine)

Use the tailnet hostname with custom headers for auth. The `mcp-remote` tool supports passing headers via `--header`.

**`~/.mcp.json`:**

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://vmi2916953.tail652dda.ts.net/mcp",
        "--transport",
        "http-only",
        "--header",
        "X-Auth: ${KNOWLEDGEFORGE_X_AUTH_SECRET}",
        "--header",
        "Authorization: Bearer ${KNOWLEDGEFORGE_BEARER_TOKEN}"
      ],
      "env": {
        "KNOWLEDGEFORGE_X_AUTH_SECRET": "<your-x-auth-secret>",
        "KNOWLEDGEFORGE_BEARER_TOKEN": "<jwt-token-after-approval>"
      },
      "description": "KnowledgeForge RAG (remote via Tailscale Funnel)"
    }
  }
}
```

**Bootstrap flow:**

1. Start Claude Code with only the `X-Auth` header set and `KNOWLEDGEFORGE_BEARER_TOKEN` empty
2. The first tool call will trigger the Telegram approval flow
3. Approve via Telegram
4. Retrieve the JWT token from the `/auth/status/{request_id}` response
5. Set `KNOWLEDGEFORGE_BEARER_TOKEN` in the config and restart Claude Code

Alternatively, bootstrap the token manually with curl first (see the "Manual Token Bootstrap" section below).

---

### 3. Claude Desktop

Claude Desktop uses the same `mcpServers` JSON format in its `claude_desktop_config.json`.

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
**Linux**: `~/.config/Claude/claude_desktop_config.json`

#### Local (on the server itself)

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8743/mcp",
        "--transport",
        "http-only"
      ]
    }
  }
}
```

#### Remote (via Tailscale Funnel)

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://vmi2916953.tail652dda.ts.net/mcp",
        "--transport",
        "http-only",
        "--header",
        "X-Auth: <your-x-auth-secret>",
        "--header",
        "Authorization: Bearer <jwt-token>"
      ]
    }
  }
}
```

Note: Claude Desktop does not support `${ENV_VAR}` interpolation in `args`. You must paste the actual secret values directly, or use a wrapper script (see "Wrapper Script" section below).

---

### 4. Windows with Environment Variables

On Windows, environment variable expansion in `args` arrays depends on the client. If your client does not expand env vars in args, use a wrapper script.

**PowerShell wrapper** (`knowledgeforge-mcp.ps1`):

```powershell
$env:NODE_TLS_REJECT_UNAUTHORIZED = "1"
npx -y mcp-remote `
  "https://vmi2916953.tail652dda.ts.net/mcp" `
  --transport http-only `
  --header "X-Auth: $env:KNOWLEDGEFORGE_X_AUTH_SECRET" `
  --header "Authorization: Bearer $env:KNOWLEDGEFORGE_BEARER_TOKEN"
```

Set the environment variables in your system/user environment:

```powershell
[System.Environment]::SetEnvironmentVariable("KNOWLEDGEFORGE_X_AUTH_SECRET", "<secret>", "User")
[System.Environment]::SetEnvironmentVariable("KNOWLEDGEFORGE_BEARER_TOKEN", "<token>", "User")
```

**Claude Desktop config using the wrapper:**

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "powershell",
      "args": ["-File", "C:\\Users\\you\\scripts\\knowledgeforge-mcp.ps1"]
    }
  }
}
```

**Batch file alternative** (`knowledgeforge-mcp.bat`):

```bat
@echo off
npx -y mcp-remote ^
  "https://vmi2916953.tail652dda.ts.net/mcp" ^
  --transport http-only ^
  --header "X-Auth: %KNOWLEDGEFORGE_X_AUTH_SECRET%" ^
  --header "Authorization: Bearer %KNOWLEDGEFORGE_BEARER_TOKEN%"
```

---

### 5. Generic `mcp-remote` (any MCP client)

Any client that speaks stdio to a subprocess can use `mcp-remote` as the bridge:

```bash
npx -y mcp-remote \
  "https://vmi2916953.tail652dda.ts.net/mcp" \
  --transport http-only \
  --header "X-Auth: <secret>" \
  --header "Authorization: Bearer <jwt>"
```

The `--transport http-only` flag is required because the gateway serves Streamable HTTP, not SSE.

---

## Manual Token Bootstrap (curl)

Use this to obtain a JWT token before configuring your MCP client.

### Step 1: Trigger the approval request

```bash
curl -s -H "X-Auth: <your-x-auth-secret>" \
  https://vmi2916953.tail652dda.ts.net/mcp
```

Response:

```json
{
  "error": "pending_approval",
  "request_id": "abc123-def456",
  "message": "Connection pending owner approval via Telegram. ..."
}
```

### Step 2: Approve via Telegram

The server owner receives a Telegram message with Approve/Deny buttons. Tap Approve.

### Step 3: Poll for the token

```bash
curl -s -H "X-Auth: <your-x-auth-secret>" \
  https://vmi2916953.tail652dda.ts.net/auth/status/abc123-def456
```

Response (after approval):

```json
{
  "status": "approved",
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_at": "2026-04-04T21:30:00Z",
  "message": "Connection approved. Use this token as Bearer auth."
}
```

Save the `token` value. It is only returned once.

### Step 4: Verify the token works

```bash
curl -s \
  -H "X-Auth: <your-x-auth-secret>" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  https://vmi2916953.tail652dda.ts.net/auth/health
```

Expected: `{"status": "healthy", ...}`

---

## Troubleshooting

### "Missing or invalid X-Auth header" (401)

- The `X-Auth` header value does not match the server's `KNOWLEDGEFORGE_AUTH_X_AUTH_SECRET`.
- Check for trailing whitespace or newlines in your secret value.
- The header name is case-insensitive (`X-Auth`, `x-auth` both work).

### "Token is invalid, expired, or revoked" (401)

- The JWT has expired (6-hour TTL). Request a new approval.
- The token was already consumed from the status endpoint by another poll. Request a new connection.

### "Token was issued for a different IP address" (403)

- The JWT is IP-bound. If your public IP changed (VPN toggle, network switch), the token is invalid.
- Request a new approval from the new IP.

### "rate_limited" (429)

- Wait 30 seconds between unauthenticated requests from the same IP.

### "MCP server is not running" (502)

- The upstream MCP server (`knowledgeforge-mcp.service`) is down.
- On the server: `systemctl --user status knowledgeforge-mcp.service`
- Restart: `systemctl --user restart knowledgeforge-mcp.service`

### "Remote auth is not configured (no Telegram bot token)" (503)

- The auth gateway cannot send Telegram approval requests.
- On the server: check `knowledgeforge-auth-gateway.service` logs for bot token errors.

### Connection hangs / no response

- Verify Tailscale Funnel is active: `tailscale funnel status`
- Verify the gateway is listening: `curl http://100.115.155.120:8744/auth/health`
- Verify the MCP server is listening: `curl http://127.0.0.1:8743/mcp` (should return an MCP error, not a connection refused)

### `mcp-remote` crashes with "Invalid URL"

- Ensure the URL includes the `/mcp` path.
- Ensure `--transport http-only` is passed (not `--transport sse`).

### Claude Desktop does not expand environment variables

- Claude Desktop passes `args` literally. Use a wrapper script (PowerShell/batch on Windows, shell script on Linux/macOS) that reads env vars and passes them as arguments.

### Token already issued / lost the token

- The JWT is returned exactly once from `/auth/status/{request_id}`. If you missed it, trigger a new approval flow from step 1.

---

## Gateway Health Check

No auth required for the health endpoint:

```bash
# Via Tailscale Funnel (public)
curl https://vmi2916953.tail652dda.ts.net/auth/health

# Via Tailscale IP (private tailnet)
curl http://100.115.155.120:8744/auth/health

# Local
curl http://127.0.0.1:8744/auth/health
```

Response:

```json
{
  "status": "healthy",
  "active_sessions": 1,
  "pending_requests": 0,
  "telegram_bot": true
}
```

---

## Service Architecture Reference

| Service | Systemd Unit | Bind Address | Transport |
|---------|-------------|--------------|-----------|
| MCP Server | `knowledgeforge-mcp.service` | `127.0.0.1:8743` | Streamable HTTP |
| Auth Gateway | `knowledgeforge-auth-gateway.service` | `100.115.155.120:8744` | HTTP (reverse proxy) |
| REST API | `knowledgeforge-api.service` | `127.0.0.1:8742` | HTTP/JSON |
| Watcher | `knowledgeforge-watcher.service` | N/A | Background daemon |

Tailscale Funnel routes `https://vmi2916953.tail652dda.ts.net` to `http://127.0.0.1:8744` (the auth gateway).
