# Historical Session Ingestion Handoff

Date: 2026-04-24

Audience: Codex agent with access to the KnowledgeForge MCP server, local shell, and OpenBrain/OB1.

## Goal

Ingest historical AI sessions into KnowledgeForge without poisoning retrieval.

The target architecture is:

1. Raw archive: keep original session files untouched for traceability.
2. Low-trust episodic search: index supported session exchanges into the `conversations` collection.
3. Structured memory: extract atomic `memory_cards` for durable decisions, constraints, failed attempts, fixes, TODOs, contracts, and user rules.

Do not treat historical sessions as current truth. Past-session cards should usually be `historical` or `active_unverified` with `current_truth: false`.

## Current State

KnowledgeForge has already been modernized to use OpenRouter embeddings:

- Config: `/home/bsdev/knowledgeforge/config.yaml`
- Secrets: `/home/bsdev/.config/knowledgeforge/secrets.env`
- Provider: `openrouter`
- Model: `openai/text-embedding-3-small`
- Vector dimension: 1536

Structured memory card support exists:

- Model: `src/knowledgeforge/core/models.py` (`MemoryCard`)
- Registry: `src/knowledgeforge/core/memory_registry.py`
- CLI: `knowledgeforge memory ...`
- Extraction prompt/helpers: `src/knowledgeforge/ingestion/memory_extraction.py`
- Operator doc: `docs/structured-memory-extraction.md`

KFreport is installed for session-end extraction:

- Canonical: `/home/bsdev/.codex/skills/kfreport`
- Also installed under Claude, Gemini, Kimi, OpenClaw, Antigravity, and Windsurf skill dirs.

The old Nomic ChromaDB was backed up earlier at:

```text
/home/bsdev/.local/share/knowledgeforge/backups/pre-openai-20260424-141356
```

That backup has a cron deletion scheduled for 2026-05-04 at 03:00.

At the time this handoff was written, no `knowledgeforge.interfaces.*` or `knowledgeforge.auth.*` process was running.

## Local Source Inventory

Observed on the server:

| Source | Path | Current count / notes |
|---|---:|---|
| Claude Code JSONL | `/home/bsdev/.claude/projects` | 594 JSONL-like files found at max depth 2 |
| Superpowers archive | `/home/bsdev/.config/superpowers/conversation-archive` | 1604 JSONL-like files found at max depth 2 |
| Codex local logs | `/home/bsdev/.codex/logs_2.sqlite` | SQLite DB with `logs` table; no JSONL under `/home/bsdev/.codex/sessions` |
| Gemini local history | `/home/bsdev/.gemini/history` | No JSON/JSONL files found at max depth 2 |
| Antigravity local | `/home/bsdev/.gemini/antigravity` | Only MCP config locally; main history likely on HomePC |
| Kimi logs | `/home/bsdev/.kimi/logs` | 9 `.log` files; operational logs, not guaranteed full transcripts |
| Windsurf local | `/home/bsdev/.config/Windsurf`, `/home/bsdev/.codeium/windsurf` | Sparse local config/memory files; main history likely on HomePC |
| OpenClaw sessions | `/home/bsdev/.openclaw/agents` | No JSON/JSONL files at max depth 2; inspect deeper before concluding |

Run a deeper inventory before implementation:

```bash
find /home/bsdev/.claude/projects -type f -name '*.jsonl' | wc -l
find /home/bsdev/.config/superpowers/conversation-archive -type f -name '*.jsonl' | wc -l
sqlite3 /home/bsdev/.codex/logs_2.sqlite '.schema logs'
find /home/bsdev/.kimi -type f | sed -n '1,200p'
find /home/bsdev/.windsurf-server /home/bsdev/.antigravity-server /home/bsdev/.codeium/windsurf -type f 2>/dev/null | sed -n '1,300p'
find /home/bsdev/.openclaw/agents -type f 2>/dev/null | sed -n '1,300p'
```

## Ingestion Plan

### Phase 0: Guardrails

