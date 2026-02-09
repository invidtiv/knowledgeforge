"""Pydantic data models for KnowledgeForge."""

from pydantic import BaseModel, Field
from typing import Optional
from uuid import uuid4
from datetime import datetime, timezone


class Chunk(BaseModel):
    """A chunk of text with metadata, ready for embedding and storage."""
    chunk_id: str                      # Deterministic ID: hash of source_file + chunk_index
    content: str                       # The actual text content
    source_file: str                   # Relative path from vault/project root
    source_file_hash: str              # SHA256 of original file content
    chunk_index: int                   # Position within the file
    chunk_type: str                    # heading_section, paragraph, code_block, file_summary, function, class, method, module_summary, import_block, config

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
        metadata["source_file"] = self.source_file
        metadata["source_file_hash"] = self.source_file_hash
        metadata["chunk_index"] = self.chunk_index
        metadata["chunk_type"] = self.chunk_type
        metadata["created_at"] = self.created_at
        metadata["updated_at"] = self.updated_at

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
        ]

        for field_name in optional_str_fields:
            value = getattr(self, field_name)
            if value:  # Only include non-empty strings
                metadata[field_name] = value

        # Optional numeric fields - only include if non-zero
        if self.start_line > 0:
            metadata["start_line"] = self.start_line
        if self.end_line > 0:
            metadata["end_line"] = self.end_line

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
            created_at=metadata.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=metadata.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


class IngestResult(BaseModel):
    """Result of an ingestion operation."""
    files_processed: int
    files_skipped: int                 # Unchanged files (same hash)
    chunks_created: int
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float


class ProjectInfo(BaseModel):
    """Information about an indexed project."""
    name: str
    path: str
    type: str                          # "obsidian" | "code"
    total_chunks: int
    last_indexed: str
    file_count: int
