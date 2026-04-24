# OB1 + KnowledgeForge Integration Analysis — 2026-04-23

## Overview

This document captures the findings from a parallel analysis of **OB1 (Open Brain)** and **KnowledgeForge**, identifying integration opportunities in both directions: bringing OB1 workflows into KnowledgeForge, and enabling OB1 to leverage KnowledgeForge's knowledge capabilities.

---

## System Profiles

### OB1 (Open Brain)

A **protocol-first persistent AI memory layer** built on Supabase + pgvector.

- **Core MCP tools**: `capture_thought`, `search_thoughts`, `list_thoughts`, `thought_stats`
- **Embeddings**: OpenRouter (text-embedding-3-small, 1536-dim)
- **Metadata**: LLM-extracted on capture (type, topics, people, actions)
- **Deduplication**: SHA-256 content fingerprinting with metadata merge on conflict
- **Content library**: 40+ recipes (workflows), 10+ skills, 6 extensions, database schemas, dashboards
- **Runtime**: Supabase Edge Functions (Deno/TypeScript), HTTP MCP transport
- **Storage**: PostgreSQL with HNSW vector index, GIN metadata index, JSONB metadata
- **Auth**: URL query param `?key=` or `x-brain-key` header

**Key data model — `thoughts` table:**
```sql
CREATE TABLE thoughts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  embedding VECTOR(1536),
  metadata JSONB DEFAULT '{}',
  content_fingerprint TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

**Metadata JSONB structure (auto-extracted):**
```json
{
  "type": "observation|task|idea|reference|person_note",
  "people": ["Alice", "Bob"],
  "action_items": ["Call Alice"],
  "dates_mentioned": ["2026-04-23"],
  "topics": ["engineering", "product"],
  "source": "mcp"
}
```

### KnowledgeForge

A **local RAG knowledge system** built on ChromaDB + sentence-transformers.

- **7 collections**: documents, codebase, discoveries, conversations, facts, runbooks, project_overviews
- **Search**: Hybrid (70% vector cosine + 30% BM25 keyword), trust-tier score boosting
- **Discovery lifecycle**: unconfirmed → confirmed → promoted to semantic memory
- **Trust model**: T1 (authoritative) through T4 (unverified), with score multipliers
- **Code parsing**: Tree-sitter AST-aware chunking (8 languages)
- **Runtime**: Python (FastAPI REST on port 8742, FastMCP for Claude Code)
- **Storage**: ChromaDB (embedded, persistent) + SQLite (keyword index)
- **Ingestion**: Obsidian vaults, code repos, Claude/Codex/Gemini conversations

---

## Direction 1: OB1 Workflows → KnowledgeForge

### Capabilities OB1 has that KnowledgeForge lacks

| OB1 Capability | KF Gap | Effort |
|---|---|---|
| **Auto-Capture recipe** — session-end capture with "Panning for Gold" quality triage | No session-end hooks or auto-triage | Medium |
| **Work Operating Model** — 5-layer elicitation interview → structured JSON + narrative exports (USER.md, SOUL.md, HEARTBEAT.md) | No structured interview/elicitation workflow | Medium |
| **Email/Chat import recipes** — Gmail MBOX, ChatGPT JSON, Slack export, Discord bot ingestion | Only ingests Claude/Codex/Gemini conversations | Medium |
| **Knowledge Graph (ob-graph)** — nodes, edges, traversal, path-finding via SQL | No graph layer | High |
| **Daily Digest / Life Engine** — scheduled proactive briefings via email/Slack/Telegram | No scheduled output or proactive push | Medium |
| **Extension pattern** — modular domain schemas (household, CRM, meal planning) with per-extension MCP servers | Collections but no domain-specific schema pattern | Low-Medium |
| **SHA-256 content fingerprinting** with metadata merge on duplicate | Discovery dedup via cosine > 0.9, but no document-level dedup | Low |
| **LLM metadata extraction on capture** — auto-extracts type, topics, people, actions | Relies on parser-extracted metadata; no LLM enrichment at ingest | Medium |
| **GitHub PR automation** — 15-point validation gate + Claude AI review | No CI/CD integration | Low |

### Specific workflow ports worth building

**1. Auto-Capture → KF Discovery Pipeline**
OB1's auto-capture fires at session close, evaluates items via "Panning for Gold", then captures high-value items as separate thoughts. In KF, this maps to:
- Claude Code hook (post-session) → calls `store_discovery()` for each high-value item
- Panning for Gold logic becomes a quality filter before storage
- Discoveries enter KF's existing lifecycle (unconfirmed → confirmed → promoted)

**2. LLM Metadata Enrichment**
OB1 calls OpenRouter's gpt-4o-mini in parallel with embedding to extract structured metadata. KF could add an optional enrichment step in the ingestion pipeline:
- After chunking, pass content to an LLM for type/topics/people/actions extraction
- Store as chunk metadata fields
- Improves search relevance and enables OB1-style metadata filtering

**3. Import Recipe Parsers**
OB1's email-history-import and chatgpt-conversation-import recipes define clear input formats. Adding these as KF ingestion parsers would expand KF's source coverage:
- `ingestion/gmail.py` — MBOX → conversation exchanges
- `ingestion/chatgpt.py` — ChatGPT JSON export → conversation exchanges
- `ingestion/slack.py` — Slack export → conversation exchanges

---

## Direction 2: KnowledgeForge Knowledge → OB1

### Capabilities KnowledgeForge has that OB1 lacks

| KF Capability | OB1 Gap | Effort |
|---|---|---|
| **Code parsing (tree-sitter)** — AST-aware function/class/method chunking across 8 languages | Thoughts are unstructured text; no code awareness | High |
| **Hybrid search (vector + BM25)** with fused scoring | Vector-only search + basic metadata filtering | Medium |
| **Trust tiers (T1-T4)** with score boosting (T1=1.0, T2=0.95, T3=0.85, T4=0.70) | No trust/confidence model | Low |
| **Discovery lifecycle** — unconfirmed → confirmed → promoted with human review gate | Captures are immediately persisted; no review stage | Medium |
| **Obsidian vault ingestion** — heading-aware splitting, wiki-link extraction, frontmatter parsing | No local file ingestion (captures from AI conversations only) | Medium |
| **Conversation indexing** — searchable Claude/Codex/Gemini session history | No conversation indexing | Medium |
| **Semantic memory types** — facts, runbooks, project_overviews with curation and supersession | Flat `type` field; no structured knowledge categories | Low-Medium |
| **File watching** — live re-ingestion on filesystem changes | Event-driven (capture on demand); no file watching | Low |

### Specific integrations worth building

**1. KF Code Search in OB1**
OB1 could expose a new MCP tool `search_code` that bridges to KF's codebase collection via REST API. This gives OB1 users code-aware search without rebuilding tree-sitter parsing on the edge.

**2. Trust Tiers in OB1**
Add `trust_level` to OB1's metadata JSONB schema. Adjust `match_thoughts` RPC to apply trust-based score boosting. Minimal schema change, significant quality improvement.

**3. Discovery Lifecycle in OB1**
Add `status` field (unconfirmed/confirmed/promoted) to thoughts metadata. New MCP tools: `confirm_thought`, `reject_thought`. Prevents low-quality captures from polluting search results.

---

## Recommended Integration Strategy

### Architecture: Bridge Pattern

Rather than merging the systems, connect them via a bidirectional bridge:

```
                    ┌──────────────┐
                    │  Bridge API  │
                    │  (sync both  │
                    │   directions)│
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ↓                         ↓
    ┌──────────────┐          ┌──────────────┐
    │ KnowledgeForge│          │     OB1      │
    │  (local RAG) │          │  (cloud MCP) │
    │  ChromaDB    │          │  Supabase    │
    │  code+docs   │          │  thoughts    │
    └──────────────┘          └──────────────┘