1. Keep KnowledgeForge services stopped until code and ingestion jobs are ready.
2. Do not mix old 768-dim Nomic vectors with new 1536-dim OpenRouter vectors.
3. Keep raw source files immutable.
4. Write an ingestion state file before doing batch work:

```text
/home/bsdev/knowledgeforge/data/historical_ingestion/state.sqlite3
```

Track at minimum:

- `host`
- `source_agent`
- `source_path`
- `source_hash`
- `adapter`
- `raw_archive_path`
- `raw_index_status`
- `extraction_status`
- `cards_imported`
- `last_error`
- `updated_at`

### Phase 1: Raw Archive

Create a normalized archive root:

```text
/home/bsdev/.local/share/knowledgeforge/raw_sessions/
```

Suggested layout:

```text
raw_sessions/
  server/
    claude/
    codex/
    gemini/
    kimi/
    openclaw/
    windsurf/
    antigravity/
  homepc/
    ...
```

Copy raw files with hashes in sidecar metadata. Do not rewrite raw files. Redact only derived outputs, not the immutable raw archive.

### Phase 2: Low-Trust Conversation Index

For supported JSONL sources, use the existing parser and `conversations` collection as low-trust episodic memory.

Existing raw ingestion path:

```bash
cd /home/bsdev/knowledgeforge
/home/bsdev/knowledgeforge/.venv/bin/python scripts/migrate_episodic_memory.py --dry-run
```

Then, only after dry-run review:

```bash
/home/bsdev/knowledgeforge/.venv/bin/python scripts/migrate_episodic_memory.py
```

Do not use conversation search as the primary truth layer. It is background evidence only.

### Phase 3: Structured Atomic Extraction

Use `memory_cards` as the primary useful output.

For each supported session:

1. Parse into semantic chunks by project/topic.
2. Run the extraction prompt from `src/knowledgeforge/ingestion/memory_extraction.py`.
3. Produce JSON with `conversation_summary` and `memory_cards`.
4. Store extraction JSON under:

```text
/home/bsdev/knowledgeforge/data/memory_extractions/<host>/<source_agent>/<session-id>.json
```

5. Import with:

```bash
/home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli memory import-json \
  /path/to/extraction.json \
  --source-path /path/to/original/session
```

For session-end work, agents should use `$kfreport`; for batch historical ingestion, build a deterministic batch runner around the same JSON schema.

### Phase 4: Source Adapters

Implement adapters in this order:

1. Claude/Superpowers JSONL
   - Existing parser mostly works.
   - Confirm sidechain/subagent filtering behavior.
   - Preserve `source_agent`, `session_id`, `archive_path`, and line ranges.

2. Codex SQLite
   - Inspect `/home/bsdev/.codex/logs_2.sqlite`.
   - The `logs` table has `feedback_log_body`, `thread_id`, timestamps, target/module metadata.
   - Do not assume it contains clean turn-level transcripts.
   - Build a read-only exporter that groups by `thread_id` and emits normalized JSONL only after schema inspection.

3. Kimi
   - `/home/bsdev/.kimi/logs/*.log` are operational logs and may not include full chat text.
   - Inspect Kimi state directories and the VS Code extension storage under `.windsurf-server` / `.antigravity-server`.
   - Treat logs as environment/diagnostic evidence, not full conversations.

4. Windsurf and Antigravity
   - Main history appears to be on HomePC, not this server.
   - Build remote inventory first.
   - Do not infer storage paths from Linux server sparsity.

5. OpenClaw
   - Inspect deeper under `/home/bsdev/.openclaw/agents/<agent>/sessions`.
   - The initial max-depth scan found no JSON/JSONL, but OpenClaw documentation says sessions live under agent session stores.

### Phase 5: Review Gate

Before importing all extracted cards:

1. Extract 20 sessions across at least 5 projects.
2. Review cards by type/status/confidence.
3. Confirm that secrets are not present.
4. Confirm old ideas are not becoming decisions.
5. Confirm `current_truth` is rare and justified.

Useful commands:

