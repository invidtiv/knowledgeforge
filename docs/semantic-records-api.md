# Semantic Records API

## Purpose
Allow KnowledgeForge to store curated semantic memory directly into high-trust collections instead of relying only on documents, code, discoveries, and conversations.

## Record types
- `fact`
- `runbook`
- `project_overview`

## REST endpoint
### `POST /api/v1/semantic-records`
Request body:
```json
{
  "title": "AndroidSynchApp overview",
  "content": "Android sync app that ...",
  "project": "AndroidSynchApp",
  "record_type": "project_overview",
  "tags": ["android", "sync"],
  "trust_level": "T2",
  "status": "active",
  "confidence": 0.9
}
```

## CLI
### Create a fact
```bash
knowledgeforge semantic create fact "Auth rule" "JWT tokens expire after 24h" --project my-app --tags auth,jwt
```

### Create a runbook
```bash
knowledgeforge semantic create runbook "Restart API" "systemctl --user restart knowledgeforge-api.service" --project knowledgeforge --tags ops,restart
```

### Create a project overview
```bash
knowledgeforge semantic create project_overview "AndroidSynchApp overview" "Main sync app for Android..." --project AndroidSynchApp --tags android,sync
```

## Current status
Implemented:
- semantic record model
- engine store path
- REST create endpoint
- CLI create command

Next step:
- list/query semantic records explicitly
- promote discoveries into semantic records
- add supersede/archive operations
