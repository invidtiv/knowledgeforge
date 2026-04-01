# Semantic Query Surfaces

## Purpose
Make high-trust semantic memory directly usable by agents and operators through MCP, REST, and CLI, instead of requiring low-level collection knowledge.

## Implemented surfaces

### MCP
- `search_semantic_memory(query, record_type?, project?, max_results?, min_score_threshold?)`
- `list_semantic_memory(record_type?, project?, status?, limit?)`

### REST
- `POST /api/v1/semantic-records/search`
- `GET /api/v1/semantic-records`
- `PATCH /api/v1/semantic-records/{record_type}/{record_id}`

### CLI
- `knowledgeforge semantic search <query> [--type ...] [--project ...]`
- `knowledgeforge semantic list [--type ...] [--project ...] [--status ...]`
- `knowledgeforge semantic archive ...`
- `knowledgeforge semantic supersede ...`

## Behavioral impact
- default engine search now includes semantic collections before documents/code/discoveries/conversations
- semantic-only query surfaces now exist for agents that want the curated layer first

## Next step
- integrate semantic tools into agent workflows by default
- add MCP tools for archive/supersede if safe/desirable
- add discovery -> semantic linkback metadata
