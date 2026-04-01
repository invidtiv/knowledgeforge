# KnowledgeForge Operations Runbook

This runbook covers day-2 operations for a shared MCP deployment:

- One always-on KnowledgeForge MCP server process
- Multiple clients connecting through `mcp-remote`
- A watchdog timer that removes stale duplicate MCP processes

## Service Topology

Recommended user services:

- `knowledgeforge-api.service` -> REST API (`127.0.0.1:8742`)
- `knowledgeforge-watcher.service` -> live file sync watcher
- `knowledgeforge-mcp.service` -> shared MCP endpoint (`127.0.0.1:8743/mcp`)
- `knowledgeforge-mcp-watchdog.timer` -> runs watchdog every 2 minutes

## Enable and Start

```bash
systemctl --user daemon-reload
systemctl --user enable --now knowledgeforge-api.service
systemctl --user enable --now knowledgeforge-watcher.service
systemctl --user enable --now knowledgeforge-mcp.service
systemctl --user enable --now knowledgeforge-mcp-watchdog.timer
```

## Quick Health Check

```bash
systemctl --user is-active knowledgeforge-api.service
systemctl --user is-active knowledgeforge-watcher.service
systemctl --user is-active knowledgeforge-mcp.service
systemctl --user is-active knowledgeforge-mcp-watchdog.timer
```

Check listeners:

```bash
ss -lntp | rg -n ':8742|:8743'
```

Expected:

- `:8742` bound by KnowledgeForge REST API process
- `:8743` bound by KnowledgeForge MCP shared server process

## Verify Single MCP Process

```bash
ps -eo pid,ppid,etime,rss,cmd | rg 'knowledgeforge.interfaces.mcp_server'
```

Expected in shared mode:

- Exactly one heavy Python process managed by `knowledgeforge-mcp.service`
- Small per-client `mcp-remote` processes are normal

Memory summary:

```bash
ps -eo rss,cmd | awk '/knowledgeforge\\.interfaces\\.mcp_server/ {sum+=$1; n++} END {printf("count=%d total_rss_gb=%.2f\n", n, sum/1024/1024)}'
```

## Client Configuration Sanity

All MCP clients should use:

```json
{
  "command": "npx",
  "args": ["-y", "mcp-remote", "http://127.0.0.1:8743/mcp"]
}
```

If a client is still configured with:

```json
{
  "command": "/home/bsdev/knowledgeforge/.venv/bin/python",
  "args": ["-m", "knowledgeforge.interfaces.mcp_server"]
}
```

it will spawn a separate Python MCP process per client/session.

## Watchdog Operations

Run watchdog immediately:

```bash
systemctl --user start knowledgeforge-mcp-watchdog.service
```

Show timer schedule:

```bash
systemctl --user list-timers --all | rg knowledgeforge-mcp-watchdog
```

Watchdog logs:

```bash
journalctl --user -u knowledgeforge-mcp-watchdog.service -n 100 --no-pager
```

## Common Troubleshooting

### 1) Duplicate MCP Python processes appear

1. Run watchdog once:

```bash
systemctl --user start knowledgeforge-mcp-watchdog.service
```

2. Re-check process count:

```bash
ps -eo pid,cmd | rg 'knowledgeforge.interfaces.mcp_server'
```

3. If duplicates keep returning, one or more clients are still configured for direct stdio launch. Update them to `mcp-remote`.

### 2) Shared MCP endpoint unavailable

Check service status:

```bash
systemctl --user status knowledgeforge-mcp.service --no-pager -n 80
```

Restart service:

```bash
systemctl --user restart knowledgeforge-mcp.service
```

### 3) High memory or swap pressure

Check memory and swap:

```bash
free -h
```

Check top KnowledgeForge memory consumers:

```bash
ps -eo pid,rss,cmd | rg 'knowledgeforge|mcp-remote' | sort -k2 -nr | head -n 20
```

If needed, restart noisy components:

```bash
systemctl --user restart knowledgeforge-watcher.service
systemctl --user restart knowledgeforge-mcp.service
```

## Emergency Cleanup (Safe)

Keep the shared service PID, terminate stale direct MCP server processes:

```bash
MAIN_PID="$(systemctl --user show knowledgeforge-mcp.service -p MainPID --value)"
ps -eo pid,cmd | awk '/knowledgeforge\\.interfaces\\.mcp_server/ {print $1}' | awk -v main="$MAIN_PID" '$1!=main' | xargs -r kill
```

Then verify:

```bash
ps -eo pid,etime,rss,cmd | rg 'knowledgeforge.interfaces.mcp_server'
```

## Important Paths

- Shared MCP service unit: `~/.config/systemd/user/knowledgeforge-mcp.service`
- Watchdog service unit: `~/.config/systemd/user/knowledgeforge-mcp-watchdog.service`
- Watchdog timer unit: `~/.config/systemd/user/knowledgeforge-mcp-watchdog.timer`
- Watchdog script: `~/.local/bin/knowledgeforge-mcp-watchdog.sh`
- MCP server code: `src/knowledgeforge/interfaces/mcp_server.py`

