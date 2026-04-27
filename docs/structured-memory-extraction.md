# Structured Memory Extraction

KnowledgeForge should not treat old conversations as current truth. The useful path is:

1. Keep the raw conversation archive for traceability.
2. Extract small atomic memory cards for retrieval.
3. Promote a card to current truth only after repo or user confirmation.

The default memory card statuses are intentionally conservative:

- `active_verified` or `verified`: confirmed by repo or user.
- `active_unverified`: likely useful, but needs confirmation before implementation.
- `historical`: orientation only.
- `failed`, `resolved`, `open_unverified`: reusable operational history.
- `superseded`, `deprecated`, `cancelled`: retained but excluded from default search.

Recommended extraction priority:

- `decision`
- `constraint`
- `failed_attempt`
- `resolution`
- `todo`
- `api_contract`
- `objective`
- `definition_of_done`
- `security_rule`
- `user_preference`

Do not extract casual back-and-forth, duplicated explanations, vague brainstorms as decisions, long code dumps, or stale assumptions without a status that marks them as historical or unverified.

## CLI Workflow

Generate an extraction prompt for a conversation JSONL:

```bash
knowledgeforge memory prompt-file /path/to/session.jsonl > extraction-prompt.md
```

Run that prompt through the chosen OpenRouter/Kimi extraction agent, then import the JSON:

```bash
knowledgeforge memory import-json extraction.json --source-path /path/to/session.jsonl
```

Search structured memory only:

```bash
knowledgeforge memory search "event immutability" --project "AIChat Gateway"
```

Promote after repo validation:

```bash
knowledgeforge memory status mem_abc123 active_verified --current-truth
```

## Batch Pilot

For server-side historical sessions, use the local ingestion runner in pilot mode. It writes extraction JSON under `data/memory_extractions/server/` and imports cards with conservative historical defaults:

```bash
KNOWLEDGEFORGE_MEMORY_EXTRACTION_MODEL=<openrouter-chat-model> \
/home/bsdev/knowledgeforge/.venv/bin/python scripts/ingest_local_historical_sessions.py \
  --skip-raw-archive \
  --skip-conversation-index \
  --extract-memory \
  --extraction-limit 20 \
  --report data/historical_ingestion/local_memory_pilot.json
```

Review the imported cards with:

```bash
knowledgeforge memory audit
knowledgeforge memory list --status active_unverified --limit 50
```

## Retrieval Rule

Future agents should rank truth in this order:

1. Current repo files
2. Current state or handover files
3. Latest session logs
4. Active verified memory cards
5. Active unverified memory cards
6. Historical memory cards
7. Deprecated or superseded memory cards

Never implement from historical memory alone. Use it to inspect the right files, ask better questions, and avoid known failed attempts.
