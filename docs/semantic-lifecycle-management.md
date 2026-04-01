# Semantic Lifecycle Management

## Purpose
Provide visibility and maintenance controls for semantic memory so curated knowledge can be listed, archived, and superseded over time.

## Implemented capabilities

### List semantic records
- REST: `GET /api/v1/semantic-records`
- CLI: `knowledgeforge semantic list`

Filters:
- `record_type`
- `project`
- `status`
- `limit`

### Archive semantic records
- REST: `PATCH /api/v1/semantic-records/{record_type}/{record_id}` with:
```json
{ "status": "archived" }
```
- CLI:
```bash
knowledgeforge semantic archive fact <record_id>
```

### Supersede semantic records
- REST: `PATCH /api/v1/semantic-records/{record_type}/{record_id}` with:
```json
{ "status": "superseded", "superseded_by": "<replacement_record_id>" }
```
- CLI:
```bash
knowledgeforge semantic supersede fact <record_id> <replacement_record_id>
```

## Why this matters
This is the lifecycle control needed to keep semantic memory trustworthy:
- active records stay visible
- archived records stop polluting default retrieval
- superseded records remain auditable but no longer act as current truth

## Next step
- add semantic search helpers / MCP exposure
- link promoted semantic record IDs back to discovery metadata
- build review jobs for stale semantic memory
