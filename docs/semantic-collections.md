# Semantic Collections

## Purpose
Introduce first-class semantic destinations for curated, high-trust shared knowledge instead of relying only on documents, code, discoveries, and conversations.

## Collections

### facts
Durable validated knowledge.
Examples:
- stable architecture truths
- validated constraints
- known system ownership
- long-lived implementation facts

Default trust:
- `trust_level: T2`
- `status: active`

### runbooks
Operational procedures and repeatable workflows.
Examples:
- restart procedures
- deployment steps
- incident response flow
- recovery playbooks

Default trust:
- `trust_level: T2`
- `status: active`

### project_overviews
Compact project-level summaries.
Examples:
- project purpose
- main entry points
- repo structure summary
- service boundaries
- key dependencies

Default trust:
- `trust_level: T2`
- `status: active`

## Retrieval order
The engine now searches semantic collections before docs/code/discoveries/conversations when no explicit collection list is provided.

Priority:
1. facts
2. runbooks
3. project_overviews
4. documents
5. codebase
6. discoveries
7. conversations

## Current implementation status
Implemented:
- config support for the three collections
- included in default search order
- included in stats collection reporting
- filter plumbing recognizes them

Next step:
- add create/store APIs and promotion flow into these collections
