"""
Configuration module for KnowledgeForge.

Handles configuration loading from YAML files and environment variables.
Uses pydantic-settings for validation and type safety.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, model_validator
from pathlib import Path
from typing import Optional, Any
import os
import yaml


def _load_env_file(env_path: str) -> None:
    """Load KEY=VALUE pairs from a local env file without overwriting env vars."""
    path = Path(os.path.expanduser(env_path))
    if not path.exists() or not path.is_file():
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                if line.startswith("export "):
                    line = line[len("export "):].strip()

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        print(f"Warning: Could not read env file {path}: {exc}")


class ProjectPath(BaseSettings):
    """Configuration for a single project path."""
    path: str
    name: str


class ProjectIngestOverride(BaseModel):
    """Optional ingest overrides for a configured project."""

    ignore_patterns: list[str] = Field(default_factory=list)
    skip_markdown: bool = False
    skip_code: bool = False


class KnowledgeForgeConfig(BaseSettings):
    """
    Main configuration class for KnowledgeForge.

    Supports loading from:
    1. YAML configuration file
    2. Environment variables (KNOWLEDGEFORGE_ prefix)

    Environment variables override YAML values.
    """

    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGEFORGE_",
        env_nested_delimiter="__",
        case_sensitive=False
    )

    # Paths
    data_dir: str = "~/.local/share/knowledgeforge"
    obsidian_vault_path: str = ""
    project_paths: list[dict] = Field(default_factory=list)  # list of {"path": str, "name": str}

    # Embedding
    embedding_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 16
    embedding_provider: str = "auto"
    openai_api_key: str = ""
    openrouter_api_key: str = ""

    # ChromaDB
    chroma_persist_dir: str = ""  # defaults to {data_dir}/chromadb
    keyword_index_path: str = ""  # defaults to {data_dir}/keyword_index.sqlite3
    memory_registry_path: str = ""  # defaults to {data_dir}/memory_registry.sqlite3

    # Collections
    docs_collection: str = "documents"
    code_collection: str = "codebase"
    discoveries_collection: str = "discoveries"
    facts_collection: str = "facts"
    runbooks_collection: str = "runbooks"
    project_overviews_collection: str = "project_overviews"
    memory_cards_collection: str = "memory_cards"

    # Chunking
    max_chunk_size: int = 400
    chunk_overlap: int = 80

    # Queue runner
    queue_max_files_per_run: int = 8
    queue_max_chunks_per_run: int = 400
    queue_time_budget_seconds: int = 240

    # Server
    rest_host: str = "127.0.0.1"
    rest_port: int = 8742
    mcp_transport: str = "stdio"

    # Watcher
    watch_enabled: bool = True
    watch_debounce_seconds: float = 2.0

    # Discovery
    obsidian_discoveries_folder: str = "KnowledgeForge Discoveries"
    auto_promote_confirmed: bool = True

    # Conversations
    conversations_collection: str = "conversations"
    conversation_sources: list[str] = Field(default_factory=lambda: [
        "~/.claude/projects",
        "~/.config/superpowers/conversation-archive/_codex",
        "~/.config/superpowers/conversation-archive/_gemini",
    ])
    conversation_archive_dir: str = "~/.config/superpowers/conversation-archive"
    conversation_enrichment_dir: str = ""  # e.g. data/enriched_conversations
    conversation_max_tool_result_chars: int = 500
    conversation_sync_on_start: bool = True

    # OB1 bridge
    ob1_supabase_url: str = ""
    ob1_supabase_key: str = ""
    ob1_access_key: str = ""

    # File patterns
    obsidian_extensions: list[str] = Field(default_factory=lambda: [".md"])
    code_extensions: list[str] = Field(default_factory=lambda: [
        ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go",
        ".c", ".cpp", ".h", ".hpp", ".sh", ".bash",
        ".yaml", ".yml", ".toml", ".json", ".sql"
    ])
    ignore_patterns: list[str] = Field(default_factory=lambda: [
        "node_modules", ".git", "__pycache__", ".obsidian",
        "venv", ".venv", "dist", "build", ".next", ".trash"
    ])
    project_ingest_overrides: dict[str, ProjectIngestOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def expand_paths(self) -> "KnowledgeForgeConfig":
        """
        Expand ~ to home directory in all path fields.
        Also sets chroma_persist_dir default if not explicitly set.
        """
        # Expand home directory in path fields
        if self.data_dir:
            self.data_dir = os.path.expanduser(self.data_dir)

        if self.obsidian_vault_path:
            self.obsidian_vault_path = os.path.expanduser(self.obsidian_vault_path)

        # Expand paths in project_paths list
        for project in self.project_paths:
            if "path" in project and project["path"]:
                project["path"] = os.path.expanduser(project["path"])

        # Expand conversation paths
        self.conversation_sources = [os.path.expanduser(p) for p in self.conversation_sources]
        if self.conversation_archive_dir:
            self.conversation_archive_dir = os.path.expanduser(self.conversation_archive_dir)
        if self.conversation_enrichment_dir:
            self.conversation_enrichment_dir = os.path.expanduser(self.conversation_enrichment_dir)

        # Set chroma_persist_dir default if not explicitly set
        if not self.chroma_persist_dir:
            self.chroma_persist_dir = os.path.join(self.data_dir, "chromadb")
        else:
            self.chroma_persist_dir = os.path.expanduser(self.chroma_persist_dir)

        if not self.keyword_index_path:
            self.keyword_index_path = os.path.join(
                self.data_dir, "keyword_index.sqlite3"
            )
        else:
            self.keyword_index_path = os.path.expanduser(self.keyword_index_path)

        if not self.memory_registry_path:
            self.memory_registry_path = os.path.join(
                self.data_dir, "memory_registry.sqlite3"
            )
        else:
            self.memory_registry_path = os.path.expanduser(self.memory_registry_path)

        return self

    @model_validator(mode="after")
    def ensure_directories_exist(self) -> "KnowledgeForgeConfig":
        """
        Ensure critical directories exist.
        Creates data_dir and chroma_persist_dir if they don't exist.
        """
        # Create data directory
        if self.data_dir:
            data_path = Path(self.data_dir)
            data_path.mkdir(parents=True, exist_ok=True)

        # Create chroma persist directory
        if self.chroma_persist_dir:
            chroma_path = Path(self.chroma_persist_dir)
            chroma_path.mkdir(parents=True, exist_ok=True)

        if self.keyword_index_path:
            keyword_parent = Path(self.keyword_index_path).parent
            keyword_parent.mkdir(parents=True, exist_ok=True)

        if self.memory_registry_path:
            memory_parent = Path(self.memory_registry_path).parent
            memory_parent.mkdir(parents=True, exist_ok=True)

        return self

    def get_project_ingest_override(self, project_name: str) -> ProjectIngestOverride:
        """Return ingest overrides for a project, or defaults if none are configured."""

        override = self.project_ingest_overrides.get(project_name)
        if override is None:
            return ProjectIngestOverride()
        return override

    @classmethod
    def load_config(cls, config_path: Optional[str] = None) -> "KnowledgeForgeConfig":
        """
        Load configuration from YAML file and environment variables.

        Priority order:
        1. Explicit config_path parameter
        2. KNOWLEDGEFORGE_CONFIG environment variable
        3. ./config.yaml (current directory)
        4. ~/.config/knowledgeforge/config.yaml (user config directory)
        5. Default values only

        Environment variables always override YAML values.

        Args:
            config_path: Optional explicit path to config file

        Returns:
            KnowledgeForgeConfig instance with loaded configuration
        """
        yaml_config = {}

        # Determine config file path
        paths_to_check = []

        # Load local secrets before pydantic-settings reads the environment.
        # Plain provider keys such as OPENROUTER_API_KEY are consumed by
        # provider clients, while KNOWLEDGEFORGE_* entries override YAML config.
        secrets_path = os.getenv(
            "KNOWLEDGEFORGE_SECRETS_FILE",
            "~/.config/knowledgeforge/secrets.env",
        )
        _load_env_file(secrets_path)

        if config_path:
            paths_to_check.append(config_path)

        # Check KNOWLEDGEFORGE_CONFIG env var
        env_config_path = os.getenv("KNOWLEDGEFORGE_CONFIG")
        if env_config_path:
            paths_to_check.append(env_config_path)

        # Default locations
        paths_to_check.extend([
            "./config.yaml",
            os.path.expanduser("~/.config/knowledgeforge/config.yaml")
        ])

        # Try to load from first existing config file
        config_file_used = None
        for path in paths_to_check:
            path_obj = Path(path).resolve()
            if path_obj.exists() and path_obj.is_file():
                try:
                    with open(path_obj, "r", encoding="utf-8") as f:
                        yaml_config = yaml.safe_load(f) or {}
                    config_file_used = str(path_obj)
                    break
                except Exception as e:
                    # If we can't read the file, continue to next option
                    print(f"Warning: Could not read config file {path_obj}: {e}")
                    continue

        # Create config instance
        # pydantic-settings will automatically read environment variables
        # and they will override YAML values
        config = cls(**yaml_config)

        # Store which config file was used for debugging
        if config_file_used:
            # Store as a private attribute (not validated by pydantic)
            object.__setattr__(config, "_config_file_path", config_file_used)

        return config

    def get_config_file_path(self) -> Optional[str]:
        """
        Get the path to the config file that was loaded, if any.

        Returns:
            Path to config file, or None if only defaults/env vars were used
        """
        return getattr(self, "_config_file_path", None)

    def to_yaml(self, file_path: Optional[str] = None) -> str:
        """
        Export current configuration to YAML format.

        Args:
            file_path: Optional path to write YAML file to

        Returns:
            YAML string representation of configuration
        """
        # Convert to dict
        config_dict = self.model_dump(mode="python")

        # Generate YAML
        yaml_str = yaml.safe_dump(
            config_dict,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True
        )

        # Write to file if specified
        if file_path:
            file_path_obj = Path(file_path).expanduser()
            file_path_obj.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path_obj, "w", encoding="utf-8") as f:
                f.write(yaml_str)

        return yaml_str


# Global config instance
_global_config: Optional[KnowledgeForgeConfig] = None


def get_config() -> KnowledgeForgeConfig:
    """
    Get the global configuration instance.
    Loads configuration on first access.

    Returns:
        Global KnowledgeForgeConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = KnowledgeForgeConfig.load_config()
    return _global_config


def set_config(config: KnowledgeForgeConfig) -> None:
    """
    Set the global configuration instance.
    Useful for testing or programmatic configuration.

    Args:
        config: KnowledgeForgeConfig instance to use globally
    """
    global _global_config
    _global_config = config


def reload_config(config_path: Optional[str] = None) -> KnowledgeForgeConfig:
    """
    Reload configuration from file system.

    Args:
        config_path: Optional explicit path to config file

    Returns:
        Newly loaded KnowledgeForgeConfig instance
    """
    global _global_config
    _global_config = KnowledgeForgeConfig.load_config(config_path)
    return _global_config
