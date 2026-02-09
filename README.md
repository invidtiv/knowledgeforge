# KnowledgeForge

**Universal RAG Knowledge System — A shared brain for AI agents**

KnowledgeForge ingests your Obsidian vault, project codebases, and agent-generated discoveries into a unified searchable knowledge base. It exposes access via MCP (for Claude Code) and REST API (for any agent).

## Features

- **Obsidian Vault Indexing** — Full frontmatter, wiki-links, embeds, and heading-aware chunking
- **AST-Aware Code Parsing** — tree-sitter based parsing for Python, JavaScript, TypeScript, Rust, Go, C/C++, Bash
- **Discovery System** — AI agents store insights that get confirmed and promoted back to Obsidian
- **Multi-Agent Access** — MCP server for Claude Code + REST API for Gemini, Codex, and others
- **Incremental Updates** — SHA256 hash-based change detection, only re-index what changed
- **Live Sync** — Filesystem watcher with debounced re-ingestion
- **Local & Private** — Everything runs locally, no external services required

## Architecture

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Claude Code  │   │   Gemini    │   │    Codex    │
│  (MCP)       │   │  (REST)     │   │  (REST)     │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │                  │                   │
       ▼                  ▼                   ▼
┌──────────────────────────────────────────────────────┐
│              KnowledgeForge Engine                     │
│                                                        │
│  MCP Server ←→ Core Engine ←→ REST API (port 8742)    │
│                     │                                  │
│         ┌───────────┼───────────┐                      │
│         ▼           ▼           ▼                      │
│    [documents]  [codebase]  [discoveries]              │
│                                                        │
│              ChromaDB (embedded)                       │
└──────────────────────────────────────────────────────┘
```

## Quick Start

### Installation

```bash
git clone https://github.com/invidtiv/knowledgeforge.git
cd knowledgeforge
pip install -e .
```

### Configuration

```bash
knowledgeforge config init
# Edit ~/.config/knowledgeforge/config.yaml
```

Set your Obsidian vault path and code project paths:

```yaml
obsidian_vault_path: ~/Documents/MyVault
project_paths:
  - path: ~/projects/my-app
    name: my-app
```

### Index Your Knowledge

```bash
knowledgeforge index vault              # Index Obsidian vault
knowledgeforge index project ~/myapp    # Index a code project
knowledgeforge index all                # Index everything
```

### Start Services

```bash
knowledgeforge serve                    # REST API on port 8742
knowledgeforge watch                    # Live filesystem sync
```

### Search

```bash
knowledgeforge search "authentication flow"
knowledgeforge search "database pooling" --project my-app --collection code
```

## MCP Integration (Claude Code)

Add to your `.mcp.json`:

```json
{
    "mcpServers": {
        "knowledgeforge": {
            "command": "python",
            "args": ["-m", "knowledgeforge.interfaces.mcp_server"],
            "env": {
                "KNOWLEDGEFORGE_CONFIG": "~/.config/knowledgeforge/config.yaml"
            }
        }
    }
}
```

Available MCP tools:
- `search_knowledge` — Search docs, code, and discoveries
- `store_discovery` — Store debugging insights and learnings
- `get_project_context` — Get project overview
- `list_projects` — List indexed projects
- `ingest_path` — Index files or directories
- `get_discoveries` — Retrieve past discoveries

## REST API

Base URL: `http://127.0.0.1:8742/api/v1`

```bash
# Search
curl -X POST http://127.0.0.1:8742/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "how does auth work", "project": "my-app"}'

# Store discovery
curl -X POST http://127.0.0.1:8742/api/v1/discoveries \
  -H "Content-Type: application/json" \
  -d '{"content": "Pool exhaustion at 100 connections", "category": "bugfix", "severity": "critical"}'

# List projects
curl http://127.0.0.1:8742/api/v1/projects

# Health check
curl http://127.0.0.1:8742/api/v1/health
```

See [docs/api-reference.md](docs/api-reference.md) for full API documentation.

## Discovery Workflow

The discovery system creates a feedback loop between AI agents and your knowledge base:

```
Agent discovers insight → Stored in ChromaDB
     ↓
User reviews via CLI → Confirms or rejects
     ↓
Confirmed → Promoted to Obsidian vault as markdown note
     ↓
Obsidian note → Re-ingested into knowledge base
     ↓
Available to all agents via search
```

