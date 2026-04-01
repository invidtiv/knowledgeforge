# KnowledgeForge High-Trust Shared Memory Blueprint

## Objective
Turn KnowledgeForge into a trusted shared memory layer for multiple agents by separating raw source material, episodic history, curated semantic memory, and archived/superseded knowledge.

## Target memory layers

### 1. Source memory
Authoritative sources:
- code repositories
- docs
- configs
- Obsidian notes
- Kanban/task records

### 2. Episodic memory
Timestamped events:
- task completions
- incidents
- debugging outcomes
- deployment notes
- discoveries

### 3. Semantic memory
Curated durable knowledge:
- facts
- runbooks
- project overviews
- conventions
- stable system rules

### 4. Archived / superseded memory
Historical but not default-retrieval:
- replaced workflows
- outdated architecture notes
- stale discoveries
- old incidents kept for audit/history

## Trust model
- T1: authoritative source
- T2: curated durable memory
- T3: episodic memory
- T4: raw/unreviewed content

Default retrieval priority:
1. facts
2. runbooks
3. project_overviews
4. documents
5. codebase
6. episodes
7. discoveries_raw
8. conversations_raw
9. archived/superseded only on fallback or explicit request

## Implementation roadmap

### Phase 1 — reliable ingestion
- explicit per-project ingest by configured project name
- project health/status reporting
- queue-friendly ingestion model
- project indexing visibility (registered vs indexed)

### Phase 2 — trust metadata
Add metadata fields to stored records:
- trust_level
- status
- reviewed_at
- valid_until
- superseded_by
- tags
- confidence

### Phase 3 — new collections
Add/support:
- episodes
- facts
- runbooks
- project_overviews
- discoveries_raw
- conversations_raw
- archived
- superseded

### Phase 4 — promotion workflow
- raw discovery -> episode
- reviewed repeated insight -> fact
- recurring operational procedure -> runbook
- stable architecture summary -> project_overview

### Phase 5 — lifecycle management
- archive stale or expired items
- mark replaced items superseded
- suppress archived/superseded from default retrieval
- periodic review jobs

## Current implementation progress
Implemented already:
- explicit ingest by configured project name
- project status and error_count in API model
- queue-oriented lightweight watcher improvement (serialize file ingestion instead of flooding API)

Next recommended coding slices:
1. add project ingest audit endpoint / CLI summary
2. add trust/status metadata to stored records
3. split conversations into lower-priority retrieval path
4. add semantic collections (facts/runbooks/project_overviews)
