"""Pydantic data models for KnowledgeForge."""

from pydantic import BaseModel, Field, model_validator
from uuid import uuid4
from datetime import datetime, timezone


class Chunk(BaseModel):
    """A chunk of text with metadata, ready for embedding and storage."""
    chunk_id: str                      # Deterministic ID: hash of source_file + chunk_index
    content: str                       # The actual text content
    file_path: str = ""                # Canonical source file location
    content_hash: str = ""             # SHA256 of original source file content
    source_file: str = ""              # Legacy alias for file_path
    source_file_hash: str = ""         # Legacy alias for content_hash
    chunk_index: int                   # Position within the file
    chunk_type: str                    # heading_section, paragraph, code_block, file_summary, function, class, method, module_summary, import_block, config

    # Trust/lifecycle metadata
    trust_level: str = "T1"           # T1 authoritative, T2 curated, T3 episodic, T4 raw
    status: str = "active"            # active | archived | superseded | expired
    reviewed_at: str = ""
    superseded_by: str = ""
    confidence: float = 1.0

    # Optional fields (collection-specific)
    vault_name: str = ""
    heading_path: str = ""             # "H1 > H2 > H3"
    frontmatter_tags: str = ""         # Comma-separated
    frontmatter_project: str = ""
    frontmatter_status: str = ""
    wiki_links_out: str = ""           # Comma-separated
    wiki_links_in: str = ""

    # Code-specific
    project_name: str = ""
    language: str = ""
    symbol_name: str = ""
    start_line: int = 0
    end_line: int = 0
    dependencies: str = ""             # Comma-separated imports
    docstring: str = ""                # First 200 chars

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def normalize_file_metadata(self) -> "Chunk":
        """Keep canonical and legacy file metadata keys in sync."""
        if not self.file_path and self.source_file:
            self.file_path = self.source_file
        if not self.source_file and self.file_path:
            self.source_file = self.file_path

        if not self.content_hash and self.source_file_hash:
            self.content_hash = self.source_file_hash
        if not self.source_file_hash and self.content_hash:
            self.source_file_hash = self.content_hash

        return self

    def to_metadata(self) -> dict:
        """Convert to flat dict for ChromaDB metadata storage.
        Exclude content (stored separately in ChromaDB documents field).
        Only include non-empty string fields and non-zero numeric fields.

        ChromaDB constraints:
        - Metadata only supports: str, int, float, bool
        - NO lists, NO None values
        - Content and chunk_id handled separately by ChromaDB
        """
        metadata = {}

        # Always include these core fields
        metadata["file_path"] = self.file_path
        metadata["content_hash"] = self.content_hash
        # Legacy compatibility for existing consumers/data
        metadata["source_file"] = self.file_path
        metadata["source_file_hash"] = self.content_hash
        metadata["chunk_index"] = self.chunk_index
        metadata["chunk_type"] = self.chunk_type
        metadata["start_line"] = int(self.start_line)
        metadata["end_line"] = int(self.end_line)
        metadata["created_at"] = self.created_at
        metadata["updated_at"] = self.updated_at
        metadata["trust_level"] = self.trust_level
        metadata["status"] = self.status
        metadata["confidence"] = float(self.confidence)

        # Optional string fields - only include if non-empty
        optional_str_fields = [
            "vault_name",
            "heading_path",
            "frontmatter_tags",
            "frontmatter_project",
            "frontmatter_status",
            "wiki_links_out",
            "wiki_links_in",
            "project_name",
            "language",
            "symbol_name",
            "dependencies",
            "docstring",
            "reviewed_at",
            "superseded_by",
        ]

        for field_name in optional_str_fields:
            value = getattr(self, field_name)
            if value:  # Only include non-empty strings
                metadata[field_name] = value

        return metadata


class SearchResult(BaseModel):
    """A single search result from ChromaDB."""
    content: str
    score: float                       # 0-1 higher=better
    metadata: dict
    collection: str                    # docs, code, or discoveries


