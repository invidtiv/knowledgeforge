#!/usr/bin/env bash
# kf-status.sh — KnowledgeForge Operational Status Dashboard
# Aggregates health across all KnowledgeForge subsystems into one view.
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED=$'\033[1;31m'  GRN=$'\033[1;32m'  YEL=$'\033[1;33m'
  BLU=$'\033[1;34m'  CYN=$'\033[1;36m'  DIM=$'\033[2m'
  BOLD=$'\033[1m'    RST=$'\033[0m'
else
  RED='' GRN='' YEL='' BLU='' CYN='' DIM='' BOLD='' RST=''
fi

# ── Config ────────────────────────────────────────────────────
API_URL="http://127.0.0.1:8742"
AUTH_GW_URL="http://100.115.155.120:8744"
DATA_DIR="${HOME}/.local/share/knowledgeforge"
QUEUE_FILE="${DATA_DIR}/ingest_queue.json"
CHROMADB_DIR="${DATA_DIR}/chromadb"

# ── Helpers ───────────────────────────────────────────────────
ok()   { echo "${GRN}OK${RST}"; }
warn() { echo "${YEL}WARN${RST}"; }
fail() { echo "${RED}FAIL${RST}"; }

badge() {
  # badge <state> <label>
  case "$1" in
    active|running|ok|healthy) echo "${GRN}$2${RST}" ;;
    activating|auto-restart|warn) echo "${YEL}$2${RST}" ;;
    *) echo "${RED}$2${RST}" ;;
  esac
}

service_status() {
  # service_status <unit> — prints one-line status
  local unit="$1"
  local active sub restarts started
  active=$(systemctl --user show "$unit" --property=ActiveState --value 2>/dev/null || echo "unknown")
  sub=$(systemctl --user show "$unit" --property=SubState --value 2>/dev/null || echo "unknown")
  restarts=$(systemctl --user show "$unit" --property=NRestarts --value 2>/dev/null || echo "?")
  started=$(systemctl --user show "$unit" --property=ExecMainStartTimestamp --value 2>/dev/null || echo "n/a")

  local state_label state_key
  if [[ "$active" == "active" && "$sub" == "running" ]]; then
    state_label="running"; state_key="active"
  elif [[ "$active" == "active" && "$sub" == "waiting" ]]; then
    state_label="active/waiting"; state_key="active"
  elif [[ "$active" == "activating" ]]; then
    state_label="crash-loop ($sub)"; state_key="activating"
  elif [[ "$active" == "inactive" && "${restarts:-0}" =~ ^[0-9]+$ && "$restarts" -gt 0 ]]; then
    state_label="stopped (after ${restarts} restarts)"; state_key="failed"
  elif [[ "$active" == "inactive" ]]; then
    state_label="stopped"; state_key="inactive"
  elif [[ "$active" == "failed" ]]; then
    state_label="failed"; state_key="failed"
  else
    state_label="$active/$sub"; state_key="unknown"
  fi
  local colored_state
  colored_state=$(badge "$state_key" "$state_label")

  printf "  %-44s %s  restarts=%-3s  started=%s\n" "$unit" "$colored_state" "$restarts" "$started"
}

section() {
  echo ""
  echo "${BLU}━━━ $1 ━━━${RST}"
}

# ── Header ────────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYN}  KnowledgeForge Status Dashboard${RST}"
echo "${DIM}  $(date '+%Y-%m-%d %H:%M:%S %Z')${RST}"

# ══════════════════════════════════════════════════════════════
section "SYSTEMD SERVICES"
# ══════════════════════════════════════════════════════════════

service_status knowledgeforge-api.service
service_status knowledgeforge-watcher.service
service_status knowledgeforge-auth-gateway.service
service_status knowledgeforge-mcp.service
service_status knowledgeforge-ingest-queue.service
service_status knowledgeforge-mcp-watchdog.timer

# ══════════════════════════════════════════════════════════════
section "API HEALTH  (${API_URL})"
# ══════════════════════════════════════════════════════════════

