# Discovery to Semantic Promotion

## Purpose
Allow confirmed discoveries to graduate into curated semantic memory instead of remaining only in the discovery bucket.

## Promotion targets
- `fact`
- `runbook`
- `project_overview`

## Rules
- only confirmed discoveries can be promoted
- promoted semantic records default to:
  - `trust_level: T2`
  - `status: active`
  - `confidence >= 0.9`
- semantic title defaults to the first line of discovery content if no explicit title is provided

## REST API
### `POST /api/v1/discoveries/{discovery_id}/promote-semantic`
Request body:
```json
{
  "record_type": "fact",
  "title": "Optional title override"
}
```

## CLI
```bash
knowledgeforge semantic promote-discovery <discovery_id> fact
knowledgeforge semantic promote-discovery <discovery_id> runbook --title "Restart procedure"
knowledgeforge semantic promote-discovery <discovery_id> project_overview
```

## Current implementation status
Implemented:
- engine promotion path
- REST API endpoint
- CLI command

Next step:
- add explicit semantic record listing/search helpers
- add archive/supersede operations
- optionally mark discovery as promoted_to_semantic or link promoted record id
