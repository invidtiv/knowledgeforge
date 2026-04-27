# Local Historical Session Ingestion

Use these scripts to inventory and ingest historical AI sessions that already exist on this server.

The local ingestion path has three layers:

1. Inventory known session locations without reading transcript contents.
2. Archive supported raw JSONL sessions unchanged.
3. Index supported JSONL sessions into the low-trust `conversations` collection.

Structured memory-card extraction is opt-in and should be piloted in small batches.

## Inventory

```bash
cd /home/bsdev/knowledgeforge
/home/bsdev/knowledgeforge/.venv/bin/python scripts/inventory_historical_sessions.py \
  --output data/historical_ingestion/local_inventory.json
```

The inventory includes Claude JSONL, Superpowers archive JSONL, Codex SQLite logs, Kimi logs, Gemini/Antigravity/Windsurf candidate paths, and OpenClaw agent state.

## Dry Run

```bash
/home/bsdev/knowledgeforge/.venv/bin/python scripts/ingest_local_historical_sessions.py \
  --dry-run \
  --report data/historical_ingestion/local_ingest_dry_run.json
```

The default dry run parses sessions and estimates chunks without tokenizing every exchange. Use `--dry-run-count-chunks` only when exact token chunk counts are worth the extra runtime.

## Full Local Index

```bash
/home/bsdev/knowledgeforge/.venv/bin/python scripts/ingest_local_historical_sessions.py \
  --report data/historical_ingestion/local_ingest_run.json
```

This copies raw JSONL files to:

```text
/home/bsdev/.local/share/knowledgeforge/raw_sessions/server/
```

Then it indexes supported JSONL into ChromaDB `conversations` and the keyword index. Generated reports and state live under:

```text
/home/bsdev/knowledgeforge/data/historical_ingestion/
```

## Retry Missed Files

The ingestion is incremental. If OpenRouter returns transient read or connection errors, rerun with `--skip-raw-archive`. To retry only affected project directories, pass explicit `--source-dir` values:

```bash
/home/bsdev/knowledgeforge/.venv/bin/python scripts/ingest_local_historical_sessions.py \
  --skip-raw-archive \
  --source-dir /path/to/archive/project-a \
  --source-dir /path/to/archive/project-b \
  --report data/historical_ingestion/local_ingest_retry.json
```

## Chroma Concurrency Rule

Do not query/count/read the local ChromaDB process from another Python process while a large ingestion writer is active. The local persistent Chroma backend is not safe for that pattern and can corrupt or crash a collection.

Wait for the ingestion process to exit before running:

```bash
knowledgeforge stats
knowledgeforge search ...
knowledgeforge memory audit
```

## Structured Memory Pilot

Only run structured extraction in a small reviewed pilot. Historical cards are forced to `current_truth=false` and `needs_repo_confirmation=true`.

```bash
KNOWLEDGEFORGE_MEMORY_EXTRACTION_MODEL=<openrouter-chat-model> \
/home/bsdev/knowledgeforge/.venv/bin/python scripts/ingest_local_historical_sessions.py \
  --skip-raw-archive \
  --skip-conversation-index \
  --extract-memory \
  --extraction-limit 20 \
  --report data/historical_ingestion/local_memory_pilot.json
```

The extraction JSON files are written under:

```text
/home/bsdev/knowledgeforge/data/memory_extractions/server/
```
