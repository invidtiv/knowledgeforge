# Episodic Memory: Architecture Analysis, Pitfalls & KnowledgeForge Integration Plan

> **Author**: Claude Code (auto-generated)
> **Date**: 2026-02-09
> **Status**: Draft / Proposal
> **Scope**: Replace episodic-memory plugin with a KnowledgeForge `conversations` collection

---

## Table of Contents

1. [How Episodic Memory Works](#1-how-episodic-memory-works)
2. [Pitfalls & Limitations](#2-pitfalls--limitations)
3. [KnowledgeForge vs Episodic Memory Comparison](#3-knowledgeforge-vs-episodic-memory-comparison)
4. [Integration Plan](#4-integration-plan)
5. [Kimi-Powered Metadata Enrichment](#5-kimi-powered-metadata-enrichment)
6. [Migration Strategy](#6-migration-strategy)
7. [Risk Assessment](#7-risk-assessment)

---

## 1. How Episodic Memory Works

### 1.1 Overview

Episodic Memory is a Claude Code plugin (by Jesse Vincent, `obra/episodic-memory`) that archives and indexes conversations from Claude Code, Codex, and Gemini sessions. It provides cross-session memory by storing conversation exchanges in a SQLite database with optional vector embeddings for semantic search.

### 1.2 Data Flow

```
Source Conversations (live)
  ~/.claude/projects/{project}/{session}.jsonl     (Claude Code)
  ~/.config/superpowers/conversation-archive/_codex/  (Codex - pre-imported)
  ~/.config/superpowers/conversation-archive/_gemini/  (Gemini - pre-imported)
        |
        v
  [sync.js] -- copies JSONL files if newer (atomic: write temp -> rename)
        |
        v
  Conversation Archive (persistent)
  ~/.config/superpowers/conversation-archive/{project}/{session}.jsonl
        |
        v
  [parser.js] -- reads JSONL line-by-line, extracts user/assistant pairs
        |        -- generates deterministic IDs: MD5(archive_path:startLine-endLine)
        |        -- preserves metadata: session_id, cwd, git_branch, version, thinking_level
        v
  Parsed Exchanges (in memory)
  ConversationExchange { id, project, timestamp, userMessage, assistantMessage, toolCalls[] }
        |
        |---> [embeddings.js] -- Xenova/all-MiniLM-L6-v2 (ONNX, local)
        |                     -- combines user + assistant + tool names
        |                     -- truncates to 2000 chars -> 384-dim float32 vector
        |
        |---> [summarizer.js] -- Claude API (haiku/sonnet) for AI summaries
        |                     -- hierarchical: chunks of 8 for long conversations (>15 exchanges)
        |                     -- writes {session}-summary.txt alongside JSONL
        v
  SQLite Database
  ~/.config/superpowers/conversation-index/db.sqlite
        |
        |--> exchanges table     (494 rows) -- full exchange data + embedding BLOB
        |--> tool_calls table    (6691 rows) -- tool invocations linked to exchanges
        |--> vec_exchanges table (virtual)   -- sqlite-vec FLOAT[384] for KNN search
        v
  [search.js] -- vector search: sqlite-vec KNN (MATCH ? AND k = ?)
              -- text search: LIKE '%query%' on user_message/assistant_message
              -- multi-concept AND: independent vector searches, intersect, avg similarity
              -- date filtering: BETWEEN on timestamp
        |
        v
  [mcp-server.js] -- exposes 2 MCP tools: search + read
                   -- search: query -> ranked results with snippets
                   -- read: path + line range -> formatted markdown conversation
```

### 1.3 Trigger Mechanism

The plugin hooks into Claude Code's `SessionStart` event:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume",
      "hooks": [{
        "type": "command",
        "command": "node ${CLAUDE_PLUGIN_ROOT}/cli/episodic-memory.js sync --background",
        "async": true
      }]
    }]
  }
}
```

On every session start (new or resumed), it:
1. Copies new/updated JSONL files from `~/.claude/projects/` to the archive
2. Parses new exchanges
3. Generates embeddings (if model loaded)
4. Inserts into SQLite + vec_exchanges
5. Optionally generates AI summaries via Claude API

### 1.4 Search Modes

| Mode | Method | Ranking | Speed |
|------|--------|---------|-------|
| **vector** | sqlite-vec KNN on 384-dim embeddings | cosine distance (0=identical) | Fast (~10ms) |
| **text** | SQL LIKE '%query%' on messages | timestamp (newest first) | Medium |
| **both** | vector + text, deduplicated | vector results first, then text | Slower |
| **multi-concept** | N independent vector searches, intersect | average similarity across concepts | Slowest |

### 1.5 Embedding Details

- **Model**: `Xenova/all-MiniLM-L6-v2` (Sentence-BERT variant)
- **Runtime**: Transformers.js (ONNX Runtime, CPU-only, local)
- **Dimensions**: 384 (float32)
- **Input**: `"User: {msg}\n\nAssistant: {msg}\n\nTools: {tool1}, {tool2}"` truncated to 2000 chars
- **Pooling**: Mean pooling with L2 normalization
- **Model size**: ~33 MB ONNX

### 1.6 Database Schema

```sql
-- Main conversation data
CREATE TABLE exchanges (
  id TEXT PRIMARY KEY,           -- MD5(archive_path:startLine-endLine)
  project TEXT NOT NULL,
  timestamp TEXT NOT NULL,       -- ISO 8601
  user_message TEXT NOT NULL,
  assistant_message TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  embedding BLOB,                -- Float32Array buffer (384 * 4 bytes)
  last_indexed INTEGER,
  parent_uuid TEXT,
  is_sidechain BOOLEAN DEFAULT 0,
  session_id TEXT,
  cwd TEXT,
  git_branch TEXT,
  claude_version TEXT,
  thinking_level TEXT,
  thinking_disabled BOOLEAN,
  thinking_triggers TEXT         -- JSON array
);

-- Tool invocations
CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  exchange_id TEXT NOT NULL REFERENCES exchanges(id),
  tool_name TEXT NOT NULL,
  tool_input TEXT,               -- JSON stringified
  tool_result TEXT,
  is_error BOOLEAN DEFAULT 0,
  timestamp TEXT NOT NULL
);

-- Vector search (sqlite-vec extension)
CREATE VIRTUAL TABLE vec_exchanges USING vec0(
  id TEXT PRIMARY KEY,
  embedding FLOAT[384]
);
```

### 1.7 Current Stats (this server)

| Metric | Value |
|--------|-------|
| Total exchanges | 494 |
| Tool call records | 6,691 |
| Unique conversations | 124 |
| Projects indexed | 11 (Claude Code) + 2 (Codex, Gemini) |
| Date range | 2025-11-10 to 2026-02-04 |
| Database size | 11 MB |
| Embeddings populated | **0 / 494 (0%)** |

---

## 2. Pitfalls & Limitations

### 2.1 CRITICAL: Embeddings Are Not Working

**The most significant issue**: 0% of exchanges have embeddings. The `vec_exchanges` table exists but is empty. This means:
- **Vector/semantic search is completely non-functional**
- Only text-based LIKE search works (basic substring matching)
- The "RAG" in this system is effectively just a text search over a SQLite database
- Multi-concept AND search (which requires vector intersection) returns nothing

**Root cause**: The embedding generation step (`generateExchangeEmbedding()`) either fails silently or is skipped during sync. The `Xenova/all-MiniLM-L6-v2` model may fail to load in the background sync context, and errors are swallowed.

### 2.2 Node.js Version Fragility

The plugin uses `better-sqlite3` (native C++ addon) and `sqlite-vec` (native extension). These are compiled against a specific Node.js ABI version:
- **Compiled for**: Node 20 (MODULE_VERSION 115)
- **System runs**: Node 22 (MODULE_VERSION 127)
- **Result**: MCP server crashes on startup with `NODE_MODULE_VERSION` mismatch
- **Fix**: `npm rebuild better-sqlite3` in the plugin directory, but this breaks again on Node upgrades
- **Impact**: After Node upgrade, episodic memory silently stops working until manually rebuilt

### 2.3 Weak Embedding Model

`all-MiniLM-L6-v2` is a lightweight model optimized for speed over quality:
- **384 dimensions** (vs KnowledgeForge's nomic-embed at 768 dimensions)
- No task-specific prefixing (nomic uses `search_document:` / `search_query:` prefixes)
- Trained on general NLI data, not optimized for code/technical conversations
- Truncates input to 2000 chars (~512 tokens) - loses context in long exchanges

### 2.4 No Incremental Change Detection

Unlike KnowledgeForge (which uses SHA256 file hashes), episodic memory:
- Relies on file modification time (`copyIfNewer()`)
- Re-parses entire JSONL files even if only new messages were appended
- No content-based deduplication - the same exchange can be re-indexed if the archive path changes
- Uses MD5 for IDs (cryptographically weak, though not a security concern here)

### 2.5 AI Summarization Cost & Reliability

- Calls Claude API (haiku/sonnet) for every conversation summary
- No caching strategy - regenerates if summary file is missing
- Hierarchical summarization for long conversations adds API calls
- Requires `ANTHROPIC_API_KEY` or custom endpoint configuration
- Fallback model switching on `thinking.budget_tokens` errors is fragile
- Summary quality varies - sometimes produces "This was a trivial conversation"

### 2.6 Single-User, Single-Machine Architecture

- SQLite with WAL mode handles concurrent reads, but not distributed access
- No authentication or access control
- No way to share conversation memory across machines
- Archive directory grows unboundedly (no cleanup/rotation)

### 2.7 No Code-Aware Parsing

Tool calls are stored as raw JSON strings:
- `tool_input` is the full JSON of what was passed to Read/Edit/Bash/etc.
- `tool_result` is the full output text
- No AST parsing, no language detection, no semantic chunking of code
- Search over tool results is just substring matching

### 2.8 Search Quality Issues

- **Text search**: Case-insensitive LIKE is slow on large datasets (no FTS5 index)
- **No relevance scoring in text mode**: Results ordered by timestamp, not relevance
- **No hybrid ranking**: "both" mode just deduplicates, doesn't combine scores
- **No filtering by tool type**: Can't search "all exchanges where Bash was used"
- **No project-scoped search**: Must search all projects at once (no project filter in MCP tools)

### 2.9 Silent Failures

- Background sync (`--background` flag) suppresses errors
- If the MCP server fails to start (like the Node version issue), Claude Code silently falls back to no memory
- No health monitoring, no alerting, no status endpoint
- The `verify.js` module exists but is never called automatically

### 2.10 Conversation Format Coupling

- Tightly coupled to Claude Code's internal JSONL format
- Codex and Gemini conversations require manual import into the archive
- Format changes in Claude Code updates could break parsing
- No schema versioning for the JSONL format

### 2.11 Storage Inefficiency

- Full conversation text stored in both:
  - The JSONL archive files
  - The `user_message` and `assistant_message` columns in SQLite
- Tool results (which can be massive - file contents, command outputs) stored verbatim
- 11 MB database for only 494 exchanges - scales poorly
- No compression, no pruning of old/irrelevant data

---

## 3. KnowledgeForge vs Episodic Memory Comparison

| Feature | Episodic Memory | KnowledgeForge |
|---------|----------------|----------------|
| **Vector DB** | sqlite-vec (single-file) | ChromaDB (HNSW index, persistent) |
| **Embedding model** | all-MiniLM-L6-v2 (384-dim) | nomic-embed-text-v1.5 (768-dim) |
| **Embedding quality** | General-purpose, small | Task-prefixed, larger, code-aware |
| **Content parsing** | Raw message text, truncated | AST-aware code, heading-aware markdown |
| **Chunking** | Whole exchange (user+assistant) | Token-based with overlap, semantic boundaries |
| **Change detection** | File mtime comparison | SHA256 content hashing |
| **Deduplication** | None (MD5 ID based on path) | Cosine similarity > 0.9 for discoveries |
| **Search** | Vector KNN + LIKE text | ChromaDB semantic + metadata filters |
| **Filtering** | Date range only | Project, tags, language, category, score threshold |
| **API** | MCP only (2 tools) | MCP (6 tools) + REST API (9 endpoints) |
| **Multi-agent** | Claude Code only | Claude Code + any HTTP client |
| **Live sync** | SessionStart hook | File watcher (watchdog) with debouncing |
| **Language** | Node.js / TypeScript | Python |
| **Native deps** | better-sqlite3, sqlite-vec (fragile) | ChromaDB, sentence-transformers (stable) |
| **Summaries** | AI-generated via Claude API | Not applicable (chunk-level indexing) |
| **Write-back** | None | Discovery promotion to Obsidian |
| **Collections** | Single (exchanges) | 3 (documents, codebase, discoveries) |

### Why KnowledgeForge is the better foundation

1. **ChromaDB is battle-tested** - HNSW index, persistent storage, no native module version issues
2. **nomic-embed is superior** - 768 dims, task prefixes, trained on code+text, actively maintained
3. **Already running** - REST API on port 8742, file watcher, systemd services
4. **Multi-collection design** - Easy to add a `conversations` collection alongside existing ones
5. **Python ecosystem** - sentence-transformers, tree-sitter, watchdog all first-class
6. **Metadata filtering** - ChromaDB supports `where` clauses on any metadata field
7. **Incremental indexing** - SHA256 hash-based change detection already implemented

---

## 4. Integration Plan

### 4.1 Goal

Add a `conversations` collection to KnowledgeForge that replaces episodic memory entirely:
- Index Claude Code, Codex, and Gemini conversations
- Provide semantic search with the same nomic-embed model used for docs/code
- Expose via both MCP and REST API
- Auto-sync on file changes (using existing watcher infrastructure)
- Maintain backward compatibility with existing episodic memory search/read tools

### 4.2 Architecture

```
Conversation Sources
  ~/.claude/projects/{project}/{session}.jsonl        (Claude Code - live)
  ~/.config/superpowers/conversation-archive/_codex/   (Codex)
  ~/.config/superpowers/conversation-archive/_gemini/  (Gemini)
        |
        v
  [ConversationParser] -- new module in knowledgeforge/ingestion/
        |  -- reads JSONL format (same as episodic memory parser.js)
        |  -- extracts exchanges with metadata
        |  -- generates conversation chunks:
        |       1. Per-exchange chunks (user + assistant, truncated tool results)
        |       2. Session summary chunks (first + last exchange, tool usage stats)
        v
  [Embedder] -- existing nomic-embed-text-v1.5 (768-dim, task-prefixed)
        |     -- "search_document: User: {msg} Assistant: {msg} Tools: {tools}"
        v
  [VectorStore] -- existing ChromaDB wrapper
        |        -- collection: "conversations"
        |        -- metadata: project, session_id, timestamp, cwd, git_branch,
        |                     claude_version, exchange_index, tool_names,
        |                     source_agent (claude|codex|gemini)
        v
  ChromaDB "conversations" collection
  ~/.local/share/knowledgeforge/chromadb/
        |
        |--> [MCP Server] -- existing + new tools:
        |      search_conversations(query, project, after, before, source_agent, limit)
        |      read_conversation(session_id, start_line, end_line)
        |
        |--> [REST API] -- existing + new endpoints:
        |      POST /api/v1/conversations/search
        |      GET  /api/v1/conversations/{session_id}
        |      POST /api/v1/conversations/sync
        |
        |--> [File Watcher] -- extend existing watcher to also watch:
               ~/.claude/projects/ for new/updated JSONL files
```

### 4.3 Implementation Phases

#### Phase 1: Conversation Parser Module

**New file**: `src/knowledgeforge/ingestion/conversations.py`

```
ConversationParser:
  parse_jsonl(file_path) -> list[ConversationExchange]
    - Read JSONL line by line
    - Extract user/assistant pairs
    - Parse tool_use / tool_result blocks
    - Preserve metadata (session_id, cwd, git_branch, timestamp, etc.)
    - Detect source agent (claude/codex/gemini from archive path)

  chunk_exchange(exchange) -> list[Chunk]
    - Create main chunk: "User: {msg}\nAssistant: {msg}"
    - Truncate tool results to 500 chars each (keep tool name + error status)
    - If exchange is very long (>2000 tokens): split into sub-chunks with overlap
    - Metadata: all exchange fields flattened for ChromaDB

  chunk_session(exchanges) -> Chunk
    - Session summary chunk: first exchange question, last exchange, tool usage stats
    - Useful for "what was that session about?" queries

  detect_source(archive_path) -> str
    - "_codex" in path -> "codex"
    - "_gemini" in path -> "gemini"
    - else -> "claude"
```

**New data model** in `src/knowledgeforge/core/models.py`:

```python
class ConversationExchange:
    exchange_id: str                    # Deterministic from path+lines
    session_id: str
    project: str
    timestamp: str                      # ISO 8601
    user_message: str
    assistant_message: str
    source_agent: str                   # claude | codex | gemini
    archive_path: str
    line_start: int
    line_end: int
    cwd: str = ""
    git_branch: str = ""
    claude_version: str = ""
    thinking_level: str = ""
    tool_names: list[str] = []          # ["Bash", "Read", "Edit"]
    tool_error_count: int = 0
    is_sidechain: bool = False
    parent_uuid: str = ""
```

#### Phase 2: Engine Integration

**Modify**: `src/knowledgeforge/core/engine.py`

```
New methods:
  ingest_conversations(source_dirs=None) -> IngestResult
    - Default sources: ~/.claude/projects/, conversation-archive/_codex/, _gemini/
    - SHA256 hash-based change detection (reuse existing pattern)
    - Parse -> chunk -> embed -> store in "conversations" collection
    - Track last-indexed per file

  search_conversations(query, project, after, before, source_agent, limit) -> SearchResponse
    - Embed query with "search_query: " prefix
    - ChromaDB query with metadata filters:
        where={"project": project, "source_agent": "claude"}
        where_document (date range via timestamp metadata)
    - Return SearchResponse with conversation-specific formatting

  get_conversation(session_id, start_line, end_line) -> str
    - Read raw JSONL from archive, format as markdown
    - Reuse episodic memory's show.js formatting logic (ported to Python)

  sync_conversations() -> dict
    - Copy from ~/.claude/projects/ to archive (if not already there)
    - Index new/changed files
    - Return stats: {copied, indexed, skipped, errors}
```

**Modify config** (`KnowledgeForgeConfig`):

```python
# New config fields
conversation_sources: list[str] = [
    "~/.claude/projects",
    "~/.config/superpowers/conversation-archive/_codex",
    "~/.config/superpowers/conversation-archive/_gemini"
]
conversation_archive_dir: str = "~/.config/superpowers/conversation-archive"
conversations_collection: str = "conversations"
conversation_max_tool_result_chars: int = 500
conversation_sync_on_start: bool = True
```

#### Phase 3: MCP + REST API Exposure

**Modify**: `src/knowledgeforge/interfaces/mcp_server.py`

```
New MCP tools:

  search_conversations(
    query: str,                        # Semantic search query
    project: str = None,               # Filter by project
    source_agent: str = None,          # "claude" | "codex" | "gemini"
    after: str = None,                 # "YYYY-MM-DD"
    before: str = None,                # "YYYY-MM-DD"
    n_results: int = 10,
    mode: str = "vector"               # "vector" | "text" | "both"
  ) -> formatted results

  read_conversation(
    session_id: str,                   # UUID of conversation
    start_line: int = None,
    end_line: int = None
  ) -> markdown formatted conversation
```

**Modify**: `src/knowledgeforge/interfaces/rest_api.py`

```
New endpoints:

  POST /api/v1/conversations/search    # Same params as MCP tool
  GET  /api/v1/conversations/{session_id}  # Read single conversation
  POST /api/v1/conversations/sync      # Trigger manual sync
  GET  /api/v1/conversations/stats     # Conversation-specific stats
```

#### Phase 4: Watcher Integration

**Modify**: `src/knowledgeforge/ingestion/watcher.py`

```
Extend watcher to also monitor:
  ~/.claude/projects/         # Live Claude Code conversations
  conversation-archive/       # For manually imported Codex/Gemini

On JSONL file change:
  - Debounce (2 seconds, same as existing)
  - Parse only new lines (track last-processed line per file)
  - Generate embeddings for new exchanges
  - Upsert into "conversations" collection
```

#### Phase 5: Migration & Backward Compatibility

**One-time migration script**: `scripts/migrate_episodic_memory.py`

```
1. Read all exchanges from ~/.config/superpowers/conversation-index/db.sqlite
2. For each exchange:
   a. Create Chunk with nomic-embed embedding (re-embed, don't reuse 384-dim)
   b. Preserve all metadata (project, session_id, timestamp, etc.)
   c. Add source_agent detection
   d. Insert into ChromaDB "conversations" collection
3. Verify: count in ChromaDB matches count in SQLite
4. Report: migrated X exchanges, Y tool calls, from Z projects
```

**MCP tool aliasing** (temporary):

Register `episodic-memory` MCP tools as aliases pointing to KnowledgeForge conversation tools, so existing agent definitions and skills continue working without modification.

#### Phase 6: Cleanup

After validation:
1. Remove episodic-memory from `~/.claude/plugins/installed_plugins.json`
2. Remove episodic-memory MCP server from `~/.mcp.json`
3. Update Claude Code skills to reference KnowledgeForge conversation tools
4. Archive (don't delete) the old SQLite database
5. Update AGENTS.md to reflect the consolidated architecture

---

## 5. Kimi-Powered Metadata Enrichment

### 5.1 Rationale

The original episodic memory embeds raw `user_message + assistant_message` text, which produces poor semantic search results because:
- Conversations are messy (IDE tags, system reminders, tool outputs)
- The actual intent is buried in noise
- No topic/category signals for filtering
- No searchable keyword expansion

**Solution**: Use Kimi CLI (`kimi-k2.5`, a cheap coding model) to generate rich metadata for each exchange before embedding. This creates a **semantically dense embedding document** that combines AI-extracted summaries, topics, technologies, and intent with the original conversation text.

### 5.2 Enrichment Pipeline

```
SQLite (494 exchanges)
    |
    v
[enrich_episodic_memory.py]
    |
    |-- Read exchange from DB
    |-- Clean messages (strip IDE/system tags, truncate)
    |-- Build prompt with exchange context
    |
    v
[Kimi CLI] --print --final-message-only --no-thinking
    |
    |-- Returns JSON metadata:
    |     summary, category, topics, technologies,
    |     outcome, intent, complexity, key_files, searchable_text
    |
    v
[Build embedding_content]
    |
    |-- Combines: summary + intent + category + topics + technologies
    |             + cleaned user/assistant messages + tools + searchable_text
    |-- This rich text is what gets embedded by nomic-embed
    |
    v
Output: enriched_conversations/{exchange_id}.json
    |
    v
[KnowledgeForge ingest] -> ChromaDB "conversations" collection
```

### 5.3 Enrichment Schema

Each exchange is enriched with:

| Field | Type | Purpose |
|-------|------|---------|
| `summary` | str | 1-2 sentence description of what happened |
| `category` | enum | setup, bugfix, feature, refactor, config, debug, research, deployment, documentation, maintenance, conversation |
| `topics` | list | 3-5 key technical topics discussed |
| `technologies` | list | Specific frameworks/tools/languages mentioned |
| `outcome` | enum | success, partial, failure, ongoing, informational |
| `intent` | str | What the user was trying to accomplish (5-10 words) |
| `complexity` | enum | trivial, simple, moderate, complex, very_complex |
| `key_files` | list | File paths mentioned or worked on |
| `searchable_text` | str | Dense paragraph of keywords/concepts for search |

### 5.4 Embedding Content Assembly

The `embedding_content` field is the text that actually gets embedded by nomic-embed. It's structured to maximize semantic search quality:

```
Summary: {AI-generated summary}
Intent: {what user wanted}
Category: {category}
Topics: {topic1}, {topic2}, {topic3}
Technologies: {tech1}, {tech2}
User: {cleaned user message, 800 chars}
Assistant: {cleaned assistant message, 800 chars}
Tools: {tool1}, {tool2}
Context: {searchable_text from Kimi}
```

This structured embedding content is the core value of the Kimi enrichment. Instead of embedding raw noisy conversation text, we embed a curated document that front-loads semantic signals (summary, intent, topics) before the raw content.

### 5.5 Pipeline Script

**Location**: `scripts/enrich_episodic_memory.py`

**Features**:
- Reads all 498 exchanges from episodic memory SQLite
- Calls Kimi CLI per exchange (`--print --final-message-only --no-thinking`)
- Parses JSON response (handles markdown fencing)
- Builds enriched embedding content
- Saves per-exchange JSON files to `data/enriched_conversations/`
- Checkpoint/resume support (saves progress every 10 exchanges)
- Fallback to basic metadata on Kimi timeout/failure
- Rate limiting (1 second between Kimi calls)

**Usage**:
```bash
# Preview
python scripts/enrich_episodic_memory.py --dry-run

# Process 10 exchanges
python scripts/enrich_episodic_memory.py --batch-size 10

# Full run (all 498 exchanges)
python scripts/enrich_episodic_memory.py

# Resume after interruption
python scripts/enrich_episodic_memory.py --resume

# Skip Kimi, use basic metadata only
python scripts/enrich_episodic_memory.py --skip-enrichment
```

### 5.6 Tested Results

Test run on 3 exchanges (2026-02-09):

| Exchange | Source | Kimi Result | Category | Outcome |
|----------|--------|-------------|----------|---------|
| d93c6583 | gemini | Enriched | setup | success |
| a58f79e9 | gemini | Enriched | documentation | success |
| ba25502a | gemini | Timeout -> fallback | conversation | informational |

**Kimi-enriched example** (exchange d93c6583):
- **Summary**: "User initialized a project setup for a Telegram bot, installed dependencies (telethon, python-dotenv), started the bot, identified git repository..."
- **Topics**: telegram bot setup, git branch management, environment configuration, dependency management, directory merging
- **Technologies**: telethon, python-dotenv, git, telegram api, python
- **Searchable text**: "Telegram bot initialization telethon python-dotenv virtual environment git repository..."

**Cost estimate**: ~60 seconds per exchange via Kimi CLI, ~498 minutes (~8.3 hours) for full database. Can be run overnight with `--resume`.

---

## 6. Migration Strategy

### 6.1 Data Migration

```
Old data locations:
  ~/.config/superpowers/conversation-archive/   <- KEEP (shared archive)
  ~/.config/superpowers/conversation-index/db.sqlite  <- ARCHIVE after migration

New data location:
  ~/.local/share/knowledgeforge/chromadb/  <- conversations collection added here
```

The conversation archive directory remains as-is. KnowledgeForge indexes from it.

### 6.2 Migration Steps

| Step | Action | Reversible |
|------|--------|-----------|
| 1 | Run `enrich_episodic_memory.py` (full Kimi enrichment) | Yes |
| 2 | Add conversation parser + models to KnowledgeForge | Yes |
| 3 | Ingest enriched JSON files into ChromaDB `conversations` collection | Yes (delete collection) |
| 4 | Add MCP + REST API tools for conversation search | Yes |
| 5 | Test search quality vs old system | N/A |
| 6 | Extend watcher for `~/.claude/projects/` | Yes |
| 7 | Disable episodic-memory plugin SessionStart hook | Yes (re-enable) |
| 8 | Remove episodic-memory MCP server from `~/.mcp.json` | Yes (re-add) |
| 9 | Update skills/agents to use new tool names | Yes |
| 10 | Archive old db.sqlite | Yes |

### 6.3 Rollback Plan

If issues arise:
1. Re-enable episodic-memory plugin (still installed in cache)
2. Add back MCP server to `~/.mcp.json`
3. Old archive + SQLite are untouched
4. Delete KnowledgeForge `conversations` collection without affecting docs/code/discoveries

---

## 7. Risk Assessment

### Low Risk
- **Data loss**: Archive files are never modified, only read. ChromaDB collection is additive.
- **Breaking existing search**: KnowledgeForge's existing collections are unaffected.
- **Performance**: ChromaDB handles much larger datasets than 498 exchanges.

### Medium Risk
- **Search quality regression**: nomic-embed may rank differently than all-MiniLM-L6-v2. Mitigation: side-by-side comparison before cutover.
- **JSONL format changes**: Claude Code updates could change format. Mitigation: parser should be lenient.
- **Kimi enrichment quality**: Some exchanges may get poor metadata. Mitigation: fallback to basic metadata, can re-enrich later.
- **Watcher overhead**: Monitoring `~/.claude/projects/` adds file descriptors. Mitigation: use polling.

### High Risk
- **AI summary loss**: Episodic memory has AI summaries per conversation that won't be migrated. Mitigation: Kimi enrichment generates per-exchange summaries which are arguably more useful for search.
- **Tool result indexing**: KnowledgeForge truncates tool results. Mitigation: store full text in metadata (not embedded) for display.

---

## Appendix A: File Inventory

### Episodic Memory (to be replaced)

```
Plugin:     ~/.claude/plugins/cache/superpowers-marketplace/episodic-memory/1.0.15/
Database:   ~/.config/superpowers/conversation-index/db.sqlite (11 MB)
Archive:    ~/.config/superpowers/conversation-archive/ (13 project dirs, 503 JSONL files)
MCP:        ~/.mcp.json -> episodic-memory server
Hooks:      SessionStart -> sync --background
Skills:     episodic-memory:search-conversations, episodic-memory:remembering-conversations
Agents:     search-conversations (haiku model)
```

### KnowledgeForge (to be expanded)

```
Project:    /home/bsdev/knowledgeforge/
Venv:       /home/bsdev/knowledgeforge/.venv/
Config:     ~/.config/knowledgeforge/config.yaml
Data:       ~/.local/share/knowledgeforge/chromadb/ (11 MB)
MCP:        ~/.mcp.json -> knowledgeforge server
REST API:   127.0.0.1:8742
Services:   knowledgeforge-api.service, knowledgeforge-watcher.service
Collections: documents, codebase, discoveries -> +conversations (new)
```

### New Files Created

```
scripts/enrich_episodic_memory.py   -- Kimi enrichment pipeline
data/enriched_conversations/        -- Output JSON files (one per exchange)
docs/episodic-memory-integration-plan.md  -- This document
```

---

## Appendix B: Estimated Effort

| Phase | Description | Effort |
|-------|-------------|--------|
| Enrichment | Run Kimi on 498 exchanges (overnight) | ~8 hours unattended |
| Phase 1 | Conversation parser module | ~200 lines Python |
| Phase 2 | Engine integration (ingest + search + sync) | ~300 lines Python |
| Phase 3 | MCP + REST API endpoints | ~150 lines Python |
| Phase 4 | Watcher extension | ~50 lines Python |
| Phase 5 | Migration script (ingest enriched JSON) | ~100 lines Python |
| Phase 6 | Cleanup + config + docs | Config changes only |
| **Total code** | | **~800 lines Python** |

The existing KnowledgeForge infrastructure (embedder, store, chunker, watcher, MCP server, REST API) handles 70% of the work. The new code is primarily the JSONL parser and the glue to integrate conversations into the existing pipeline.