```bash
/home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli memory audit
/home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli memory list --status active_unverified --limit 50
/home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli memory search "event immutability" --project "AIChat Gateway"
```

### Phase 6: Full Batch

Batch in small checkpoints:

- 25 sessions per batch for extraction.
- Import after each batch.
- Write batch report with cards by `project`, `type`, `status`, `confidence`.
- Stop immediately on repeated secret leakage, bad project classification, or malformed cards.

## Remote Windows HomePC Plan

The HomePC is expected to contain the useful Windsurf and Antigravity history. Its user profile root is:

```text
C:\Users\tiaz
```

Do not use Linux `$HOME` assumptions on HomePC. Discover paths locally under the Windows profile, `AppData\Roaming`, `AppData\Local`, and `AppData\LocalLow`.

Preferred workflow:

1. Start a Codex agent on HomePC with:
   - KnowledgeForge MCP access to this server.
   - OpenBrain/OB1 access.
   - Local filesystem access to `C:\Users\tiaz`.

2. Run a read-only source inventory from PowerShell:

```powershell
$UserHome = 'C:\Users\tiaz'
$Roots = @(
  $UserHome,
  "$UserHome\AppData\Roaming",
  "$UserHome\AppData\Local",
  "$UserHome\AppData\LocalLow"
)
$Pattern = 'windsurf|antigravity|codeium|codex|claude|gemini|kimi|openclaw'

$Roots |
  Where-Object { Test-Path -LiteralPath $_ } |
  ForEach-Object {
    Get-ChildItem -LiteralPath $_ -Force -Recurse -Depth 5 -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match $Pattern } |
      Select-Object FullName,
        @{Name='Type';Expression={ if ($_.PSIsContainer) { 'dir' } else { 'file' } }},
        Length,
        LastWriteTime
  } |
  Sort-Object FullName |
  Select-Object -First 1000
```

3. For each discovered app, inventory likely session stores:

```powershell
$Candidate = 'C:\Users\tiaz\AppData\Roaming\Windsurf' # replace with discovered directory
Get-ChildItem -LiteralPath $Candidate -Force -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object {
    $_.Extension -in '.json', '.jsonl', '.sqlite', '.sqlite3', '.db', '.log' -or
    $_.Name -match 'conversation|session|history|state|storage|workspace'
  } |
  Sort-Object FullName |
  Select-Object FullName, Length, LastWriteTime |
  Export-Csv -NoTypeInformation -Encoding UTF8 "$env:TEMP\knowledgeforge-homepc-inventory.csv"
```

Likely roots to check first:

```text
C:\Users\tiaz\.codex
C:\Users\tiaz\.claude
C:\Users\tiaz\.gemini
C:\Users\tiaz\.gemini\antigravity
C:\Users\tiaz\.kimi
C:\Users\tiaz\.openclaw
C:\Users\tiaz\.windsurf
C:\Users\tiaz\.windsurf-server
C:\Users\tiaz\.codeium
C:\Users\tiaz\AppData\Roaming\Windsurf
C:\Users\tiaz\AppData\Roaming\Codeium
C:\Users\tiaz\AppData\Roaming\Antigravity
C:\Users\tiaz\AppData\Local\Windsurf
C:\Users\tiaz\AppData\Local\Codeium
C:\Users\tiaz\AppData\Local\Antigravity
```

4. Prefer extracting structured memory on HomePC and importing cards over MCP.
   - This avoids moving huge raw private archives across machines.
   - Preserve raw archive metadata and hashes.
   - Only transfer raw files when needed for traceability or parser development.

5. If the remote Codex agent has only MCP tools, import cards through KnowledgeForge MCP if the memory-card MCP tools are available. Otherwise write extraction JSON and send it back to this server for:

```bash
/home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli memory import-json <json>
```

6. Use OpenBrain for cross-system continuity:
   - Search OpenBrain for existing project aliases and global user rules before extraction.
   - Capture a short OpenBrain thought after each remote batch: source, counts, errors, and next batch pointer.
   - Do not store raw transcripts or secrets in OpenBrain.

