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
from knowledgeforge.core.models import SearchResponse, Discovery, IngestResult, ProjectInfo

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
    n_results: int = 5
    min_score: float = 0.0


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
    path: str
    project_name: str = ""
    full_reindex: bool = False


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
        collections=request.collections,
        project=request.project,
        tags=request.tags,
        language=request.language,
        category=request.category,
        confirmed_only=request.confirmed_only,
        n_results=request.n_results,
        min_score=request.min_score
    )


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
    if os.path.isdir(request.path):
        name = request.project_name or os.path.basename(request.path)
        return engine.ingest_project(request.path, name, request.full_reindex)
    else:
        return engine.ingest_file(request.path)


@app.get("/api/v1/projects")
async def list_projects():
    """
    List all indexed projects with statistics.

    Returns information about:
    - Obsidian vault (if configured)
    - All code projects

    Each project includes:
    - name: Project name
    - path: Absolute path
    - type: "obsidian" or "code"
    - total_chunks: Number of indexed chunks
    - file_count: Number of indexed files
    """
    engine = get_engine()
    return engine.list_projects()


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
