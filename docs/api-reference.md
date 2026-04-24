# KnowledgeForge REST API Reference

Base URL: `http://127.0.0.1:8742/api/v1`

All endpoints return JSON responses with standard HTTP status codes.

## Table of Contents

- [Authentication](#authentication)
- [Search](#search)
- [Discoveries](#discoveries)
- [Projects](#projects)
- [Ingestion](#ingestion)
- [Health & Status](#health--status)
- [Error Handling](#error-handling)

---

## Authentication

Currently, KnowledgeForge runs locally without authentication. Future versions will support API keys for remote access.

**Headers:**
- `Content-Type: application/json` (for POST/PUT requests)

---

## Search

### Search Knowledge Base

Search across all indexed documents, code, and discoveries.

**Endpoint:** `POST /api/v1/search`

**Request Body:**
```json
{
  "query": "authentication implementation",
  "project": "my-app",
  "collections": ["codebase", "documents", "discoveries", "conversations"],
  "tags": ["auth"],
  "language": "python",
  "category": "bugfix",
  "confirmed_only": false,
  "n_results": 10,
  "min_score": 0.2
}
```

**Parameters:**
- `query` (string, required) - Search query text
- `project` (string, optional) - Filter by project name
- `collections` (array[string], optional) - Collections to search. Defaults to all.
- `tags` (array[string], optional) - Tag filter (documents collection)
- `language` (string, optional) - Language filter (codebase collection)
- `category` (string, optional) - Category filter (discoveries/conversations)
- `confirmed_only` (boolean, optional, default: `false`) - Confirmed discoveries only
- `n_results` (integer, optional, default: 5) - Number of results to return
- `min_score` (number, optional, default: 0.0) - Minimum relevance score (0-1)

**Response:**
```json
{
  "query": "authentication implementation",
  "results": [
    {
      "content": "Authentication is handled by OAuth2 flow...",
      "score": 0.766,
      "metadata": {
        "source_file": "src/auth.py",
        "project_name": "my-app",
        "language": "python"
      },
      "collection": "codebase"
    }
  ],
  "total_results": 1,
  "search_time_ms": 123.4
}
```

**Status Codes:**
- `200 OK` - Search successful
- `400 Bad Request` - Invalid query parameters
- `500 Internal Server Error` - Search failed

**Example:**
```bash
curl -X POST http://127.0.0.1:8742/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "how does authentication work",
    "project": "my-app",
    "collections": ["codebase"],
    "language": "python",
    "n_results": 5
  }'
```

---

## Discoveries

### List Discoveries

Retrieve stored agent discoveries with optional filtering.

**Endpoint:** `GET /api/v1/discoveries`

**Query Parameters:**
- `project` (string, optional) - Filter by project name
- `category` (string, optional) - Filter by category: `bugfix`, `optimization`, `insight`, `pattern`, `workaround`
- `severity` (string, optional) - Filter by severity: `low`, `medium`, `high`, `critical`
- `confirmed` (boolean, optional) - Filter by confirmation status
- `limit` (integer, optional, default: 50) - Max results to return
- `offset` (integer, optional, default: 0) - Pagination offset

**Response:**
```json
{
    "discoveries": [
        {
            "id": "disc_xyz789",
            "content": "Database connection pool exhausted at 100 concurrent connections. Increased max_connections to 200.",
            "category": "bugfix",
            "severity": "critical",
            "project": "my-app",
            "agent_name": "debugger-agent",
            "confirmed": false,
            "created_at": "2025-01-15T14:30:00Z",
            "metadata": {
                "file": "db/pool.py",
                "error_type": "PoolExhausted"
            }
        }
    ],
    "total": 1,
    "limit": 50,
    "offset": 0
}
```

**Status Codes:**
- `200 OK` - Request successful
- `400 Bad Request` - Invalid parameters

**Example:**
```bash
curl "http://127.0.0.1:8742/api/v1/discoveries?project=my-app&confirmed=false&category=bugfix"
```

---

### Create Discovery

Store a new discovery from an AI agent.

**Endpoint:** `POST /api/v1/discoveries`

**Request Body:**
```json
{
    "content": "Found memory leak in image processing. Need to call .release() after cv2.imread().",
    "category": "bugfix",
    "severity": "high",
    "project": "image-processor",
    "agent_name": "debugger-agent",
    "metadata": {
        "file": "src/processor.py",
        "function": "process_image",
        "line": 145
    }
}
```

**Parameters:**
- `content` (string, required) - Discovery description
- `category` (string, required) - One of: `bugfix`, `optimization`, `insight`, `pattern`, `workaround`
- `severity` (string, optional, default: `medium`) - One of: `low`, `medium`, `high`, `critical`
- `project` (string, optional) - Associated project name
- `agent_name` (string, optional) - Name of agent that created discovery
- `metadata` (object, optional) - Additional context

**Response:**
```json
{
    "id": "disc_abc123",
    "content": "Found memory leak in image processing...",
    "category": "bugfix",
    "severity": "high",
    "project": "image-processor",
    "agent_name": "debugger-agent",
    "confirmed": false,
    "created_at": "2025-01-15T14:35:00Z",
    "metadata": {
        "file": "src/processor.py",
        "function": "process_image",
        "line": 145
    }
}
```

**Status Codes:**
- `201 Created` - Discovery created successfully
- `400 Bad Request` - Invalid request body
- `500 Internal Server Error` - Creation failed

**Example:**
```bash
curl -X POST http://127.0.0.1:8742/api/v1/discoveries \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Redis cache timeout should be 300s not 30s for session data",
    "category": "optimization",
    "severity": "medium",
    "project": "my-app"
  }'
```

---

### Get Discovery

Retrieve a specific discovery by ID.

**Endpoint:** `GET /api/v1/discoveries/{discovery_id}`

**Response:**
```json
{
    "id": "disc_abc123",
    "content": "Found memory leak in image processing...",
    "category": "bugfix",
    "severity": "high",
    "project": "image-processor",
    "agent_name": "debugger-agent",
    "confirmed": true,
    "created_at": "2025-01-15T14:35:00Z",
    "confirmed_at": "2025-01-15T15:00:00Z",
    "metadata": {
        "file": "src/processor.py"
    }
}
```

**Status Codes:**
- `200 OK` - Discovery found
- `404 Not Found` - Discovery ID not found

---

### Confirm Discovery

Mark a discovery as confirmed by human review.

**Endpoint:** `PUT /api/v1/discoveries/{discovery_id}/confirm`

**Response:**
```json
{
    "id": "disc_abc123",
    "confirmed": true,
    "confirmed_at": "2025-01-15T15:00:00Z"
}
```

**Status Codes:**
- `200 OK` - Discovery confirmed
- `404 Not Found` - Discovery ID not found

**Example:**
```bash
curl -X PUT http://127.0.0.1:8742/api/v1/discoveries/disc_abc123/confirm
```

---

### Reject Discovery

Mark a discovery as rejected (will be excluded from promotion).

**Endpoint:** `PUT /api/v1/discoveries/{discovery_id}/reject`

**Response:**
```json
{
    "id": "disc_abc123",
    "confirmed": false,
    "rejected": true,
    "rejected_at": "2025-01-15T15:05:00Z"
}
```

**Status Codes:**
- `200 OK` - Discovery rejected
- `404 Not Found` - Discovery ID not found

---

### Promote Discoveries

Promote confirmed discoveries to Obsidian vault as markdown notes.

**Endpoint:** `POST /api/v1/discoveries/promote`

**Request Body:**
```json
{
    "project": "my-app",
    "category": "bugfix",
    "dry_run": false
}
```

**Parameters:**
- `project` (string, optional) - Only promote discoveries for this project
- `category` (string, optional) - Only promote discoveries of this category
- `dry_run` (boolean, optional, default: false) - Preview without writing files

**Response:**
```json
{
    "promoted": [
        {
            "discovery_id": "disc_abc123",
            "file_path": "/vault/Discoveries/2025-01-15-memory-leak-fix.md",
            "title": "Memory Leak in Image Processing"
        }
    ],
    "total_promoted": 1,
    "dry_run": false
}
```

**Status Codes:**
- `200 OK` - Promotion successful
- `400 Bad Request` - Invalid parameters
- `500 Internal Server Error` - Promotion failed

---

## Projects

### List Projects

Get all indexed projects.

**Endpoint:** `GET /api/v1/projects`

**Response:**
```json
{
    "projects": [
        {
            "name": "my-app",
            "path": "/home/user/projects/my-app",
            "indexed_at": "2025-01-15T10:00:00Z",
            "file_count": 234,
            "collections": ["code", "documents"],
            "languages": ["python", "javascript", "typescript"]
        },
        {
            "name": "data-pipeline",
            "path": "/home/user/projects/data-pipeline",
            "indexed_at": "2025-01-14T08:30:00Z",
            "file_count": 89,
            "collections": ["code"],
            "languages": ["python", "rust"]
        }
    ],
    "total": 2
}
```

**Status Codes:**
- `200 OK` - Request successful

**Example:**
```bash
curl http://127.0.0.1:8742/api/v1/projects
```

---

### Get Project Context

Get comprehensive context for a specific project.

**Endpoint:** `GET /api/v1/projects/{project_name}/context`

**Query Parameters:**
- `include_readme` (boolean, optional, default: true) - Include README content
- `include_structure` (boolean, optional, default: true) - Include directory structure
- `include_stats` (boolean, optional, default: true) - Include statistics

**Response:**
```json
{
    "name": "my-app",
    "path": "/home/user/projects/my-app",
    "readme": "# My App\n\nA web application for...",
    "structure": {
        "src/": {
            "auth/": ["login.py", "oauth.py"],
            "api/": ["routes.py", "models.py"]
        }
    },
    "stats": {
        "total_files": 234,
        "total_lines": 45678,
        "languages": {
            "python": 156,
            "javascript": 45,
            "typescript": 33
        }
    },
    "indexed_at": "2025-01-15T10:00:00Z"
}
```

**Status Codes:**
- `200 OK` - Project found
- `404 Not Found` - Project not found

---

## Ingestion

### Ingest Path

Index a file or directory into the knowledge base.

**Endpoint:** `POST /api/v1/ingest`

**Request Body:**
```json
{
    "path": "/home/user/projects/my-app/src",
    "project_name": "my-app",
    "force_full": false,
    "collection": "code"
}
```

**Parameters:**
- `path` (string, required) - File or directory path to index
- `project_name` (string, optional) - Associate with project
- `force_full` (boolean, optional, default: false) - Force full re-index
- `collection` (string, optional) - Target collection: `documents`, `code`

**Response:**
```json
{
    "status": "success",
    "path": "/home/user/projects/my-app/src",
    "files_processed": 156,
    "files_skipped": 12,
    "chunks_created": 1234,
    "duration_seconds": 45.2
}
```

**Status Codes:**
- `200 OK` - Ingestion successful
- `400 Bad Request` - Invalid path or parameters
- `500 Internal Server Error` - Ingestion failed

**Example:**
```bash
curl -X POST http://127.0.0.1:8742/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/home/user/projects/my-app",
    "project_name": "my-app"
  }'
```

---

### Index Obsidian Vault

Trigger full or incremental vault indexing.

**Endpoint:** `POST /api/v1/ingest/vault`

**Request Body:**
```json
{
    "force_full": false
}
```

**Parameters:**
- `force_full` (boolean, optional, default: false) - Force full re-index

**Response:**
```json
{
    "status": "success",
    "vault_path": "/home/user/Documents/MyVault",
    "files_processed": 456,
    "files_skipped": 23,
    "chunks_created": 3456,
    "duration_seconds": 67.8
}
```

**Status Codes:**
- `200 OK` - Indexing successful
- `500 Internal Server Error` - Indexing failed

---

## Health & Status

### Health Check

Get service health status.

**Endpoint:** `GET /api/v1/health`

**Response:**
```json
{
    "status": "healthy",
    "version": "0.1.0",
    "uptime_seconds": 3600,
    "chromadb_connected": true,
    "embedder_loaded": true
}
```

**Status Codes:**
- `200 OK` - Service healthy
- `503 Service Unavailable` - Service unhealthy

**Example:**
```bash
curl http://127.0.0.1:8742/api/v1/health
```

---

### System Statistics

Get system statistics and metrics.

**Endpoint:** `GET /api/v1/stats`

**Response:**
```json
{
    "collections": {
        "documents": 4567,
        "code": 12345,
        "discoveries": 89
    },
    "total_chunks": 17001,
    "projects": 3,
    "vault_indexed": true,
    "last_index_time": "2025-01-15T10:00:00Z",
    "storage_size_mb": 234.5
}
```

**Status Codes:**
- `200 OK` - Request successful

---

## Error Handling

All errors follow a consistent JSON format:

```json
{
    "error": {
        "code": "INVALID_QUERY",
        "message": "Query text cannot be empty",
        "details": {
            "field": "query",
            "constraint": "non_empty"
        }
    }
}
```

### Common Error Codes

| HTTP Status | Error Code | Description |
|-------------|------------|-------------|
| 400 | `INVALID_QUERY` | Search query is malformed |
| 400 | `INVALID_PARAMETERS` | Request parameters are invalid |
| 404 | `NOT_FOUND` | Resource not found |
| 404 | `PROJECT_NOT_FOUND` | Project does not exist |
| 404 | `DISCOVERY_NOT_FOUND` | Discovery ID does not exist |
| 409 | `ALREADY_EXISTS` | Resource already exists |
| 500 | `SEARCH_FAILED` | Search operation failed |
| 500 | `INGESTION_FAILED` | Ingestion operation failed |
| 500 | `INTERNAL_ERROR` | Unexpected server error |
| 503 | `SERVICE_UNAVAILABLE` | ChromaDB or embedder unavailable |

---

## Rate Limiting

Currently no rate limiting is applied for local usage. Future versions will support configurable rate limits.

---

## Pagination

Endpoints that return lists support pagination via `limit` and `offset` parameters:

```bash
# Get discoveries 11-20
curl "http://127.0.0.1:8742/api/v1/discoveries?limit=10&offset=10"
```

---

## Filtering

Advanced filtering is supported via the `filter` parameter in search requests:

```json
{
    "query": "authentication",
    "filter": {
        "file_type": "python",
        "project": "my-app",
        "function_name": {"$contains": "auth"}
    }
}
```

**Supported operators:**
- `$eq` - Equals
- `$ne` - Not equals
- `$gt` - Greater than
- `$gte` - Greater than or equal
- `$lt` - Less than
- `$lte` - Less than or equal
- `$contains` - String contains
- `$in` - Value in list
- `$nin` - Value not in list

---

### List Conversation Sessions

List grouped conversation sessions from the indexed `conversations` collection.

**Endpoint:** `GET /api/v1/conversations/sessions`

**Query Parameters:**
- `project` (string, optional) - Exact conversation project filter
- `source_agent` (string, optional) - `claude`, `codex`, or `gemini`
- `after` (string, optional) - Only include sessions after `YYYY-MM-DD`
- `before` (string, optional) - Only include sessions before `YYYY-MM-DD`
- `limit` (integer, optional, default: `200`, max: `1000`) - Maximum grouped sessions to return

**Response:**
```json
{
  "total_sessions": 17,
  "sessions": [
    {
      "session_id": "2a15a66d-32ac-4b4b-b4a7-819373c3c67b",
      "project": "-home-bsdev-knowledgeforge",
      "source_agent": "claude",
      "exchange_count": 2,
      "first_timestamp": "2026-04-03T19:41:03.027Z",
      "last_timestamp": "2026-04-03T19:41:36.134Z",
      "archive_path": "/home/bsdev/.claude/projects/-home-bsdev-knowledgeforge/2a15a66d-32ac-4b4b-b4a7-819373c3c67b.jsonl",
      "tool_names": ["Bash", "Read"],
      "summary_hint": "",
      "category": "",
      "intent": ""
    }
  ]
}
```

**Example:**
```bash
curl "http://127.0.0.1:8742/api/v1/conversations/sessions?project=-home-bsdev-knowledgeforge&limit=10"
```

---

## Webhooks (Future)

Future versions will support webhook notifications for:
- New discoveries created
- Discoveries confirmed/rejected
- Indexing completed
- Search queries executed

Configuration will be available via `config.yaml`.

---

## SDK Support (Future)

Official SDKs planned for:
- Python (`knowledgeforge-sdk`)
- JavaScript/TypeScript (`@knowledgeforge/sdk`)
- Go (`github.com/tiaz-fr/knowledgeforge-go`)

---

## Version History

- **v1** (current) - Initial REST API release
- Semantic versioning follows KnowledgeForge releases

---

For questions or issues, please open an issue at:
https://github.com/tiaz-fr/knowledgeforge/issues
