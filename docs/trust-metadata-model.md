# Trust Metadata Model

## Purpose
Add trust/lifecycle metadata to KnowledgeForge records so agents can prefer high-trust, active knowledge and avoid stale or superseded memory by default.

## Fields

### trust_level
- `T1` authoritative source
- `T2` curated/reviewed knowledge
- `T3` episodic memory
- `T4` raw/unreviewed material

### status
- `active`
- `archived`
- `superseded`
- `expired`

### reviewed_at
ISO timestamp indicating human or trusted review completed.

### superseded_by
ID or logical replacement reference for outdated knowledge.

### confidence
Float (0.0-1.0) indicating confidence in the record.

## Current defaults

### Chunk records
- default `trust_level`: `T1`
- default `status`: `active`
- default `confidence`: `1.0`

### Discovery records
- default `trust_level`: `T3`
- default `status`: `active`
- default `confidence`: `0.7`
- on confirm:
  - `trust_level` -> `T2`
  - `reviewed_at` set
  - `confidence` raised to at least `0.9`

## Retrieval behavior now implemented
- default search filters require `status=active`
- inactive records are suppressed from normal retrieval
- trust-aware score weighting is applied:
  - `T1` strongest
  - `T2` slightly lower
  - `T3` reduced
  - `T4` lowest

## Next implementation steps
1. Add new semantic collections (`facts`, `runbooks`, `project_overviews`).
2. Add supersession/archive workflows.
3. Add promotion pipeline from discovery -> semantic memory.
4. Add collection-order policy so semantic collections outrank raw conversation memory by default.
