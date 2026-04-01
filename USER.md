# KnowledgeForge User Guide

## What KnowledgeForge Is

KnowledgeForge is a local-first knowledge system for developers and AI-agent operators. It turns your Obsidian vault, code projects, agent discoveries, and archived AI conversations into one searchable knowledge base that can be used from the CLI, over a local REST API, or through MCP.

In practice, it acts like a shared memory layer for tools such as Claude Code, Codex, and Gemini.

## Who Uses It

KnowledgeForge is built for people who:

- keep working notes or docs in Obsidian
- work across one or more software repositories
- want AI agents to search the same project knowledge
- want to preserve debugging insights and decisions between sessions

It is best suited to a local workstation, homelab box, or private server. It is not a browser app and it does not currently ship with user authentication for remote multi-tenant use.

## What You Can Do With It

- index an Obsidian vault into a `documents` collection
- index one or more codebases into a `codebase` collection
- store reusable discoveries in a `discoveries` collection
- index archived Claude Code, Codex, and Gemini conversations into a `conversations` collection
- search across those collections with hybrid vector + keyword search
- expose the knowledge base to local tools over REST or MCP
- keep content fresh with a filesystem watcher

## Main Interfaces

You use KnowledgeForge through three interfaces:

1. CLI for setup, indexing, search, stats, and discovery review
2. REST API for local automation and non-MCP clients
3. MCP server for agent tools such as Claude Code

## First-Time Setup

### 1. Install

From the repository root:

```bash
pip install -e .
pip install --index-url https://download.pytorch.org/whl/cpu torch
```

If you prefer the bundled setup script:

```bash
./scripts/setup.sh
```

### 2. Create Your Config

```bash
knowledgeforge config init
```

This creates:

```text
~/.config/knowledgeforge/config.yaml
```

### 3. Point It at Your Knowledge Sources

Edit the config and set at least your Obsidian vault path and any code projects you want indexed:

```yaml
obsidian_vault_path: ~/Documents/MyVault
project_paths:
  - path: ~/projects/my-app
    name: my-app
  - path: ~/projects/backend-api
    name: backend-api
```

Useful defaults to know:

- indexed data lives under `~/.local/share/knowledgeforge`
- REST API defaults to `127.0.0.1:8742`
- the default collections are `documents`, `codebase`, `discoveries`, and `conversations`

## Basic Usage

### Index Your Content

Index your vault:

```bash
knowledgeforge index vault
```

Index one project:

```bash
knowledgeforge index project ~/projects/my-app --name my-app
```

Index everything configured:

```bash
knowledgeforge index all
```

Use `--full` when you want a full rebuild instead of incremental re-indexing.

### Search

Search everything:

```bash
knowledgeforge search "authentication flow"
```

Search a specific project:

```bash
knowledgeforge search "rate limiting" --project my-app
```

Search a specific collection:

```bash
knowledgeforge search "pool exhaustion" --collection discoveries
knowledgeforge search "router setup" --collection codebase
```

### Inspect the Indexed State

```bash
knowledgeforge projects
knowledgeforge stats
knowledgeforge config show
```

## Key Features

### Obsidian-Aware Markdown Indexing

Markdown is split into retrieval-friendly chunks using headings and token limits. Frontmatter, tags, and file metadata are kept so searches can be scoped later.

### Code-Aware Project Indexing

KnowledgeForge indexes code and project markdown from the same project tree. It supports Python, JavaScript, TypeScript, Rust, Go, C, C++, Bash, plus common config and SQL files.

### Hybrid Search

Search combines embeddings and keyword search, which helps with both semantic queries and exact-term lookups.

### Discovery Workflow

Agents can store useful findings, and you can review or reject them before promoting confirmed discoveries back into your Obsidian vault.

### Shared Memory for Agents

Through MCP and REST, multiple agents can search the same indexed knowledge instead of each agent rebuilding context from scratch.

### Live Sync

The watcher monitors configured paths and re-indexes changed files with debounce logic, so you do not have to re-run a full ingest for every small edit.

## Common Workflows

### Workflow 1: Build a Searchable Personal Knowledge Base