```bash
# Interactive review
knowledgeforge discoveries review

# Promote confirmed discoveries to Obsidian
knowledgeforge discoveries promote
```

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Vector DB | ChromaDB (embedded, persistent) |
| Embeddings | sentence-transformers (nomic-embed-text-v1.5) |
| Code Parsing | tree-sitter (8 languages) |
| Markdown | markdown-it-py + python-frontmatter |
| MCP Server | FastMCP |
| REST API | FastAPI + uvicorn |
| CLI | Typer + Rich |
| File Watching | watchdog |
| Config | pydantic-settings |

## Project Structure

```
knowledgeforge/
├── src/knowledgeforge/
│   ├── config.py               # Configuration (YAML + env vars)
│   ├── core/
│   │   ├── engine.py           # Main orchestrator
│   │   ├── embedder.py         # Embedding wrapper
│   │   ├── store.py            # ChromaDB wrapper
│   │   └── models.py           # Pydantic data models
│   ├── ingestion/
│   │   ├── obsidian.py         # Obsidian vault parser
│   │   ├── code.py             # tree-sitter code parser
│   │   ├── chunker.py          # Chunking utilities
│   │   └── watcher.py          # Filesystem watcher
│   ├── interfaces/
│   │   ├── mcp_server.py       # MCP server (Claude Code)
│   │   ├── rest_api.py         # REST API (FastAPI)
│   │   └── cli.py              # Admin CLI (Typer)
│   └── discovery/
│       ├── manager.py          # Discovery CRUD
│       └── promoter.py         # Obsidian write-back
├── scripts/
│   ├── setup.sh / setup.ps1    # Installation scripts
│   └── run.sh                  # Service launcher
├── pyproject.toml
└── config.yaml
```

## CLI Reference

```bash
knowledgeforge index vault [--full]          # Index Obsidian vault
knowledgeforge index project PATH [--name X] # Index code project
knowledgeforge index all [--full]            # Index everything

knowledgeforge search QUERY [-p PROJECT] [-c COLLECTION] [-n COUNT]

knowledgeforge discoveries list [-p PROJECT] [--unconfirmed] [--category X]
knowledgeforge discoveries review            # Interactive review
knowledgeforge discoveries confirm ID
knowledgeforge discoveries reject ID
knowledgeforge discoveries promote           # Write to Obsidian

knowledgeforge projects                      # List indexed projects
knowledgeforge stats                         # System statistics
knowledgeforge serve [--rest-only|--mcp-only]
knowledgeforge watch                         # Live file sync
knowledgeforge config show|init
```

## Requirements

- Python 3.10+
- No Docker required — everything runs natively

## Documentation

- [API Reference](docs/api-reference.md) - Complete REST API documentation
- [Configuration Guide](docs/configuration.md) - Configuration options and examples
- [Architecture](docs/architecture.md) - System design and data flow
- [Development Guide](docs/development.md) - Contributing and development setup

## Use Cases

### For Solo Developers
- Index your personal Obsidian knowledge vault
- Search across all your projects from any AI agent
- Store debugging insights that persist across sessions

### For Teams
- Shared knowledge base across multiple projects
- Standardize how AI agents access project context
- Discovery system creates living documentation

### For Research
- Index academic papers and notes from Obsidian
- Code analysis across multiple research codebases
- Track experimental insights and findings

## Performance

- **Incremental indexing**: Only changed files are re-processed
- **Efficient chunking**: Semantic boundaries for better retrieval
- **Local embeddings**: No API calls, instant results
- **Persistent storage**: ChromaDB maintains state across restarts

## Roadmap

- [ ] Support for additional document formats (PDF, DOCX)
- [ ] Web UI for discovery management
- [ ] Multi-user support with access controls
- [ ] Slack/Discord bot integration
- [ ] GraphRAG for relationship extraction
- [ ] Custom embedding model support

## Contributing

Contributions welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT

## Support

- Issues: https://github.com/tiaz-fr/knowledgeforge/issues
- Discussions: https://github.com/tiaz-fr/knowledgeforge/discussions

---

Built with love for the AI agent ecosystem. Built by developers, for developers.