```

### Phased Implementation

**Phase 1 — Quick wins (Low effort):**
1. **OB1 → KF ingestion parser**: KF parser that reads OB1 thoughts via Supabase REST API, indexes them in a `thoughts` collection
2. **KF → OB1 export**: KF MCP tool `export_to_ob1()` that bulk-captures confirmed discoveries and semantic records as OB1 thoughts
3. **Content fingerprinting in KF**: Port OB1's SHA-256 dedup to KF's chunk model

**Phase 2 — Workflow ports (Medium effort):**
4. **Auto-Capture in KF**: Port OB1's auto-capture recipe as a Claude Code hook calling `store_discovery()` with panning-for-gold triage
5. **LLM metadata enrichment in KF**: Add optional LLM step during ingestion to extract type/topics/people/actions
6. **Trust tiers in OB1**: Add trust_level to metadata JSONB + scoring adjustments in match_thoughts
7. **Import parsers**: Gmail, ChatGPT, Slack ingestion parsers for KF

**Phase 3 — Deep integration (High effort):**
8. **Knowledge graph in KF**: Port OB1's ob-graph schema using SQLite or NetworkX backend
9. **Code awareness in OB1**: Bridge OB1 to KF's code collection for code-aware search
10. **Unified search**: MCP tool that searches both systems and fuses results with cross-system score normalization

---

## Key Architectural Differences

| Dimension | OB1 | KnowledgeForge |
|---|---|---|
| **Deployment** | Cloud (Supabase Edge Functions) | Local (Python services) |
| **Vector DB** | PostgreSQL + pgvector (HNSW) | ChromaDB (embedded) |
| **Embedding model** | text-embedding-3-small (1536-dim, via OpenRouter) | nomic-embed-text-v1.5 (768-dim, local) |
| **Search** | Vector-only + metadata filters | Hybrid vector + BM25 keyword |
| **Transport** | HTTP MCP (StreamableHTTP via Hono) | stdio MCP (FastMCP) + REST API |
| **Auth** | Access key (URL param or header) | Local-only (no auth by default) |
| **Language** | TypeScript/Deno | Python |
| **Data model** | Single `thoughts` table + extensions | 7 typed collections |
| **Knowledge lifecycle** | Capture → immediate persistence | Capture → review → confirm → promote |
| **Code awareness** | None | Tree-sitter AST parsing (8 languages) |

## Embedding Compatibility Note

OB1 uses 1536-dim embeddings (text-embedding-3-small) while KF uses 768-dim (nomic-embed-text-v1.5). Cross-system vector search requires either:
- Re-embedding content in the target system's model on sync
- A shared embedding model for the bridge layer
- Score normalization without direct vector comparison (metadata-based matching)

---

## Files Referenced

### OB1
- `/home/bsdev/github/OB1/server/index.ts` — Core MCP server (4 tools + auth)
- `/home/bsdev/github/OB1/docs/01-getting-started.md` — Setup guide
- `/home/bsdev/github/OB1/CLAUDE.md` — Agent instructions
- `/home/bsdev/github/OB1/.github/workflows/ob1-gate.yml` — 15-point PR gate
- `/home/bsdev/github/OB1/recipes/auto-capture/` — Auto-capture workflow
- `/home/bsdev/github/OB1/schemas/enhanced-thoughts/` — Enhanced schema
- `/home/bsdev/github/OB1/extensions/household-knowledge/schema.sql` — Extension pattern

### KnowledgeForge
- `/home/bsdev/knowledgeforge/src/knowledgeforge/core/engine.py` — Main orchestrator
- `/home/bsdev/knowledgeforge/src/knowledgeforge/core/models.py` — Data models
- `/home/bsdev/knowledgeforge/src/knowledgeforge/core/store.py` — ChromaDB wrapper
- `/home/bsdev/knowledgeforge/src/knowledgeforge/interfaces/rest_api.py` — FastAPI server
- `/home/bsdev/knowledgeforge/src/knowledgeforge/interfaces/mcp_server.py` — MCP tools
- `/home/bsdev/knowledgeforge/src/knowledgeforge/discovery/manager.py` — Discovery lifecycle
- `/home/bsdev/knowledgeforge/src/knowledgeforge/ingestion/obsidian.py` — Markdown parser
- `/home/bsdev/knowledgeforge/src/knowledgeforge/ingestion/code.py` — Code parser
- `/home/bsdev/knowledgeforge/config.yaml` — Configuration
