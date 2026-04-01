# Discovery ↔ Semantic Linkback and Audit

## Purpose
Maintain traceability between discoveries and promoted semantic records, and provide audit helpers to measure semantic-memory health.

## Linkback model

### Discovery -> Semantic
Discovery metadata now stores:
- `promoted_semantic_record_id`
- `promoted_semantic_record_type`

### Semantic -> Discovery
Semantic record metadata now stores:
- `source_discovery_id`

This creates bidirectional traceability between the episodic and semantic layers.

## Audit helpers

### Engine
- `get_semantic_audit()`

Returns:
- active / archived / superseded counts
- records with discovery linkback
- discoveries with semantic linkback
- counts by project
- counts by record type

### REST
- `GET /api/v1/semantic-records/audit`

### CLI
- `knowledgeforge semantic audit`

### MCP
- `get_semantic_audit()`

## Why this matters
This makes the semantic layer more trustworthy by answering:
- what semantic knowledge exists?
- how much of it is maintained?
- what came from reviewed discoveries?
- where is semantic coverage concentrated?

## Next step
- add stale-review helpers (e.g. records without reviewed_at after threshold)
- add project coverage gaps report
- add automatic promotion suggestions from confirmed discoveries
