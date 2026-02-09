"""FastMCP server exposing KnowledgeForge to Claude Code."""
import os
import json
import logging
import sys
from mcp.server.fastmcp import FastMCP

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.engine import KnowledgeForgeEngine

logger = logging.getLogger(__name__)

# Create MCP server
mcp = FastMCP("KnowledgeForge")

# Global engine instance (lazy initialized)
_engine = None

def get_engine() -> KnowledgeForgeEngine:
    global _engine
    if _engine is None:
        config = KnowledgeForgeConfig.load_config()
        _engine = KnowledgeForgeEngine(config)
    return _engine


@mcp.tool()
def search_knowledge(
    query: str,
    project: str = "",
    collections: str = "",
    tags: str = "",
    language: str = "",
    n_results: int = 5
) -> str:
    """Search the knowledge base across documentation, code, and agent discoveries.
    Returns relevant chunks with source information and similarity scores.

    Args:
        query: Search query text
        project: Filter by project name (optional)
        collections: Comma-separated collection names to search: docs,code,discoveries (optional, default: all)
        tags: Comma-separated tags to filter by (optional, Obsidian docs only)
        language: Programming language filter (optional, code only)
        n_results: Number of results to return (default: 5)
    """
    engine = get_engine()

    # Parse comma-separated inputs
    col_list = [c.strip() for c in collections.split(",") if c.strip()] or None
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None

    response = engine.search(
        query=query,
        collections=col_list,
        project=project or None,
        tags=tag_list,
        language=language or None,
        n_results=n_results
    )

    # Format results as readable text
    lines = [f"Found {response.total_results} results ({response.search_time_ms}ms):\n"]
    for i, r in enumerate(response.results, 1):
        source = r.metadata.get("source_file", "unknown")
        lines.append(f"--- Result {i} (score: {r.score}, collection: {r.collection}) ---")
        lines.append(f"Source: {source}")
        lines.append(f"{r.content[:500]}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def store_discovery(
    content: str,
    context: str = "",
    project: str = "",
    category: str = "gotcha",
    severity: str = "important",
    related_files: str = ""
) -> str:
    """Store a discovery or insight found during a programming or debugging session.
    Use this when you find something non-obvious: undocumented behavior, surprising gotchas,
    configuration dependencies, performance insights, or useful patterns.

    Args:
        content: The discovery content - what you found
        context: What you were working on when you found it
        project: Project name this discovery relates to
        category: Category: bugfix|gotcha|performance|config|pattern|dependency|workaround|security
        severity: Severity: critical|important|nice-to-know
        related_files: Comma-separated file paths related to this discovery
    """
    engine = get_engine()
    files = [f.strip() for f in related_files.split(",") if f.strip()]

    discovery = engine.store_discovery(
        content=content,
        context=context,
        project=project,
        category=category,
        severity=severity,
        source_agent="claude-code",
        related_files=files
    )

    return f"Discovery stored (ID: {discovery.discovery_id[:8]}...)\nCategory: {discovery.category}\nSeverity: {discovery.severity}"


@mcp.tool()
def get_project_context(project: str) -> str:
    """Get a high-level overview of a project: file summaries, architecture notes,
    recent discoveries, and key documentation.

    Args:
        project: Project name to get context for
    """
    engine = get_engine()

    # Search for project overview content
    docs = engine.search(query=f"{project} architecture overview", project=project,
                        collections=[engine.config.docs_collection], n_results=3)
    code = engine.search(query=f"{project} main entry point", project=project,
                        collections=[engine.config.code_collection], n_results=3)
    discoveries = engine.get_discoveries(project=project)

    lines = [f"# Project Context: {project}\n"]

    lines.append("## Documentation")
    for r in docs.results:
        lines.append(f"- [{r.metadata.get('source_file', '?')}] {r.content[:200]}")

    lines.append("\n## Code")
    for r in code.results:
        source = r.metadata.get("source_file", "?")
        symbol = r.metadata.get("symbol_name", "")
        lines.append(f"- [{source}] {symbol}: {r.content[:150]}")

    lines.append(f"\n## Discoveries ({len(discoveries)} total)")
    for d in discoveries[:5]:
        status = "✓" if d.confirmed else "?"
        lines.append(f"- [{status}] [{d.category}] {d.content[:100]}")

    return "\n".join(lines)


@mcp.tool()
def list_projects() -> str:
    """List all indexed projects with their stats."""
    engine = get_engine()
    projects = engine.list_projects()

    if not projects:
        return "No projects indexed yet."

    lines = ["Indexed Projects:\n"]
    for p in projects:
        lines.append(f"- {p.name} ({p.type}): {p.total_chunks} chunks, {p.file_count} files")
        lines.append(f"  Path: {p.path}")

    return "\n".join(lines)


@mcp.tool()
def ingest_path(path: str, project_name: str = "") -> str:
    """Trigger ingestion of a file or directory into the knowledge base.

    Args:
        path: Path to file or directory to ingest
        project_name: Project name (required for directories, auto-detected for files)
    """
    engine = get_engine()

    import os
    if os.path.isdir(path):
        name = project_name or os.path.basename(path)
        result = engine.ingest_project(path, name)
    else:
        result = engine.ingest_file(path)

    errors_str = f"\nErrors: {'; '.join(result.errors)}" if result.errors else ""
    return (f"Ingestion complete: {result.files_processed} files processed, "
            f"{result.files_skipped} skipped, {result.chunks_created} chunks created "
            f"({result.duration_seconds}s){errors_str}")


@mcp.tool()
def get_discoveries(
    project: str = "",
    unconfirmed_only: bool = False,
    category: str = ""
) -> str:
    """Retrieve discoveries from past debugging and programming sessions.

    Args:
        project: Filter by project name
        unconfirmed_only: Only show unconfirmed discoveries
        category: Filter by category (bugfix|gotcha|performance|config|pattern|dependency|workaround|security)
    """
    engine = get_engine()
    discoveries = engine.get_discoveries(
        project=project or None,
        unconfirmed_only=unconfirmed_only,
        category=category or None
    )

    if not discoveries:
        return "No discoveries found."

    lines = [f"Found {len(discoveries)} discoveries:\n"]
    for d in discoveries:
        status = "Confirmed" if d.confirmed else "Unconfirmed"
        lines.append(f"ID: {d.discovery_id[:8]}... | {d.category} | {d.severity} | {status}")
        lines.append(f"  {d.content[:200]}")
        if d.context:
            lines.append(f"  Context: {d.context[:100]}")
        lines.append("")

    return "\n".join(lines)


def main():
    """Run the MCP server."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    transport = os.environ.get("KNOWLEDGEFORGE_MCP_TRANSPORT", "stdio")

    if transport == "sse":
        port = int(os.environ.get("KNOWLEDGEFORGE_MCP_PORT", "8743"))
        mcp.run(transport="sse", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
