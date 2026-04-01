# KnowledgeForge Architecture

This document explains the system design, data flow, and architectural decisions behind KnowledgeForge.

## Table of Contents

- [System Overview](#system-overview)
- [Core Components](#core-components)
- [Data Flow](#data-flow)
- [Ingestion Pipeline](#ingestion-pipeline)
- [Search & Retrieval](#search--retrieval)
- [Discovery System](#discovery-system)
- [Interface Layer](#interface-layer)
- [Storage Architecture](#storage-architecture)
- [Design Decisions](#design-decisions)
- [Performance Considerations](#performance-considerations)
- [Security Model](#security-model)
- [Future Architecture](#future-architecture)

---

## System Overview

KnowledgeForge is a **local-first, multi-agent RAG (Retrieval-Augmented Generation) knowledge system** that creates a unified searchable index of:
- Obsidian vault (personal knowledge base)
- Code projects (AST-aware indexing)
- Agent discoveries (debugging insights, learnings)

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      INTERFACE LAYER                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │  MCP Server  │  │   REST API   │  │     CLI      │           │
│  │ (Claude Code)│  │  (FastAPI)   │  │   (Typer)    │           │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
└─────────┼──────────────────┼──────────────────┼──────────────────┘
          │                  │                  │
          └──────────────────┼──────────────────┘
                             │
┌─────────────────────────────▼─────────────────────────────────┐
│                      CORE ENGINE                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              KnowledgeForge Engine                        │  │
│  │  - Orchestration     - Query routing                     │  │
│  │  - Index management  - Discovery management              │  │
│  └────┬─────────────────────┬───────────────────────────┬───┘  │
│       │                     │                           │       │
│  ┌────▼─────┐          ┌────▼─────┐               ┌────▼─────┐ │
│  │ Embedder │          │  Store   │               │ Watcher  │ │
│  │(sentence-│          │(ChromaDB)│               │(watchdog)│ │
│  │transform)│          │          │               │          │ │
│  └──────────┘          └──────────┘               └──────────┘ │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   INGESTION PIPELINE                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │  Obsidian    │  │    Code      │  │  Discovery   │           │
│  │   Parser     │  │   Parser     │  │   Manager    │           │
│  │ - Frontmatter│  │ - tree-sitter│  │ - CRUD ops   │           │
│  │ - Wiki-links │  │ - AST chunks │  │ - Promotion  │           │
│  │ - Headings   │  │ - 8 langs    │  │              │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
└──────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                      STORAGE LAYER                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │   ChromaDB   │  │  File Hashes │  │ Config Store │           │
│  │  (vectors +  │  │  (SHA256)    │  │  (YAML)      │           │
│  │   metadata)  │  │              │  │              │           │
│  └──────────────┘  └──────────────┘  └──────────────┘           │
└──────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. KnowledgeForge Engine

**Location:** `src/knowledgeforge/core/engine.py`

The **orchestrator** that coordinates all operations:
- Index management (create, update, query)
- Routing search queries to correct collections
- Managing project metadata
- Coordinating ingestion pipelines

**Key Methods:**
- `index_vault()` - Index Obsidian vault
- `index_project()` - Index code project
- `search()` - Search across collections
- `store_discovery()` - Store agent insight
- `get_project_context()` - Get project overview

**Responsibilities:**
- High-level orchestration
- Transaction coordination
- Error handling and retry logic
- Logging and metrics

---

### 2. Embedder

**Location:** `src/knowledgeforge/core/embedder.py`

Wrapper around `sentence-transformers` for generating embeddings.

**Key Features:**
- Model loading and caching
- Batch processing
- GPU/CPU device management
- Embedding normalization

**Default Model:** `nomic-ai/nomic-embed-text-v1.5`
- 768-dimensional embeddings
- Optimized for retrieval tasks
- Good performance on code and natural language

**Interface:**
```python
class Embedder:
    def embed_text(self, text: str) -> List[float]
    def embed_batch(self, texts: List[str]) -> List[List[float]]
```

---

### 3. Vector Store

**Location:** `src/knowledgeforge/core/store.py`

Wrapper around ChromaDB for vector storage and retrieval.

**Collections:**
- `documents` - Obsidian notes, README files, documentation
- `code` - Source code chunks (AST-aware)
- `discoveries` - Agent-generated insights

**Key Features:**
- CRUD operations for documents
- Metadata filtering
- Similarity search with distance metrics
- Collection management

**Interface:**
```python
class VectorStore:
    def add_documents(self, collection: str, documents: List[Document])
    def search(self, collection: str, query_embedding: List[float], n_results: int, filter: Dict)
    def delete(self, collection: str, ids: List[str])
    def get_stats(self) -> Dict
```

---

### 4. File Watcher

**Location:** `src/knowledgeforge/ingestion/watcher.py`

Filesystem monitoring for live sync using `watchdog`.

**Features:**
- Debounced event handling (avoid duplicate work)
- Pattern-based filtering (ignore `.git/`, `node_modules/`, etc.)
- Incremental re-indexing (only changed files)

**Workflow:**
```
File change detected → Debounce (2s) → Hash check → Re-index if changed
```

---

## Data Flow

### Indexing Flow

```
┌─────────────┐
│  User/Agent │
│   triggers  │
│  indexing   │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│ Engine receives │
│  index request  │
└──────┬──────────┘
       │
       ▼
┌──────────────────────┐
│ Select parser based  │
│ on content type:     │
│ - .md → Obsidian     │
│ - .py → Code         │
│ - etc.               │
└──────┬───────────────┘
       │
       ▼
┌─────────────────────────┐
│ Parser extracts:         │
│ - Text content           │
│ - Metadata (frontmatter) │
│ - Structure (AST/headings)│
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Chunker splits content  │
│ into retrieval units:   │
│ - Respect boundaries    │
│ - Add overlap           │
│ - Preserve context      │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Embedder generates      │
│ vector embeddings       │
│ (batch processing)      │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Store writes to ChromaDB│
│ with metadata:          │
│ - Source file           │
│ - Chunk position        │
│ - Project/collection    │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Hash tracker updates    │
│ SHA256 cache for        │
│ incremental indexing    │
└─────────────────────────┘
```

---

### Search Flow

```
┌─────────────┐
│  User/Agent │
│   searches  │
└──────┬──────┘
       │
       ▼
┌─────────────────────────┐
│ Interface (MCP/REST/CLI)│
│ receives search query   │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Engine routes query to  │
│ appropriate collections │
│ (documents/code/all)    │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Embedder generates      │
│ query embedding         │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Store performs vector   │
│ similarity search in    │
│ ChromaDB (HNSW index)   │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Results filtered by     │
│ metadata (project, etc.)│
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Results ranked by       │
│ similarity score        │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Return to user/agent    │
│ with source metadata    │
└─────────────────────────┘
```

---

### Discovery Flow

```
┌─────────────┐
│    Agent    │
│  discovers  │
│   insight   │
└──────┬──────┘
       │
       ▼
┌─────────────────────────┐
│ Agent calls MCP/REST    │
│ store_discovery()       │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Discovery Manager       │
│ creates record with:    │
│ - Content               │
│ - Category              │
│ - Severity              │
│ - Project               │
│ - Timestamp             │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Store in discoveries    │
│ collection with         │
│ confirmed=false         │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ User reviews via CLI:   │
│ knowledgeforge          │
│   discoveries review    │
└──────┬──────────────────┘
       │
       ├─→ CONFIRM ─────┐
       │                │
       └─→ REJECT       │
                        │
                        ▼
              ┌─────────────────┐
              │ Promoter writes │
              │ to Obsidian     │
              │ vault as .md    │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Watcher detects │
              │ new file, re-   │
              │ indexes it      │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Now searchable  │
              │ by all agents   │
              └─────────────────┘
```

---

## Ingestion Pipeline

### Obsidian Parser

**Location:** `src/knowledgeforge/ingestion/obsidian.py`

**Capabilities:**
- YAML frontmatter extraction (tags, dates, custom fields)
- Wiki-link resolution (`[[Link]]`, `[[Link|Alias]]`)
- Heading hierarchy preservation
- Code block detection (language hints)
- Callouts/admonitions support

**Chunking Strategy:**
```
Document → Split by headings (H1, H2, H3) → Split large sections → Add overlap
```

**Metadata Extracted:**
- File path
- Title (from frontmatter or first H1)
- Tags (from frontmatter)
- Creation/modification dates
- Backlinks (wiki-links)

**Example Chunk:**
```json
{
    "content": "## Authentication Flow\n\nThe system uses OAuth2...",
    "metadata": {
        "source": "/vault/Development/Auth.md",
        "title": "Authentication Design",
        "tags": ["security", "oauth"],
        "heading": "Authentication Flow",
        "section_level": 2,
        "chunk_index": 3,
        "created_at": "2025-01-10"
    }
}
```

---

### Code Parser

**Location:** `src/knowledgeforge/ingestion/code.py`

**Powered by tree-sitter for AST-aware parsing.**

**Supported Languages:**
- Python (`.py`)
- JavaScript (`.js`, `.jsx`, `.mjs`)
- TypeScript (`.ts`, `.tsx`)
- Rust (`.rs`)
- Go (`.go`)
- C/C++ (`.c`, `.cpp`, `.h`, `.hpp`)
- Bash (`.sh`, `.bash`)

**Extraction Strategy:**
1. Parse file into AST
2. Extract definitions:
   - Functions/methods
   - Classes/structs
   - Imports/modules
3. Create chunks with context:
   - Include parent class for methods
   - Include docstrings
   - Preserve type annotations

**Example Chunk:**
```json
{
    "content": "def authenticate_user(username: str, password: str) -> User:\n    \"\"\"Authenticate user via OAuth2.\"\"\"\n    ...",
    "metadata": {
        "source": "/project/myapp/auth.py",
        "project": "myapp",
        "language": "python",
        "type": "function",
        "name": "authenticate_user",
        "line_start": 45,
        "line_end": 78,
        "parent_class": null,
        "docstring": "Authenticate user via OAuth2."
    }
}
```

---

### Incremental Indexing

**Hash-based change detection:**

1. Before indexing, compute `SHA256(file_content)`
2. Check against stored hash in metadata
3. If hash matches → skip (file unchanged)
4. If hash differs → re-index and update hash

**Benefits:**
- Dramatically faster re-indexing (only changed files)
- No unnecessary embedding computations
- Storage-efficient

**Stored in ChromaDB metadata:**
```json
{
    "file_hash": "a3f5e8d9c1b2...",
    "indexed_at": "2025-01-15T10:30:00Z"
}
```

---

## Search & Retrieval

### Vector Search

**Algorithm:** Hierarchical Navigable Small World (HNSW)
- Fast approximate nearest neighbor search
- O(log n) query time
- High recall with small index size

**Distance Metric:** Cosine similarity
- Normalized embeddings (L2 norm = 1)
- Range: -1 (opposite) to 1 (identical)
- Returned as distance (lower = better match)

**Filtering:**
- Pre-filtering via ChromaDB metadata filters
- Post-filtering for complex logic
- Project/collection scoping

---

### Hybrid Search (Future)

Planned for v0.2.0:
- **BM25 keyword search** for exact matches
- **Reciprocal Rank Fusion** for combining vector + keyword
- **Re-ranking** with cross-encoder models

---

### Context Window Management

For large result sets:
- Return top-K chunks (default K=10)
- Include metadata for source tracking
- Agent can request more results via pagination

---

## Discovery System

### Discovery Lifecycle

```
Created (unconfirmed) → Reviewed by human → Confirmed/Rejected
                                                 │
                                                 ▼
                                           Promoted to Obsidian
                                                 │
                                                 ▼
                                           Re-indexed as document
                                                 │
                                                 ▼
                                        Searchable by all agents
```

### Categories

- `bugfix` - Bug fixes and workarounds
- `optimization` - Performance improvements
- `insight` - Understanding gained
- `pattern` - Reusable patterns
- `workaround` - Temporary solutions

### Severity Levels

- `low` - Nice to know
- `medium` - Useful insight
- `high` - Important finding
- `critical` - Critical issue/fix

### Promotion to Obsidian

**File naming:**
```
{vault}/Discoveries/{YYYY-MM-DD}-{slug}.md
```

**Template:**
```markdown
---
title: {title}
created: {timestamp}
category: {category}
severity: {severity}
project: {project}
agent: {agent_name}
tags: [discovery, {category}]
---

# {title}

{content}

## Context

**Project:** {project}
**Category:** {category}
**Severity:** {severity}
**Discovered by:** {agent_name}
**Date:** {timestamp}

## Metadata

{JSON dump of metadata}
```

---

## Interface Layer

### MCP Server

**Location:** `src/knowledgeforge/interfaces/mcp_server.py`

**Protocol:** Model Context Protocol (MCP)
**Framework:** FastMCP

**Available Tools:**
1. `search_knowledge` - Search across all collections
2. `get_knowledge_context` - Read exact source lines after a search hit
3. `store_discovery` - Store agent insight
4. `get_project_context` - Get project overview
5. `list_projects` - List indexed projects
6. `ingest_path` - Index file/directory
7. `get_discoveries` - Retrieve discoveries
8. `search_conversations` - Search archived conversations
9. `read_conversation` - Read conversation transcript by session ID

**Tool Schema Example:**
```python
@mcp.tool()
def search_knowledge(query: str, project: str = "", max_results: int = 6) -> list[dict]:
    """Search the knowledge base and return lean snippets."""
    snippets = engine.search_snippets(query=query, project=project or None, max_results=max_results)
    return [s.model_dump() for s in snippets]
```

**Recommended usage in .mcp.json (shared endpoint):**
```json
{
    "mcpServers": {
        "knowledgeforge": {
            "command": "npx",
            "args": ["-y", "mcp-remote", "http://127.0.0.1:8743/mcp"]
        }
    }
}
```

**Direct stdio usage (legacy/simple):**
```json
{
    "mcpServers": {
        "knowledgeforge": {
            "command": "/home/bsdev/knowledgeforge/.venv/bin/python",
            "args": ["-m", "knowledgeforge.interfaces.mcp_server"]
        }
    }
}
```

---

### REST API

**Location:** `src/knowledgeforge/interfaces/rest_api.py`

**Framework:** FastAPI + Uvicorn
**Port:** 8742 (default)

**Endpoint Groups:**
- `/api/v1/search` - Search operations
- `/api/v1/discoveries` - Discovery CRUD
- `/api/v1/projects` - Project management
- `/api/v1/ingest` - Indexing operations
- `/api/v1/health` - Health checks

**Features:**
- OpenAPI/Swagger docs at `/docs`
- CORS enabled for browser access
- Pydantic request/response validation
- Structured error responses

---

### CLI

**Location:** `src/knowledgeforge/interfaces/cli.py`

**Framework:** Typer + Rich (colored output)

**Command Structure:**
```
knowledgeforge
  ├── index {vault|project|all}
  ├── search <query>
  ├── discoveries {list|review|confirm|reject|promote}
  ├── projects
  ├── stats
  ├── serve {--rest-only|--mcp-only}
  ├── watch
  └── config {show|init|validate}
```

**Interactive Features:**
- Rich tables for discovery review
- Progress bars for indexing
- Colored output for readability
- Confirmation prompts for destructive actions

---

## Storage Architecture

### ChromaDB Collections

**Schema:**

#### `documents` Collection
```
Vector: embedding (768-dim)
Metadata:
  - source: str (file path)
  - title: str
  - tags: List[str]
  - created_at: str (ISO date)
  - heading: str (section heading)
  - chunk_index: int
  - file_hash: str (SHA256)
```

#### `code` Collection
```
Vector: embedding (768-dim)
Metadata:
  - source: str (file path)
  - project: str
  - language: str (python, typescript, etc.)
  - type: str (function, class, module)
  - name: str (function/class name)
  - line_start: int
  - line_end: int
  - parent_class: Optional[str]
  - docstring: Optional[str]
  - file_hash: str (SHA256)
```

#### `discoveries` Collection
```
Vector: embedding (768-dim)
Metadata:
  - discovery_id: str (UUID)
  - category: str (bugfix, optimization, etc.)
  - severity: str (low, medium, high, critical)
  - project: Optional[str]
  - agent_name: Optional[str]
  - confirmed: bool
  - created_at: str (ISO timestamp)
  - confirmed_at: Optional[str]
```

---

### File Structure

```
~/.local/share/knowledgeforge/
  ├── chroma/              # ChromaDB persistent storage
  │   ├── documents/
  │   ├── code/
  │   └── discoveries/
  └── logs/
      └── knowledgeforge.log

~/.config/knowledgeforge/
  └── config.yaml          # User configuration
```

---

## Design Decisions

### Why ChromaDB?

**Alternatives considered:** Qdrant, Weaviate, Milvus, Pinecone

**Reasons for ChromaDB:**
- ✅ Embedded mode (no separate server)
- ✅ Persistent storage (survives restarts)
- ✅ Simple Python API
- ✅ Built-in metadata filtering
- ✅ HNSW index (fast search)
- ✅ Open source and local

---

### Why sentence-transformers?

**Alternatives considered:** OpenAI embeddings, Cohere, custom models

**Reasons for sentence-transformers:**
- ✅ Fully local (no API calls)
- ✅ Free and open source
- ✅ Wide model selection
- ✅ Good performance on code + text
- ✅ GPU support (CUDA, MPS)
- ✅ Easy model swapping

---

### Why tree-sitter for code parsing?

**Alternatives considered:** Regex, AST (ast module), language-specific parsers

**Reasons for tree-sitter:**
- ✅ Multi-language support (8+ languages)
- ✅ Fast and robust
- ✅ AST-aware (semantic chunking)
- ✅ Error-tolerant (handles incomplete code)
- ✅ Widely adopted (GitHub uses it)

---

### Why MCP for Claude Code integration?

**Alternatives considered:** Custom protocol, REST API only

**Reasons for MCP:**
- ✅ Official Anthropic protocol
- ✅ Standardized tool interface
- ✅ Better context sharing
- ✅ FastMCP framework simplicity
- ✅ Future-proof (growing ecosystem)

---

### Why local-first architecture?

**Alternatives considered:** Cloud-hosted, hybrid

**Reasons for local-first:**
- ✅ Privacy (no data leaves machine)
- ✅ No API costs
- ✅ Instant response times
- ✅ Works offline
- ✅ Full control over data
- ✅ Aligns with Obsidian philosophy

---

## Performance Considerations

### Indexing Performance

**Bottlenecks:**
1. Embedding generation (GPU helps significantly)
2. File I/O (SSD recommended)
3. Tree-sitter parsing (CPU-bound)

**Optimizations:**
- Batch embedding generation (32-256 items)
- Parallel file processing (4-8 threads)
- Incremental indexing (SHA256 hashing)
- Skip large files (>10MB default)

**Benchmarks** (M1 MacBook Pro, 10K documents):
- Initial indexing: ~5 min
- Incremental re-index: ~10 sec (95% skipped)
- Search query: ~50ms

---

### Search Performance

**Factors:**
- Collection size
- Number of results requested
- Metadata filtering complexity

**Optimizations:**
- HNSW index (log(n) search time)
- Pre-filtering in ChromaDB
- Embedding caching (LRU cache)

---

### Memory Usage

**Typical footprint:**
- Embedding model: ~500MB (loaded once)
- ChromaDB: ~100MB per 10K documents
- Python runtime: ~200MB

**Total:** ~1GB for moderate usage

---

## Security Model

### Threat Model

**Current scope:** Local single-user system

**Assumptions:**
- Trusted user
- Trusted filesystem access
- No network exposure (localhost only)

**Future considerations:**
- Multi-user access control
- API authentication (JWT)
- TLS for remote access

---

### Data Privacy

**Guarantees:**
- All data stays local
- No telemetry or analytics
- No external API calls (except HuggingFace model downloads)

---

## Future Architecture

### Planned Enhancements (v0.2.0)

1. **Hybrid Search**
   - BM25 keyword search
   - Reciprocal Rank Fusion
   - Re-ranking models

2. **GraphRAG**
   - Entity extraction
   - Relationship graphs
   - Knowledge graph queries

3. **Multi-Vault Support**
   - Separate collections per vault
   - Cross-vault search
   - Vault-specific permissions

4. **Web UI**
   - Discovery review interface
   - Search playground
   - Configuration management

5. **Cloud Sync (Optional)**
   - S3-compatible storage
   - Encrypted backups
   - Multi-device sync

---

### Long-Term Vision

**KnowledgeForge as Universal Agent Memory:**
- Every agent stores learnings in KnowledgeForge
- Learnings shared across agent ecosystem
- Human curates and validates
- Validated knowledge re-ingested
- Continuous improvement loop

**Integration Targets:**
- Slack/Discord bots
- CI/CD pipelines (automated docs)
- IDE extensions (in-editor search)
- Web annotation tools (save highlights to vault)

---

## Conclusion

KnowledgeForge is designed as a **lightweight, local-first RAG system** that bridges personal knowledge management (Obsidian) with agent-driven development workflows. The architecture prioritizes:
- **Privacy** (local-only)
- **Speed** (incremental indexing, HNSW search)
- **Flexibility** (multi-interface, multi-language)
- **Simplicity** (no complex setup, embedded DB)

The discovery system creates a **virtuous learning loop** where agents contribute to knowledge bases and humans validate insights, creating living documentation that improves over time.

---

For implementation details, see:
- [Configuration Guide](configuration.md)
- [API Reference](api-reference.md)
- [Source Code](../src/knowledgeforge/)

For questions or suggestions, open an issue:
https://github.com/tiaz-fr/knowledgeforge/issues