1. Configure your vault and project paths.
2. Run `knowledgeforge index all`.
3. Use `knowledgeforge search` to find notes, code, and discoveries together.
4. Re-run indexing or start the watcher when your source material changes.

### Workflow 2: Add a New Project

1. Add the project to `project_paths` in `~/.config/knowledgeforge/config.yaml`.
2. Run:

```bash
knowledgeforge index project ~/projects/new-project --name new-project
```

3. Verify it appears in:

```bash
knowledgeforge projects
```

4. Search it with:

```bash
knowledgeforge search "startup sequence" --project new-project
```

### Workflow 3: Review and Promote Discoveries

List discoveries:

```bash
knowledgeforge discoveries list
knowledgeforge discoveries list --unconfirmed
```

Review pending discoveries interactively:

```bash
knowledgeforge discoveries review
```

Promote confirmed discoveries back to your vault:

```bash
knowledgeforge discoveries promote
```

This is the workflow that turns one-off debugging findings into reusable knowledge.

### Workflow 4: Run KnowledgeForge as a Local Service

Start the REST API:

```bash
knowledgeforge serve
```

Start the filesystem watcher:

```bash
knowledgeforge watch
```

For a persistent local deployment, the repository also includes systemd user service files and an operations runbook in [docs/operations.md](docs/operations.md).

### Workflow 5: Connect AI Agents Over MCP

For Claude Code, you can launch KnowledgeForge directly over stdio:

```json
{
  "mcpServers": {
    "knowledgeforge": {
      "command": "/path/to/python",
      "args": ["-m", "knowledgeforge.interfaces.mcp_server"],
      "env": {
        "KNOWLEDGEFORGE_CONFIG": "/home/you/.config/knowledgeforge/config.yaml"
      }
    }
  }
}
```

For a shared local endpoint, the recommended pattern is to run one MCP service and connect clients through `mcp-remote`.

Useful MCP tools include:

- `search_knowledge`
- `get_knowledge_context`
- `store_discovery`
- `get_project_context`
- `list_projects`
- `ingest_path`
- `get_discoveries`
- `search_conversations`
- `read_conversation`

### Workflow 6: Use the REST API from Scripts or Other Agents

Start the API:

```bash
knowledgeforge serve
```

Then call it on `http://127.0.0.1:8742/api/v1`.

Examples:

```bash
curl -X POST http://127.0.0.1:8742/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"authentication flow","project":"my-app"}'
```

```bash
curl http://127.0.0.1:8742/api/v1/projects
```

```bash
curl http://127.0.0.1:8742/api/v1/health
```

## Conversation Search

KnowledgeForge can also index archived AI conversations from configured source directories and make them searchable as a `conversations` collection.

This is useful if you want past Claude Code, Codex, or Gemini sessions to become part of your searchable knowledge base.

Current behavior to know:

- conversation sources are configured in `config.yaml`
- conversation search is available through MCP and REST
- manual syncing is exposed through the REST API at `POST /api/v1/conversations/sync`
- there is no dedicated CLI subcommand for conversation sync in the current command surface

## Recommended Day-to-Day Operating Model

For most users, the simplest routine is:

1. keep your config up to date
2. run `knowledgeforge index all` after major changes, or keep `knowledgeforge watch` running
3. use `knowledgeforge search` for manual lookups
4. let agents use MCP or REST for automated retrieval
5. review discoveries regularly so useful findings become part of the permanent knowledge base

## Limitations and Expectations

- KnowledgeForge is local-first. If you expose it beyond localhost, you are responsible for network security.
- The REST API currently runs without built-in authentication.
- The default CLI is strongest for indexing, search, and discovery review; some advanced conversation operations are REST or MCP oriented.
- There is no graphical UI in this repository.

## Where to Go Next

- [README.md](README.md) for the quick start and project overview
- [docs/configuration.md](docs/configuration.md) for config details
- [docs/api-reference.md](docs/api-reference.md) for REST usage
- [docs/operations.md](docs/operations.md) for a shared long-running deployment
- [docs/architecture.md](docs/architecture.md) for internal design details
