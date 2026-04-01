# Deterministic Ingestion Queue

## Purpose
Replace chat-style recurring ingestion prompts with a deterministic queue-backed runner.

## How it works
- Queue state is stored in:
  - `~/.local/share/knowledgeforge/ingest_queue.json`
- Each configured project gets a state entry:
  - `pending`
  - `running`
  - `retry`
  - `done`
- Each run processes **one** project only.
- Success/failure is based on explicit ingest result, not conversational inference.

## CLI
Run one queue step manually:
```bash
knowledgeforge queue run-once
```

## Systemd
Service:
- `~/.config/systemd/user/knowledgeforge-ingest-queue.service`

Timer:
- `~/.config/systemd/user/knowledgeforge-ingest-queue.timer`

## Why this is better than cron-agent prompts
- explicit state
- deterministic project selection
- measurable attempts
- retry tracking
- no dependence on vague conversational summaries