api_health=""
api_status="unreachable"
if api_health=$(curl -sf --max-time 3 "${API_URL}/api/v1/health" 2>/dev/null); then
  api_status=$(echo "$api_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
  uptime=$(echo "$api_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uptime_seconds',0))" 2>/dev/null || echo "?")
  echo "  Status:  $(badge ok "$api_status")   Uptime: ${uptime}s"

  # Try to get stats
  if stats=$(curl -sf --max-time 3 "${API_URL}/api/v1/stats" 2>/dev/null); then
    total=$(echo "$stats" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_chunks','?'))" 2>/dev/null || echo "?")
    model=$(echo "$stats" | python3 -c "import sys,json; print(json.load(sys.stdin).get('embedding_model','?'))" 2>/dev/null || echo "?")
    projects_cfg=$(echo "$stats" | python3 -c "import sys,json; print(json.load(sys.stdin).get('code_projects_configured','?'))" 2>/dev/null || echo "?")
    echo "  Chunks:  ${total}   Model: ${model}   Projects configured: ${projects_cfg}"

    # Per-collection counts
    collections=$(echo "$stats" | python3 -c "
import sys, json
cols = json.load(sys.stdin).get('collections', {})
for name, count in sorted(cols.items()):
    print(f'    {name}: {count}')
" 2>/dev/null || true)
    if [[ -n "$collections" ]]; then
      echo "  Collections:"
      echo "$collections"
    fi
  fi
else
  echo "  Status:  $(fail)  — API not responding (${API_URL}/api/v1/health)"
  # Show last journal lines for diagnosis
  echo "  ${DIM}Last log lines:${RST}"
  journalctl --user -u knowledgeforge-api.service --no-pager -n 3 2>/dev/null | sed 's/^/    /' || true
fi

# ══════════════════════════════════════════════════════════════
section "AUTH GATEWAY  (${AUTH_GW_URL})"
# ══════════════════════════════════════════════════════════════

auth_health=""
if auth_health=$(curl -sf --max-time 3 "${AUTH_GW_URL}/auth/health" 2>/dev/null); then
  gw_status=$(echo "$auth_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
  gw_sessions=$(echo "$auth_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('active_sessions',0))" 2>/dev/null || echo "?")
  gw_pending=$(echo "$auth_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pending_requests',0))" 2>/dev/null || echo "?")
  gw_tg=$(echo "$auth_health" | python3 -c "import sys,json; d=json.load(sys.stdin); print('connected' if d.get('telegram_bot') else 'disconnected')" 2>/dev/null || echo "?")
  echo "  Status:  $(badge ok "$gw_status")   Sessions: ${gw_sessions}   Pending: ${gw_pending}   Telegram bot: ${gw_tg}"
else
  echo "  Status:  $(fail)  — Auth gateway not responding"
fi

# ══════════════════════════════════════════════════════════════
section "INGESTION QUEUE"
# ══════════════════════════════════════════════════════════════

if [[ -f "$QUEUE_FILE" ]]; then
  python3 -c "
import json, time, sys

with open('${QUEUE_FILE}') as f:
    data = json.load(f)

projects = data.get('projects', [])
updated = data.get('updated_at', 0)
age_hrs = (time.time() - updated) / 3600 if updated else 0

# Count by status
counts = {}
for p in projects:
    s = p.get('status', 'unknown')
    counts[s] = counts.get(s, 0) + 1

total = len(projects)
done = counts.get('done', 0)
pending = counts.get('pending', 0)
retry = counts.get('retry', 0)
running = counts.get('running', 0)

pct = int(done * 100 / total) if total else 0

# Progress bar
bar_width = 30
filled = int(bar_width * done / total) if total else 0
bar = '#' * filled + '-' * (bar_width - filled)

print(f'  Progress: [{bar}] {pct}% ({done}/{total} projects)')
print(f'  done={done}  pending={pending}  retry={retry}  running={running}')
print(f'  Last updated: {age_hrs:.1f}h ago')
print()

# List retries and running (potential blockers)
blockers = [p for p in projects if p['status'] in ('retry', 'running')]
if blockers:
    print('  Attention-needed projects:')
    for p in blockers:
        err = p.get('last_error', '') or 'no error recorded'
        attempts = p.get('attempts', 0)
        print(f'    {p[\"status\"]:>7}  {p[\"name\"]}  (attempts={attempts})  {err}')
" 2>/dev/null || echo "  ${YEL}Could not parse queue file${RST}"

  # Lock file check
  lock_file="${DATA_DIR}/ingest_queue.lock"
  if [[ -f "$lock_file" ]]; then
    lock_pid=$(python3 -c "import json; d=json.load(open('${lock_file}')); print(d.get('pid','?'))" 2>/dev/null || echo "?")
    if kill -0 "$lock_pid" 2>/dev/null; then
      echo "  Lock: held by PID ${lock_pid} (process alive)"
    else
      echo "  ${YEL}Lock: stale (PID ${lock_pid} not running) — may block queue${RST}"
    fi
  else
    echo "  Lock: none (queue idle)"
  fi
else
  echo "  ${YEL}Queue file not found at ${QUEUE_FILE}${RST}"
fi

# ══════════════════════════════════════════════════════════════
section "SEMANTIC MEMORY"
# ══════════════════════════════════════════════════════════════

if api_health=$(curl -sf --max-time 3 "${API_URL}/api/v1/semantic-records/audit" 2>/dev/null); then
  python3 -c "
import sys, json
data = json.loads('''${api_health}''')
if isinstance(data, dict):
    total = data.get('total_records', '?')
    active = data.get('active', '?')
    archived = data.get('archived', '?')
    stale = data.get('stale_count', data.get('stale', '?'))
    print(f'  Records: {total} total, {active} active, {archived} archived')
    if stale and stale != '?' and int(str(stale)) > 0:
        print(f'  \033[1;33mStale records: {stale} — consider auditing\033[0m')
    gaps = data.get('coverage_gaps', data.get('gaps', []))
    if gaps:
        print(f'  Coverage gaps ({len(gaps)}):')
        for g in gaps[:5]:
            if isinstance(g, str):
                print(f'    - {g}')
            elif isinstance(g, dict):
                print(f\"    - {g.get('project', g.get('name', '?'))}\")
elif isinstance(data, list):
    print(f'  {len(data)} semantic records returned')
else:
    print('  Unexpected response format')
" 2>/dev/null || echo "  Could not parse semantic audit response"
else
  echo "  ${DIM}(API unavailable — cannot query semantic audit)${RST}"
  # Fallback: check chromadb presence
  if [[ -d "$CHROMADB_DIR" ]]; then
    db_size=$(du -sh "$CHROMADB_DIR" 2>/dev/null | cut -f1)
    echo "  ChromaDB on disk: ${db_size}"
  fi
fi

# ══════════════════════════════════════════════════════════════
section "DATA STORE"
# ══════════════════════════════════════════════════════════════

if [[ -d "$DATA_DIR" ]]; then
  total_size=$(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)
  echo "  Data directory: ${DATA_DIR}"
  echo "  Total size: ${total_size}"
  if [[ -d "$CHROMADB_DIR" ]]; then
    chroma_size=$(du -sh "$CHROMADB_DIR" 2>/dev/null | cut -f1)
    echo "  ChromaDB: ${chroma_size}"
  fi
  if [[ -f "${DATA_DIR}/keyword_index.sqlite3" ]]; then
    kw_size=$(du -sh "${DATA_DIR}/keyword_index.sqlite3" 2>/dev/null | cut -f1)
    echo "  Keyword index: ${kw_size}"
  fi
else
  echo "  ${YEL}Data directory not found: ${DATA_DIR}${RST}"
fi

# ══════════════════════════════════════════════════════════════
section "TOP BLOCKERS"
# ══════════════════════════════════════════════════════════════

blockers_found=0

# Check API crash-loop
api_active=$(systemctl --user show knowledgeforge-api.service --property=ActiveState --value 2>/dev/null || echo "unknown")
api_sub=$(systemctl --user show knowledgeforge-api.service --property=SubState --value 2>/dev/null || echo "unknown")
api_restarts=$(systemctl --user show knowledgeforge-api.service --property=NRestarts --value 2>/dev/null || echo "0")
if [[ "$api_active" == "activating" || "$api_active" == "failed" ]] || \
   [[ "$api_active" == "inactive" && "$api_restarts" -gt 0 ]]; then
  echo "  ${RED}[CRITICAL]${RST} knowledgeforge-api is in ${api_active}/${api_sub} (restarts=${api_restarts})"
  echo "            -> Watcher and all API-dependent features are degraded"
  blockers_found=1
fi

# Check watcher
watcher_active=$(systemctl --user show knowledgeforge-watcher.service --property=ActiveState --value 2>/dev/null || echo "unknown")
if [[ "$watcher_active" != "active" ]]; then
  watcher_sub=$(systemctl --user show knowledgeforge-watcher.service --property=SubState --value 2>/dev/null || echo "unknown")
  echo "  ${YEL}[WARNING]${RST}  knowledgeforge-watcher is ${watcher_active}/${watcher_sub}"
  echo "            -> File changes are not being detected"
  blockers_found=1
fi

# Check stale lock
lock_file="${DATA_DIR}/ingest_queue.lock"
if [[ -f "$lock_file" ]]; then
  lock_pid=$(python3 -c "import json; d=json.load(open('${lock_file}')); print(d.get('pid','?'))" 2>/dev/null || echo "?")
  if ! kill -0 "$lock_pid" 2>/dev/null; then
    echo "  ${YEL}[WARNING]${RST}  Stale queue lock (PID ${lock_pid} dead) — run: rm ${lock_file}"
    blockers_found=1
  fi
fi

# Check retry projects
if [[ -f "$QUEUE_FILE" ]]; then
  retry_count=$(python3 -c "
import json
with open('${QUEUE_FILE}') as f:
    data = json.load(f)
print(sum(1 for p in data.get('projects',[]) if p.get('status')=='retry'))
" 2>/dev/null || echo "0")
  if [[ "$retry_count" -gt 0 ]]; then
    echo "  ${YEL}[WARNING]${RST}  ${retry_count} projects in retry state — may need manual re-queue"
    blockers_found=1
  fi
fi

if [[ "$blockers_found" -eq 0 ]]; then
  echo "  ${GRN}No blockers detected.${RST}"
fi

echo ""
