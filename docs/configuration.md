# KnowledgeForge Configuration Guide

KnowledgeForge uses a layered configuration system with YAML files and environment variables.

> Current implementation note:
> The runtime schema is defined by `src/knowledgeforge/config.py`. Some nested examples below are illustrative, but the active keys in this repo are flat keys (for example `max_chunk_size`, `chunk_overlap`, `rest_host`, `rest_port`).

## Table of Contents

- [Configuration File Location](#configuration-file-location)
- [Current Runtime Keys](#current-runtime-keys)
- [Configuration Schema](#configuration-schema)
- [Environment Variables](#environment-variables)
- [Example Configurations](#example-configurations)
- [Migration & Defaults](#migration--defaults)

---

## Configuration File Location

KnowledgeForge looks for configuration in the following order:

1. `KNOWLEDGEFORGE_CONFIG` environment variable (absolute path)
2. `./config.yaml` (current directory)
3. `~/.config/knowledgeforge/config.yaml` (user config)
4. `/etc/knowledgeforge/config.yaml` (system config, Linux only)

**Initialize default config:**
```bash
knowledgeforge config init
```

This creates `~/.config/knowledgeforge/config.yaml` with sensible defaults.

---

## Current Runtime Keys

The currently implemented config keys (in `src/knowledgeforge/config.py`) include:

- `data_dir`
- `obsidian_vault_path`
- `project_paths`
- `embedding_provider`
- `embedding_model`
- `embedding_device`
- `embedding_batch_size`
- `max_chunk_size`
- `chunk_overlap`
- `rest_host`
- `rest_port`
- `watch_enabled`
- `watch_debounce_seconds`
- `obsidian_discoveries_folder`
- `auto_promote_confirmed`
- `conversation_sources`
- `conversation_archive_dir`
- `conversation_enrichment_dir`
- `conversation_max_tool_result_chars`
- `conversation_sync_on_start`
- `obsidian_extensions`
- `code_extensions`
- `ignore_patterns`

---

## Configuration Schema

### Complete Example

```yaml
# KnowledgeForge Configuration
# Generated: 2025-01-15

# Obsidian vault path (required)
obsidian_vault_path: ~/Documents/MyVault

# Project paths to index
project_paths:
  - path: ~/projects/web-app
    name: web-app
    enabled: true

  - path: ~/projects/data-pipeline
    name: data-pipeline
    enabled: true

  - path: ~/projects/mobile-app
    name: mobile-app
    enabled: false  # Disabled, won't be indexed

# ChromaDB settings
chromadb:
  persist_directory: ~/.local/share/knowledgeforge/chroma
  collection_metadata:
    hnsw:space: cosine

# Embedding model configuration
embedding_provider: openrouter  # auto, openai, openrouter, local
embedding_model: openai/text-embedding-3-small
embedding_device: cpu    # Used only by local sentence-transformers models
embedding_batch_size: 16
memory_registry_path: ~/.local/share/knowledgeforge/memory_registry.sqlite3

# Chunking strategy
chunking:
  # Document chunking (Obsidian notes, README, etc.)
  documents:
    chunk_size: 512
    chunk_overlap: 50
    respect_headings: true
    preserve_frontmatter: true

  # Code chunking (AST-aware)
  code:
    chunk_size: 256
    chunk_overlap: 30
    include_context: true  # Include class/function signatures
    max_depth: 3  # Maximum AST depth

# File watching
watcher:
  enabled: true
  debounce_seconds: 2.0
  watch_vault: true
  watch_projects: true
  ignored_patterns:
    - "*.tmp"
    - "*.swp"
    - ".git/**"
    - "node_modules/**"
    - "__pycache__/**"
    - "*.pyc"

# REST API server
rest_api:
  host: 127.0.0.1
  port: 8742
  reload: false  # Enable auto-reload for development
  workers: 1
  log_level: info

# MCP defaults (used when launching MCP from CLI)
mcp_transport: stdio  # stdio | sse | streamable-http

# Discovery system
discoveries:
  auto_promote: false  # Automatically promote confirmed discoveries
  promotion_path: Discoveries  # Path within vault for promoted notes
  require_confirmation: true
  retention_days: 90  # Keep unconfirmed discoveries for 90 days

# Indexing behavior
indexing:
  incremental: true  # Use SHA256 hashing for change detection
  exclude_patterns:
    - "*.min.js"
    - "*.map"
    - "dist/**"
    - "build/**"
    - ".venv/**"
    - "venv/**"
  max_file_size_mb: 10  # Skip files larger than 10MB

  # Language-specific settings
  languages:
    python:
      enabled: true
      extensions: [".py", ".pyw"]
    javascript:
      enabled: true
      extensions: [".js", ".jsx", ".mjs"]
    typescript:
      enabled: true
      extensions: [".ts", ".tsx"]
    rust:
      enabled: true
      extensions: [".rs"]
    go:
      enabled: true
      extensions: [".go"]
    cpp:
      enabled: true
      extensions: [".cpp", ".cc", ".cxx", ".hpp", ".h"]
    c:
      enabled: true
      extensions: [".c", ".h"]
    bash:
      enabled: true
      extensions: [".sh", ".bash"]

# Logging
logging:
  level: INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  file: ~/.local/share/knowledgeforge/logs/knowledgeforge.log
  max_bytes: 10485760  # 10MB
  backup_count: 5
  console: true

# Performance tuning
performance:
  max_concurrent_ingestions: 4
  embedding_cache_size: 1000
  search_timeout_seconds: 30
```

---

## Configuration Fields Reference

### Core Settings

#### `obsidian_vault_path`
- **Type:** `string` (path)
- **Required:** Yes
- **Description:** Path to your Obsidian vault
- **Example:** `~/Documents/MyVault`
- **Supports:** Tilde expansion (`~`), environment variables (`$HOME`)

#### `project_paths`
- **Type:** `array` of objects
- **Required:** No
- **Description:** List of code projects to index
- **Fields:**
  - `path` (string, required) - Project directory path
  - `name` (string, required) - Project identifier
  - `enabled` (boolean, default: true) - Enable/disable indexing

**Example:**
```yaml
project_paths:
  - path: ~/dev/myapp
    name: myapp
  - path: /mnt/projects/backend
    name: backend-api
    enabled: false
```

---

### ChromaDB Settings

#### `chromadb`
- **Type:** `object`
- **Description:** ChromaDB vector database configuration

**Fields:**

##### `persist_directory`
- **Type:** `string` (path)
- **Default:** `~/.local/share/knowledgeforge/chroma`
- **Description:** Where ChromaDB stores vector data

##### `collection_metadata`
- **Type:** `object`
- **Default:** `{hnsw:space: cosine}`
- **Description:** ChromaDB collection metadata
- **Options for `hnsw:space`:**
  - `cosine` - Cosine similarity (default, normalized)
  - `l2` - Euclidean distance
  - `ip` - Inner product

---

### Embedding Settings

##### `embedding_provider`
- **Type:** `string`
- **Default:** `auto`
- **Options:** `auto`, `openai`, `openrouter`, `local`
- **Description:** Embedding backend. Use `openrouter` to match OB1. `auto` uses `OPENAI_API_KEY` when present, then `OPENROUTER_API_KEY`, then falls back to the local CPU model.

##### `embedding_model`
- **Type:** `string`
- **Default:** `nomic-ai/nomic-embed-text-v1.5`
- **Recommended OpenRouter model:** `openai/text-embedding-3-small`
- **Description:** Embedding model identifier. With OpenRouter, use `openai/text-embedding-3-small`.

##### `embedding_device`
- **Type:** `string`
- **Default:** `cpu`
- **Options:** `cpu`, `cuda`, `mps`
- **Description:** Device for local sentence-transformers inference only.

##### `embedding_batch_size`
- **Type:** `integer`
- **Default:** `16`
- **Description:** Number of texts sent per embedding request or local encode batch.

Switching from the local Nomic model to OpenRouter/OpenAI embeddings changes vector dimensions from the existing local collection. Clear/reindex Chroma collections before mixing old and new embeddings.

Secrets are loaded from `~/.config/knowledgeforge/secrets.env` before config parsing. Put `OPENROUTER_API_KEY=...` there and keep the file mode restricted.

### Structured Memory Settings

##### `memory_cards_collection`
- **Type:** `string`
- **Default:** `memory_cards`
- **Description:** ChromaDB collection used for atomic extracted memories from past conversations.

##### `memory_registry_path`
- **Type:** `string` (path)
- **Default:** `{data_dir}/memory_registry.sqlite3`
- **Description:** SQLite registry for structured filtering by project, type, status, confidence, and current-truth state.

---

### Chunking Settings

#### `max_chunk_size`
- **Type:** `integer`
- **Default:** `1000`
- **Description:** Target chunk size in approximate tokens for text splitting.

#### `chunk_overlap`
- **Type:** `integer`
- **Default:** `100`
- **Description:** Approximate token overlap between adjacent chunks.

#### How large files are chunked in practice

- Obsidian markdown:
  - Split by heading sections first.
  - Sections larger than `max_chunk_size` are split by paragraphs/sentences with `chunk_overlap`.
- Conversations:
  - One exchange chunk by default.
  - Oversized exchange text is split using `max_chunk_size` and `chunk_overlap`.
- Code (tree-sitter languages):
  - Chunked structurally (module summary + class/function/method chunks).
  - Large symbol bodies are currently kept as a single chunk (not token-split).
- Heuristic files (`.sql`, `.yaml`, `.yml`, `.json`, `.toml`):
  - Split by statements or sections; fallback uses token chunking.

---

### File Watcher Settings

#### `watcher`
- **Type:** `object`
- **Description:** Live filesystem monitoring

**Fields:**

##### `enabled`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Enable filesystem watching

##### `debounce_seconds`
- **Type:** `float`
- **Default:** `2.0`
- **Description:** Wait time before triggering re-index (prevents duplicate work)

##### `watch_vault`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Watch Obsidian vault for changes

##### `watch_projects`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Watch project directories for changes

##### `ignored_patterns`
- **Type:** `array` of strings
- **Default:** `["*.tmp", "*.swp", ".git/**", ...]`
- **Description:** Glob patterns to exclude from watching

---

### REST API Settings

#### `rest_api`
- **Type:** `object`
- **Description:** REST API server configuration

**Fields:**

##### `host`
- **Type:** `string`
- **Default:** `127.0.0.1`
- **Description:** Bind address (use `0.0.0.0` for remote access)

##### `port`
- **Type:** `integer`
- **Default:** `8742`
- **Description:** TCP port for REST API

##### `reload`
- **Type:** `boolean`
- **Default:** `false`
- **Description:** Auto-reload on code changes (development only)

##### `workers`
- **Type:** `integer`
- **Default:** `1`
- **Description:** Number of uvicorn workers

##### `log_level`
- **Type:** `string`
- **Default:** `info`
- **Options:** `debug`, `info`, `warning`, `error`, `critical`

---

### MCP Server Settings

#### `mcp_transport`
- **Type:** `string`
- **Default:** `stdio`
- **Options:** `stdio`, `sse`, `streamable-http`
- **Description:** Default MCP transport mode used by KnowledgeForge MCP entrypoint.

#### Runtime Environment Variables

These control MCP runtime behavior when starting `knowledgeforge.interfaces.mcp_server`:

##### `KNOWLEDGEFORGE_MCP_TRANSPORT`
- **Type:** `string`
- **Default:** `stdio`
- **Options:** `stdio`, `sse`, `streamable-http`
- **Description:** Overrides transport at runtime.

##### `KNOWLEDGEFORGE_MCP_HOST`
- **Type:** `string`
- **Default:** `127.0.0.1` (for network transports)
- **Description:** Host bind address for `sse` and `streamable-http`.

##### `KNOWLEDGEFORGE_MCP_PORT`
- **Type:** `integer`
- **Default:** `8743` (for network transports)
- **Description:** Port bind for `sse` and `streamable-http`.

##### `KNOWLEDGEFORGE_MCP_MOUNT_PATH`
- **Type:** `string`
- **Default:** unset
- **Description:** Optional FastMCP mount path for network transport.

##### `FASTMCP_HOST` / `FASTMCP_PORT`
- **Type:** `string` / `integer`
- **Description:** FastMCP-native fallback variables; used if `KNOWLEDGEFORGE_MCP_*` is not set.

#### Recommended Multi-Client Setup

For Codex/Claude/Gemini/Windsurf on the same host, run one shared MCP server and connect clients with `mcp-remote`:

- Shared server endpoint: `http://127.0.0.1:8743/mcp`
- Client command: `npx -y mcp-remote http://127.0.0.1:8743/mcp`

This avoids one Python MCP process per client/session and reduces memory pressure.

---

### Discovery System Settings

#### `discoveries`
- **Type:** `object`
- **Description:** AI agent discovery management

**Fields:**

##### `auto_promote`
- **Type:** `boolean`
- **Default:** `false`
- **Description:** Automatically promote confirmed discoveries to Obsidian

##### `promotion_path`
- **Type:** `string`
- **Default:** `Discoveries`
- **Description:** Vault subdirectory for promoted discovery notes

##### `require_confirmation`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Require human confirmation before promotion

##### `retention_days`
- **Type:** `integer`
- **Default:** `90`
- **Description:** Delete unconfirmed discoveries after N days (0 = keep forever)

---

### Indexing Settings

#### `indexing`
- **Type:** `object`
- **Description:** File indexing behavior

**Fields:**

##### `incremental`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Use SHA256 hashing for change detection

##### `exclude_patterns`
- **Type:** `array` of strings
- **Default:** `["*.min.js", "*.map", "dist/**", ...]`
- **Description:** Glob patterns to exclude from indexing

##### `max_file_size_mb`
- **Type:** `integer`
- **Default:** `10`
- **Description:** Skip files larger than this (prevents memory issues)

##### `languages`
- **Type:** `object`
- **Description:** Per-language configuration
- **Structure:**
  ```yaml
  languages:
    python:
      enabled: true
      extensions: [".py", ".pyw"]
  ```

**Supported languages:**
- `python`, `javascript`, `typescript`, `rust`, `go`, `cpp`, `c`, `bash`

---

### Logging Settings

#### `logging`
- **Type:** `object`
- **Description:** Application logging configuration

**Fields:**

##### `level`
- **Type:** `string`
- **Default:** `INFO`
- **Options:** `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

##### `format`
- **Type:** `string`
- **Default:** `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"`
- **Description:** Python logging format string

##### `file`
- **Type:** `string` (path)
- **Default:** `~/.local/share/knowledgeforge/logs/knowledgeforge.log`
- **Description:** Log file path

##### `max_bytes`
- **Type:** `integer`
- **Default:** `10485760` (10MB)
- **Description:** Max log file size before rotation

##### `backup_count`
- **Type:** `integer`
- **Default:** `5`
- **Description:** Number of rotated log files to keep

##### `console`
- **Type:** `boolean`
- **Default:** `true`
- **Description:** Also log to console/stdout

---

### Performance Tuning

#### `performance`
- **Type:** `object`
- **Description:** Performance optimization settings

**Fields:**

##### `max_concurrent_ingestions`
- **Type:** `integer`
- **Default:** `4`
- **Description:** Max parallel file processing threads

##### `embedding_cache_size`
- **Type:** `integer`
- **Default:** `1000`
- **Description:** LRU cache size for embeddings

##### `search_timeout_seconds`
- **Type:** `integer`
- **Default:** `30`
- **Description:** Max time for search queries

---

## Environment Variables

Environment variables override YAML settings. All are prefixed with `KNOWLEDGEFORGE_`.

| Variable | Type | Example |
|----------|------|---------|
| `KNOWLEDGEFORGE_CONFIG` | path | `~/my-config.yaml` |
| `KNOWLEDGEFORGE_OBSIDIAN_VAULT_PATH` | path | `~/Documents/Vault` |
| `KNOWLEDGEFORGE_CHROMADB_PERSIST_DIRECTORY` | path | `/data/chroma` |
| `KNOWLEDGEFORGE_EMBEDDINGS_DEVICE` | string | `cuda` |
| `KNOWLEDGEFORGE_REST_API_PORT` | integer | `9000` |
| `KNOWLEDGEFORGE_LOG_LEVEL` | string | `DEBUG` |

**Example:**
```bash
export KNOWLEDGEFORGE_OBSIDIAN_VAULT_PATH=~/MyVault
export KNOWLEDGEFORGE_EMBEDDINGS_DEVICE=cuda
knowledgeforge serve
```

---

## Example Configurations

### Minimal Configuration

```yaml
obsidian_vault_path: ~/Documents/MyVault
```

This uses all defaults and only indexes the Obsidian vault.

---

### Development Configuration

```yaml
obsidian_vault_path: ~/Documents/DevVault

project_paths:
  - path: ~/dev/myapp
    name: myapp

rest_api:
  reload: true
  log_level: debug

logging:
  level: DEBUG
  console: true

watcher:
  debounce_seconds: 0.5  # Faster response
```

---

### Production Configuration

```yaml
obsidian_vault_path: /data/vault

project_paths:
  - path: /app/backend
    name: backend
  - path: /app/frontend
    name: frontend

chromadb:
  persist_directory: /data/chroma

embeddings:
  device: cuda
  batch_size: 128

rest_api:
  host: 0.0.0.0
  port: 8742
  workers: 4
  log_level: warning

logging:
  level: INFO
  file: /var/log/knowledgeforge/app.log
  console: false

performance:
  max_concurrent_ingestions: 8
  embedding_cache_size: 5000
```

---

### GPU-Optimized Configuration

```yaml
obsidian_vault_path: ~/Documents/Vault

embeddings:
  model_name: BAAI/bge-base-en-v1.5
  device: cuda
  batch_size: 256  # Larger batch for GPU

chunking:
  documents:
    chunk_size: 768  # Larger chunks for better GPU utilization
  code:
    chunk_size: 512

performance:
  max_concurrent_ingestions: 8
  embedding_cache_size: 10000
```

---

### Multi-Vault Configuration (Future)

```yaml
vaults:
  - path: ~/Documents/PersonalVault
    name: personal
  - path: ~/Documents/WorkVault
    name: work

project_paths:
  - path: ~/projects/app1
    name: app1
    vault: work  # Associate with work vault
  - path: ~/projects/hobby
    name: hobby
    vault: personal
```

Currently, only single vault is supported. Multi-vault is planned for v0.2.0.

---

## Migration & Defaults

### Migrating from v0.1 to v0.2 (Future)

When v0.2 releases with breaking changes, run:

```bash
knowledgeforge config migrate
```

This will update your config file to the new schema.

---

### Viewing Current Configuration

```bash
knowledgeforge config show
```

Shows resolved configuration (YAML + env vars merged).

---

### Validating Configuration

```bash
knowledgeforge config validate
```

Checks for errors and warnings in your configuration.

---

## Troubleshooting

### Issue: "Obsidian vault path not found"

**Solution:** Check that path exists and use absolute paths:
```yaml
obsidian_vault_path: /home/user/Documents/Vault  # Absolute
# NOT: ~/Documents/Vault (tilde not expanded in YAML)
```

Use `knowledgeforge config show` to see resolved path.

---

### Issue: "ChromaDB permission denied"

**Solution:** Ensure persist directory is writable:
```bash
mkdir -p ~/.local/share/knowledgeforge/chroma
chmod 755 ~/.local/share/knowledgeforge/chroma
```

---

### Issue: "Out of memory during embedding"

**Solution:** Reduce batch size:
```yaml
embeddings:
  batch_size: 8  # Lower from default 32
```

---

### Issue: "Slow indexing on large projects"

**Solution:** Increase concurrency and batch size:
```yaml
performance:
  max_concurrent_ingestions: 8
embeddings:
  batch_size: 64
```

---

For more help, see:
- [Architecture Documentation](architecture.md)
- [API Reference](api-reference.md)
- [GitHub Issues](https://github.com/tiaz-fr/knowledgeforge/issues)
