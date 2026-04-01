# Stale Review and Coverage Gap Audit

## Purpose
Help operators and agents identify where semantic memory needs maintenance or is missing entirely.

## Implemented audit dimensions

### Stale review helpers
Flags semantic records missing `reviewed_at` so curated memory can be revisited and validated.

### Promotion candidate report
Shows confirmed discoveries that have not yet been promoted into semantic memory.

### Coverage gap audit
Shows projects with indexed code but no active semantic records.

### Supersession hygiene
Flags superseded semantic records that do not point to a replacement.

## Surfaces

### Engine
- `get_semantic_audit()`

### REST
- `GET /api/v1/semantic-records/audit`

### CLI
- `knowledgeforge semantic audit`

### MCP
- `get_semantic_audit()`

## Output includes
- lifecycle counts
- by-project semantic distribution
- by-type semantic distribution
- coverage gap projects
- promotion candidates
- stale review candidates
- superseded-without-replacement count

## Why this matters
This turns semantic memory from a passive store into something operationally maintainable. It helps answer:
- what should be reviewed?
- what discoveries should be promoted next?
- which indexed projects still lack curated semantic memory?
- where is lifecycle hygiene breaking down?