## Handoff Prompt For Remote Codex Agent

Use this prompt for the Codex agent that will run on the Windows HomePC with KnowledgeForge MCP and OpenBrain access:

```text
You are continuing the KnowledgeForge historical-session ingestion project.

Read first, if these server-side paths are available through MCP, a mount, or pasted context:
- /home/bsdev/knowledgeforge/docs/handoffs/2026-04-24-historical-session-ingestion-handoff.md
- /home/bsdev/knowledgeforge/docs/structured-memory-extraction.md
- /home/bsdev/.codex/skills/kfreport/SKILL.md

Goal:
Inventory local and HomePC historical AI sessions, keep raw archives untouched, and extract only durable atomic KnowledgeForge memory cards. Prioritize decisions, constraints, failed attempts, resolutions/fixes, TODOs, API contracts, data schemas, security rules, environment facts, and user preferences.

Rules:
- Do not ingest full raw transcript text as current truth.
- Do not store secrets, tokens, env file contents, private keys, cookies, phone numbers, or credential-bearing logs.
- Historical sessions default to current_truth=false and needs_repo_confirmation=true.
- Use active_verified/current_truth=true only if current repo state, tests, or explicit user confirmation proves it.
- Batch small, audit frequently, and stop on leakage or noisy extraction.

Local server facts:
- KnowledgeForge root: /home/bsdev/knowledgeforge
- KF CLI: /home/bsdev/knowledgeforge/.venv/bin/python -m knowledgeforge.interfaces.cli
- Memory import: knowledgeforge memory import-json <extraction.json> --source-path <raw-source>
- KFreport helper: /home/bsdev/.codex/skills/kfreport/scripts/kfreport.py
- Existing local inventory:
  - Claude JSONL: /home/bsdev/.claude/projects
  - Superpowers archive: /home/bsdev/.config/superpowers/conversation-archive
  - Codex logs DB: /home/bsdev/.codex/logs_2.sqlite
  - Kimi logs: /home/bsdev/.kimi/logs
  - HomePC likely has the main Windsurf and Antigravity histories.

Windows HomePC facts:
- User profile root: C:\Users\tiaz
- Use PowerShell commands for inventory.
- Search under C:\Users\tiaz, AppData\Roaming, AppData\Local, and AppData\LocalLow.
- Do not assume Linux paths or WSL paths for HomePC session stores.
- Treat raw files as immutable. Produce extraction JSON and import memory cards through KnowledgeForge MCP when possible.

OpenBrain usage:
- Search OpenBrain for project aliases and global user rules before extraction.
- Capture a short batch summary to OpenBrain after each batch.
- Keep OpenBrain summaries high-level and free of secrets.

Deliverables:
1. Inventory report with source paths, file counts, and adapter status.
2. Parser/export plan for unsupported sources.
3. Pilot extraction report for 20 sessions.
4. Memory audit after pilot.
5. Full batch plan with checkpoint size and rollback strategy.
6. Final import report by project/type/status/confidence.

Start by verifying KnowledgeForge MCP connectivity, then run a read-only Windows inventory under C:\Users\tiaz. Do not start full ingestion until the pilot plan is reviewed.
```

## Immediate Next Tasks

1. Add a batch extraction runner around `memory_extraction.py`.
2. Add a source inventory script that outputs JSON.
3. Add Codex SQLite exporter after schema inspection.
4. Run pilot on 20 Claude/Superpowers JSONL sessions.
5. Review pilot cards.
6. Start Windows HomePC remote inventory under `C:\Users\tiaz` for Windsurf and Antigravity.

## Done Criteria

- Raw source inventory exists for server and Windows HomePC.
- Supported JSONL sessions are indexed as low-trust `conversations`.
- Durable cards are imported into `memory_cards`.
- Memory audit shows cards by project/type/status.
- At least one sample query per major project returns useful cards.
- Known failed attempts and constraints are easy to retrieve.
- No secrets appear in imported memory.
