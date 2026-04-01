"""FastAPI REST server for KnowledgeForge."""
import time
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.engine import KnowledgeForgeEngine
from knowledgeforge.core.models import (
    SearchResponse,
    SearchSnippet,
    Discovery,
    IngestResult,
    ProjectInfo,
    SemanticRecord,
)

logger = logging.getLogger(__name__)

# Global engine
_engine = None
_start_time = time.time()


def get_engine() -> KnowledgeForgeEngine:
    """Get or create the global KnowledgeForgeEngine instance."""
    global _engine
    if _engine is None:
        config = KnowledgeForgeConfig.load_config()
        _engine = KnowledgeForgeEngine(config)
    return _engine


# Request/Response models
class SearchRequest(BaseModel):
    """Request model for search endpoint."""
    query: str
    project: Optional[str] = None
    collections: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    language: Optional[str] = None
    category: Optional[str] = None
    confirmed_only: bool = False
    max_results: int = 6
    min_score_threshold: float = 0.35
    n_results: Optional[int] = None
    min_score: Optional[float] = None


class KnowledgeContextRequest(BaseModel):
    """Request model for line-window context reads."""
    file_path: str
    start_line: int
    line_count: int = 80


class DiscoveryRequest(BaseModel):
    """Request model for creating a discovery."""
    content: str
    context: str = ""
    project: str = ""
    category: str = "gotcha"
    severity: str = "important"
    source_agent: str = "unknown"
    source_session: str = ""
    related_files: list[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    """Request model for ingestion endpoint."""
    path: str = ""
    project_name: str = ""
    full_reindex: bool = False


class ConversationSearchRequest(BaseModel):
    """Request model for conversation search endpoint."""
    query: str
    project: Optional[str] = None
    source_agent: Optional[str] = None
    after: Optional[str] = None
    before: Optional[str] = None
    n_results: int = 10
    min_score: float = 0.0


class SemanticRecordRequest(BaseModel):
    """Request model for curated semantic memory records."""
    title: str
    content: str
    project: str = ""
    record_type: str = "fact"
    tags: list[str] = Field(default_factory=list)
    source_agent: str = "unknown"
    source_session: str = ""
    trust_level: str = "T2"
    status: str = "active"
    reviewed_at: str = ""
    superseded_by: str = ""
    confidence: float = 0.9


class DiscoveryPromotionRequest(BaseModel):
    """Promote a confirmed discovery into semantic memory."""
    record_type: str = "fact"
    title: str = ""


class SemanticStatusUpdateRequest(BaseModel):
    """Archive or supersede a semantic record."""
    status: str = "archived"
    superseded_by: str = ""


class SemanticBootstrapRequest(BaseModel):
    """Bootstrap semantic coverage for a project."""
    project: str
    limit: int = 20


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize engine on startup, cleanup on shutdown."""
    logger.info("KnowledgeForge REST API starting...")
    get_engine()  # Initialize engine
    yield
    logger.info("KnowledgeForge REST API shutting down...")


# Create FastAPI app
app = FastAPI(
    title="KnowledgeForge API",
    description="Universal RAG Knowledge System REST API",
    version="0.1.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/v1/search", response_model=SearchResponse)
async def search(request: SearchRequest):
    """
    Search the knowledge base across documents, code, and discoveries.

    Example request:
    ```json
    {
      "query": "how to configure authentication",
      "collections": ["documents", "code"],
      "n_results": 10,
      "min_score": 0.5
    }
    ```
    """
    engine = get_engine()
    return engine.search(
        query=request.query,
        project=request.project,
        max_results=request.max_results,
        min_score_threshold=request.min_score_threshold,
        collections=request.collections,
        tags=request.tags,
        language=request.language,
        category=request.category,
        confirmed_only=request.confirmed_only,
        n_results=request.n_results,
        min_score=request.min_score
    )


@app.post("/api/v1/search_knowledge", response_model=list[SearchSnippet])
async def search_knowledge(request: SearchRequest):
    """Lean hybrid search endpoint for Search-then-Get workflows."""
    engine = get_engine()
    return engine.search_snippets(
        query=request.query,
        project=request.project,
        max_results=request.max_results if request.n_results is None else request.n_results,
        min_score_threshold=(
            request.min_score_threshold
            if request.min_score is None
            else request.min_score
        ),
        collections=request.collections,
        tags=request.tags,
        language=request.language,
        category=request.category,
        confirmed_only=request.confirmed_only,
    )


@app.post("/api/v1/context")
async def get_knowledge_context(request: KnowledgeContextRequest):
    """Read exact line windows from local files after search hits."""
    engine = get_engine()
    try:
        return engine.get_knowledge_context(
            request.file_path, request.start_line, request.line_count
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/discoveries", response_model=Discovery)
async def create_discovery(request: DiscoveryRequest):
    """
    Store a new discovery with automatic deduplication.

    Example request:
    ```json
    {
      "content": "Database connection pool must be closed in finally block",
      "context": "Found during integration testing",
      "project": "user-service",
      "category": "bugfix",
      "severity": "critical",
      "source_agent": "debugging-agent",
      "related_files": ["src/db/connection.py"]
    }
    ```
    """
    engine = get_engine()
    return engine.store_discovery(
        content=request.content,
        context=request.context,
        project=request.project,
        category=request.category,
        severity=request.severity,
        source_agent=request.source_agent,
        source_session=request.source_session,
        related_files=request.related_files
    )


@app.post("/api/v1/semantic-records", response_model=SemanticRecord)
async def create_semantic_record(request: SemanticRecordRequest):
    """Create a curated semantic record (fact, runbook, or project overview)."""
    engine = get_engine()
    record = SemanticRecord(
        title=request.title,
        content=request.content,
        project=request.project,
        record_type=request.record_type,
        tags=request.tags,
        source_agent=request.source_agent,
        source_session=request.source_session,
        trust_level=request.trust_level,
        status=request.status,
        reviewed_at=request.reviewed_at,
        superseded_by=request.superseded_by,
        confidence=request.confidence,
    )
    return engine.store_semantic_record(record)


@app.post("/api/v1/semantic-records/search", response_model=SearchResponse)
async def search_semantic_records(request: SearchRequest):
    """Search semantic collections only."""
    engine = get_engine()
    record_type = None
    if request.collections and len(request.collections) == 1:
        alias = request.collections[0]
        if alias in ["fact", "runbook", "project_overview"]:
            record_type = alias
    return engine.search_semantic_records(
        query=request.query,
        record_type=record_type,
        project=request.project,
        max_results=request.max_results if request.n_results is None else request.n_results,
        min_score_threshold=request.min_score_threshold if request.min_score is None else request.min_score,
    )


@app.post("/api/v1/discoveries/{discovery_id}/promote-semantic", response_model=SemanticRecord)
async def promote_discovery_to_semantic(discovery_id: str, request: DiscoveryPromotionRequest):
    """Promote a confirmed discovery into a semantic record."""
    engine = get_engine()
    return engine.promote_discovery_to_semantic(discovery_id, request.record_type, request.title)


@app.post("/api/v1/semantic-records/suggest-promotions")
async def suggest_promotions(request: SemanticBootstrapRequest):
    """Suggest confirmed discoveries that should likely be promoted next."""
    engine = get_engine()
    return engine.suggest_promotions(project=request.project, limit=request.limit)


@app.post("/api/v1/semantic-records/generate-overview", response_model=SemanticRecord)
async def generate_project_overview(request: SemanticBootstrapRequest):
    """Generate and store a first-pass project overview."""
    engine = get_engine()
    return engine.generate_project_overview(request.project)


@app.post("/api/v1/semantic-records/bootstrap-project")
async def bootstrap_project_semantic_coverage(request: SemanticBootstrapRequest):
    """Bootstrap semantic coverage for a project."""
    engine = get_engine()
    return engine.bootstrap_project_semantic_coverage(request.project)


@app.get("/api/v1/semantic-records")
async def list_semantic_records(
    record_type: Optional[str] = Query(None, description="fact | runbook | project_overview"),
    project: Optional[str] = Query(None, description="Filter by project"),
    status: str = Query("active", description="active | archived | superseded | expired"),
    limit: int = Query(100, description="Max records to return"),
):
    """List semantic records across semantic collections."""
    engine = get_engine()
    return engine.list_semantic_records(record_type=record_type, project=project, status=status, limit=limit)


@app.patch("/api/v1/semantic-records/{record_type}/{record_id}")
async def update_semantic_record_status(record_type: str, record_id: str, request: SemanticStatusUpdateRequest):
    """Archive or supersede a semantic record."""
    engine = get_engine()
    updated = engine.update_semantic_record_status(record_id, record_type, request.status, request.superseded_by)
    if not updated:
        raise HTTPException(status_code=404, detail="Semantic record not found")
    return {"updated": True, "record_id": record_id, "record_type": record_type, "status": request.status}


@app.get("/api/v1/discoveries")
async def list_discoveries(
    project: Optional[str] = Query(None, description="Filter by project name"),
    unconfirmed_only: bool = Query(False, description="Only return unconfirmed discoveries"),
    category: Optional[str] = Query(None, description="Filter by category")
):
    """
    List discoveries with optional filters.

    Query parameters:
    - project: Filter by project name
    - unconfirmed_only: Only return unconfirmed discoveries (default: false)
    - category: Filter by category (bugfix|gotcha|performance|etc)
    """
    engine = get_engine()
    discoveries = engine.get_discoveries(
        project=project,
        unconfirmed_only=unconfirmed_only,
        category=category
    )
    return discoveries


@app.patch("/api/v1/discoveries/{discovery_id}/confirm", response_model=Discovery)
async def confirm_discovery(discovery_id: str):
    """
    Confirm a discovery and optionally promote it to Obsidian vault.

    Path parameters:
    - discovery_id: UUID of the discovery to confirm
    """
    engine = get_engine()
    result = engine.confirm_discovery(discovery_id)
    if not result:
        raise HTTPException(status_code=404, detail="Discovery not found")
    return result


@app.delete("/api/v1/discoveries/{discovery_id}")
async def delete_discovery(discovery_id: str):
    """
    Reject and delete a discovery.

    Path parameters:
    - discovery_id: UUID of the discovery to delete
    """
    engine = get_engine()
    deleted = engine.reject_discovery(discovery_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Discovery not found")
    return {"deleted": True}


@app.post("/api/v1/ingest", response_model=IngestResult)
async def ingest(request: IngestRequest):
    """
    Trigger ingestion of a file or directory.

    For directories, uses project_name to identify the project.
    For files, auto-detects collection based on extension.

    Example request:
    ```json
    {
      "path": "/path/to/project",
      "project_name": "my-project",
      "full_reindex": false
    }
    ```
    """
    engine = get_engine()
    import os

    if request.project_name and not request.path:
        return engine.ingest_registered_project(request.project_name, request.full_reindex)

    if os.path.isdir(request.path):
        name = request.project_name or os.path.basename(request.path)
        return engine.ingest_project(request.path, name, request.full_reindex)
    else:
        return engine.ingest_file(request.path)


@app.get("/api/v1/projects")
async def list_projects():
    """List all indexed/configured projects with statistics."""
    engine = get_engine()
    return engine.list_projects()


@app.get("/api/v1/projects/audit")
async def project_audit():
    """Return ingest/health audit data for configured projects."""
    engine = get_engine()
    return engine.get_project_audit()


@app.get("/api/v1/semantic-records/audit")
async def semantic_audit():
    """Return lifecycle/linkback audit data for semantic memory."""
    engine = get_engine()
    return engine.get_semantic_audit()


@app.get("/api/v1/stats")
async def get_stats():
    """
    Get system-wide statistics.

    Returns:
    - collections: Count of chunks per collection
    - total_chunks: Total chunks across all collections
    - embedding_model: Model being used for embeddings
    - data_dir: Data directory path
    - obsidian_vault_configured: Whether Obsidian vault is configured
    - code_projects_configured: Number of code projects configured
    """
    engine = get_engine()
    return engine.get_stats()


@app.get("/api/v1/health")
async def health():
    """
    Health check endpoint.

    Returns:
    - status: "ok" if system is healthy
    - collections: Collection statistics
    - uptime_seconds: Server uptime in seconds
    """
    engine = get_engine()
    stats = engine.get_stats()
    uptime = time.time() - _start_time
    return {
        "status": "ok",
        "collections": stats.get("collections", {}),
        "uptime_seconds": round(uptime, 2)
    }


@app.delete("/api/v1/documents")
async def delete_documents(
    source_file: str = Query(..., description="Source filename to delete")
):
    """
    Delete all documents/chunks for a given source file.

    This removes all chunks from all collections that match the source_file.

    Args:
        source_file: The filename (basename) of the source file to delete

    Returns:
        deleted: Number of chunks deleted
    """
    engine = get_engine()
    total_deleted = 0

    collections = [
        engine.config.docs_collection,
        engine.config.code_collection,
    ]

    for collection in collections:
        try:
            deleted = engine.store.delete_by_source_file(collection, source_file)
            total_deleted += deleted
            if deleted > 0:
                logger.info(f"Deleted {deleted} chunks from {collection} for {source_file}")
        except Exception as e:
            logger.error(f"Error deleting from {collection}: {e}")

    return {"deleted": total_deleted}


@app.post("/api/v1/conversations/search", response_model=SearchResponse)
async def search_conversations(request: ConversationSearchRequest):
    """
    Search past conversations semantically.

    Example request:
    ```json
    {
      "query": "telegram bot setup",
      "source_agent": "claude",
      "n_results": 5
    }
    ```
    """
    engine = get_engine()
    return engine.search_conversations(
        query=request.query,
        project=request.project,
        source_agent=request.source_agent,
        after=request.after,
        before=request.before,
        n_results=request.n_results,
        min_score=request.min_score,
    )


@app.post("/api/v1/conversations/sync", response_model=IngestResult)
async def sync_conversations():
    """
    Trigger manual sync and indexing of conversation files.

    Scans configured conversation source directories for new/updated JSONL
    files and indexes them into the conversations collection.
    """
    engine = get_engine()
    return engine.ingest_conversations()


@app.get("/api/v1/conversations/stats")
async def conversation_stats():
    """
    Get conversation-specific statistics.

    Returns:
    - total_exchanges: Number of indexed conversation exchanges
    - by_agent: Count by source agent (claude, codex, gemini)
    - by_project: Count by project
    """
    engine = get_engine()
    return engine.get_conversation_stats()


@app.get("/api/v1/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    start_line: Optional[int] = Query(None, description="Starting line (1-indexed)"),
    end_line: Optional[int] = Query(None, description="Ending line (1-indexed)"),
):
    """
    Read a specific conversation by session ID.

    Returns formatted markdown with human messages, assistant responses,
    and tool usage.
    """
    engine = get_engine()
    content = engine.get_conversation(session_id, start_line, end_line)
    return {"session_id": session_id, "content": content}


def main():
    """Run the REST API server."""
    import uvicorn
    config = KnowledgeForgeConfig.load_config()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "knowledgeforge.interfaces.rest_api:app",
        host=config.rest_host,
        port=config.rest_port,
        reload=False
    )


if __name__ == "__main__":
    main()