class SearchResponse(BaseModel):
    """Complete search response with all results."""
    query: str
    results: list[SearchResult]
    total_results: int
    search_time_ms: float


class SearchSnippet(BaseModel):
    """Lean snippet response for Search-then-Get workflows."""
    text_preview: str
    file_path: str
    start_line: int
    end_line: int
    score: float


class Discovery(BaseModel):
    """A discovered insight, bug fix, or learning from AI agents."""
    discovery_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    context: str = ""
    project: str = ""
    category: str = "gotcha"           # bugfix|gotcha|performance|config|pattern|dependency|workaround|security
    severity: str = "important"        # critical|important|nice-to-know
    source_agent: str = "unknown"
    source_session: str = ""
    related_files: list[str] = Field(default_factory=list)
    confirmed: bool = False
    confirmed_at: str = ""
    promoted_to_obsidian: bool = False
    promoted_semantic_record_id: str = ""
    promoted_semantic_record_type: str = ""
    trust_level: str = "T3"
    status: str = "active"
    reviewed_at: str = ""
    superseded_by: str = ""
    confidence: float = 0.7
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_metadata(self) -> dict:
        """Convert to flat dict for ChromaDB metadata storage.

        ChromaDB constraints:
        - Metadata only supports: str, int, float, bool
        - Convert lists to comma-separated strings
        - Skip empty strings to keep metadata lean
        """
        metadata = {
            "discovery_id": self.discovery_id,
            "category": self.category,
            "severity": self.severity,
            "source_agent": self.source_agent,
            "confirmed": self.confirmed,
            "promoted_to_obsidian": self.promoted_to_obsidian,
            "trust_level": self.trust_level,
            "status": self.status,
            "confidence": float(self.confidence),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

        # Optional string fields - only include if non-empty
        if self.context:
            metadata["context"] = self.context
        if self.project:
            metadata["project"] = self.project
        if self.source_session:
            metadata["source_session"] = self.source_session
        if self.confirmed_at:
            metadata["confirmed_at"] = self.confirmed_at
        if self.reviewed_at:
            metadata["reviewed_at"] = self.reviewed_at
        if self.superseded_by:
            metadata["superseded_by"] = self.superseded_by
        if self.promoted_semantic_record_id:
            metadata["promoted_semantic_record_id"] = self.promoted_semantic_record_id
        if self.promoted_semantic_record_type:
            metadata["promoted_semantic_record_type"] = self.promoted_semantic_record_type

        # Convert related_files list to comma-separated string
        if self.related_files:
            metadata["related_files"] = ",".join(self.related_files)

        return metadata

    @classmethod
    def from_metadata(cls, metadata: dict, content: str) -> "Discovery":
        """Reconstruct Discovery from ChromaDB metadata + content.

        Args:
            metadata: Flat dict from ChromaDB
            content: The discovery content text

        Returns:
            Discovery instance with all fields restored
        """
        # Parse related_files from comma-separated string back to list
        related_files_str = metadata.get("related_files", "")
        related_files = [f.strip() for f in related_files_str.split(",") if f.strip()]

        return cls(
            discovery_id=metadata.get("discovery_id", str(uuid4())),
            content=content,
            context=metadata.get("context", ""),
            project=metadata.get("project", ""),
            category=metadata.get("category", "gotcha"),
            severity=metadata.get("severity", "important"),
            source_agent=metadata.get("source_agent", "unknown"),
            source_session=metadata.get("source_session", ""),
            related_files=related_files,
            confirmed=metadata.get("confirmed", False),
            confirmed_at=metadata.get("confirmed_at", ""),
            promoted_to_obsidian=metadata.get("promoted_to_obsidian", False),
            promoted_semantic_record_id=metadata.get("promoted_semantic_record_id", ""),
            promoted_semantic_record_type=metadata.get("promoted_semantic_record_type", ""),
            trust_level=metadata.get("trust_level", "T3"),
            status=metadata.get("status", "active"),
            reviewed_at=metadata.get("reviewed_at", ""),
            superseded_by=metadata.get("superseded_by", ""),
            confidence=float(metadata.get("confidence", 0.7)),
            created_at=metadata.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=metadata.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


class ToolCallRecord(BaseModel):
    """A single tool call within a conversation exchange."""
    tool_name: str
    tool_input: str = ""
    tool_result: str = ""
    is_error: bool = False


class ConversationExchange(BaseModel):
    """A paired human+assistant exchange from a Claude Code conversation."""
    exchange_id: str                       # SHA256 of archive_path:line_start-line_end
    session_id: str
    project: str
    timestamp: str                         # ISO 8601
    user_message: str
    assistant_message: str
    source_agent: str = "claude"           # "claude" | "codex" | "gemini"
    archive_path: str = ""
    line_start: int = 0
    line_end: int = 0
    cwd: str = ""
    git_branch: str = ""
    claude_version: str = ""
    thinking_level: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    tool_error_count: int = 0
    is_sidechain: bool = False
    parent_uuid: str = ""
    enrichment: dict = Field(default_factory=dict)

    def to_metadata(self) -> dict:
        """Convert to flat dict for ChromaDB metadata storage.

        ChromaDB constraints: str, int, float, bool only.
        """
        metadata = {
            "exchange_id": self.exchange_id,
            "session_id": self.session_id,
            "project": self.project,
            "timestamp": self.timestamp,
            "source_agent": self.source_agent,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "tool_error_count": self.tool_error_count,
        }

        if self.archive_path:
            metadata["archive_path"] = self.archive_path
        if self.cwd:
            metadata["cwd"] = self.cwd
        if self.git_branch:
            metadata["git_branch"] = self.git_branch
        if self.claude_version:
            metadata["claude_version"] = self.claude_version
        if self.thinking_level:
            metadata["thinking_level"] = self.thinking_level
        if self.tool_names:
            metadata["tool_names"] = ",".join(self.tool_names)
        if self.parent_uuid:
            metadata["parent_uuid"] = self.parent_uuid
        if self.is_sidechain:
            metadata["is_sidechain"] = True

        # Kimi enrichment fields
        e = self.enrichment
        if e.get("category"):
            metadata["category"] = e["category"]
        if e.get("topics"):
            metadata["topics"] = e["topics"]
        if e.get("technologies"):
            metadata["technologies"] = e["technologies"]
        if e.get("intent"):
            metadata["intent"] = e["intent"]
        if e.get("complexity"):
            metadata["complexity"] = e["complexity"]
        if e.get("key_files"):
            metadata["key_files"] = e["key_files"]

        return metadata

    def build_embedding_content(self, max_user_chars: int = 800, max_asst_chars: int = 800) -> str:
        """Build the text to embed in ChromaDB.

        If Kimi enrichment is available, uses structured format with
        summary/intent/topics front-loaded for better semantic search.
        Otherwise falls back to basic user+assistant+tools.
        """
        e = self.enrichment
        if e.get("summary"):
            parts = [f"Summary: {e['summary']}"]
            if e.get("intent"):
                parts.append(f"Intent: {e['intent']}")
            if e.get("category"):
                parts.append(f"Category: {e['category']}")
            if e.get("topics"):
                parts.append(f"Topics: {e['topics']}")
            if e.get("technologies"):
                parts.append(f"Technologies: {e['technologies']}")
            parts.append(f"User: {self.user_message[:max_user_chars]}")
            parts.append(f"Assistant: {self.assistant_message[:max_asst_chars]}")
            if self.tool_names:
                parts.append(f"Tools: {', '.join(self.tool_names)}")
            if e.get("searchable_text"):
                parts.append(f"Context: {e['searchable_text']}")
            return "\n\n".join(parts)

        # Basic format
        parts = [f"User: {self.user_message[:max_user_chars]}"]
        parts.append(f"Assistant: {self.assistant_message[:max_asst_chars]}")
        if self.tool_names:
            parts.append(f"Tools: {', '.join(self.tool_names)}")
        return "\n\n".join(parts)

    @classmethod
    def from_metadata(cls, metadata: dict, content: str) -> "ConversationExchange":
        """Reconstruct from ChromaDB metadata + content."""
        tool_names_str = metadata.get("tool_names", "")
        tool_names = [t.strip() for t in tool_names_str.split(",") if t.strip()]

        enrichment = {}
        for key in ("category", "topics", "technologies", "intent", "complexity", "key_files"):
            if key in metadata:
                enrichment[key] = metadata[key]

        return cls(
            exchange_id=metadata.get("exchange_id", ""),
            session_id=metadata.get("session_id", ""),
            project=metadata.get("project", ""),
            timestamp=metadata.get("timestamp", ""),
            user_message=content,
            assistant_message="",
            source_agent=metadata.get("source_agent", "claude"),
            archive_path=metadata.get("archive_path", ""),
            line_start=metadata.get("line_start", 0),
            line_end=metadata.get("line_end", 0),
            cwd=metadata.get("cwd", ""),
            git_branch=metadata.get("git_branch", ""),
            claude_version=metadata.get("claude_version", ""),
            thinking_level=metadata.get("thinking_level", ""),
            tool_names=tool_names,
            tool_error_count=metadata.get("tool_error_count", 0),
            is_sidechain=metadata.get("is_sidechain", False),
            parent_uuid=metadata.get("parent_uuid", ""),
            enrichment=enrichment,
        )


class IngestResult(BaseModel):
    """Result of an ingestion operation."""
    files_processed: int
    files_skipped: int                 # Unchanged files (same hash)
    chunks_created: int
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float


class SemanticRecord(BaseModel):
    """Curated semantic memory record for facts, runbooks, and project overviews."""
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    content: str
    project: str = ""
    record_type: str = "fact"        # fact | runbook | project_overview
    tags: list[str] = Field(default_factory=list)
    source_agent: str = "unknown"
    source_session: str = ""
    source_discovery_id: str = ""
    trust_level: str = "T2"
    status: str = "active"
    reviewed_at: str = ""
    superseded_by: str = ""
    confidence: float = 0.9
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_metadata(self) -> dict:
        metadata = {
            "record_id": self.record_id,
            "title": self.title,
            "project": self.project,
            "record_type": self.record_type,
            "source_agent": self.source_agent,
            "source_session": self.source_session,
            "source_discovery_id": self.source_discovery_id,
            "trust_level": self.trust_level,
            "status": self.status,
            "confidence": float(self.confidence),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.tags:
            metadata["tags"] = ",".join(self.tags)
        if self.reviewed_at:
            metadata["reviewed_at"] = self.reviewed_at
        if self.superseded_by:
            metadata["superseded_by"] = self.superseded_by
        return metadata

    @classmethod
    def from_metadata(cls, metadata: dict, content: str) -> "SemanticRecord":
        tags_str = metadata.get("tags", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        return cls(
            record_id=metadata.get("record_id", str(uuid4())),
            title=metadata.get("title", ""),
            content=content,
            project=metadata.get("project", ""),
            record_type=metadata.get("record_type", "fact"),
            tags=tags,
            source_agent=metadata.get("source_agent", "unknown"),
            source_session=metadata.get("source_session", ""),
            source_discovery_id=metadata.get("source_discovery_id", ""),
            trust_level=metadata.get("trust_level", "T2"),
            status=metadata.get("status", "active"),
            reviewed_at=metadata.get("reviewed_at", ""),
            superseded_by=metadata.get("superseded_by", ""),
            confidence=float(metadata.get("confidence", 0.9)),
            created_at=metadata.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=metadata.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


class ProjectInfo(BaseModel):
    """Information about an indexed project."""
    name: str
    path: str
    type: str                          # "obsidian" | "code"
    total_chunks: int
    last_indexed: str
    file_count: int
    status: str = "registered"        # registered | indexed | stale | error
    error_count: int = 0
