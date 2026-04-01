# Phase A: Automation and Ergonomics

## Implemented helpers

### Promotion suggestion / review helpers
- Engine: `suggest_promotions(project?, limit?)`
- CLI: `knowledgeforge semantic suggest-promotions`
- MCP: `suggest_semantic_promotions()`
- REST: `POST /api/v1/semantic-records/suggest-promotions`

Purpose:
- identify confirmed discoveries that should likely become facts/runbooks/project overviews
- suggest target semantic record type based on discovery category

### Project-overview generation helper
- Engine: `generate_project_overview(project)`
- CLI: `knowledgeforge semantic generate-overview <project>`
- MCP: `generate_project_overview(project)`
- REST: `POST /api/v1/semantic-records/generate-overview`

Purpose:
- generate and store a first-pass semantic project overview from indexed docs, code, and discoveries

### Semantic coverage bootstrap per project
- Engine: `bootstrap_project_semantic_coverage(project)`
- CLI: `knowledgeforge semantic bootstrap-project <project>`
- MCP: `bootstrap_project_semantic_coverage(project)`
- REST: `POST /api/v1/semantic-records/bootstrap-project`

Purpose:
- create an initial project overview
- return suggested promotion candidates for the same project

## Suggested next phase
### Optional scheduled review jobs
Only after code paths are stable:
- daily semantic audit summary
- daily/weekly promotion review queue
- weekly coverage-gap report
- weekly stale-review report

Why later:
- avoid noisy automation before the workflows are proven useful
