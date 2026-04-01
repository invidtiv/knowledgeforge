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
    max_results: int = 6,
    min_score_threshold: float = 0.35,
    n_results: int = 0,
) -> list[dict]:
    """Search the knowledge base across documentation, code, and agent discoveries.
    Returns lean snippets suitable for Search-then-Get workflows.

    Args:
        query: Search query text
        project: Filter by project name (optional)
        collections: Comma-separated collection names to search: docs,code,discoveries (optional, default: all)
        tags: Comma-separated tags to filter by (optional, Obsidian docs only)
        language: Programming language filter (optional, code only)
        max_results: Number of snippets to return (default: 6)
        min_score_threshold: Minimum fused relevance score (default: 0.35)
        n_results: Backward-compatible alias for max_results
    """
    engine = get_engine()
    if n_results > 0:
        max_results = n_results

    # Parse comma-separated inputs
    col_list = [c.strip() for c in collections.split(",") if c.strip()] or None
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None

    snippets = engine.search_snippets(
        query=query,
        project=project or None,
        max_results=max_results,
        min_score_threshold=min_score_threshold,
        collections=col_list,
        tags=tag_list,
        language=language or None,
    )
    return [s.model_dump() for s in snippets]


@mcp.tool()
def get_knowledge_context(file_path: str, start_line: int, line_count: int = 80) -> dict:
    """Read exact source lines after a search hit.

    Args:
        file_path: File path returned by search_knowledge
        start_line: Starting line number (1-indexed)
        line_count: Number of lines to read
    """
    engine = get_engine()
    return engine.get_knowledge_context(file_path, start_line, line_count)


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
def search_semantic_memory(
    query: str,
    record_type: str = "",
    project: str = "",
    max_results: int = 6,
    min_score_threshold: float = 0.35,
) -> str:
    """Search curated semantic memory only: facts, runbooks, and project overviews.

    Args:
        query: Search query text
        record_type: Optional: fact | runbook | project_overview
        project: Optional project filter
        max_results: Max number of results
        min_score_threshold: Minimum score threshold
    """
    engine = get_engine()
    response = engine.search_semantic_records(
        query=query,
        record_type=record_type or None,
        project=project or None,
        max_results=max_results,
        min_score_threshold=min_score_threshold,
    )

    if not response.results:
        return "No semantic memory results found."

    lines = [f"Found {response.total_results} semantic results ({response.search_time_ms}ms):\n"]
    for i, r in enumerate(response.results, 1):
        meta = r.metadata or {}
        lines.append(f"--- Result {i} (score: {r.score}) ---")
        lines.append(f"Type: {meta.get('record_type', '?')} | Project: {meta.get('project', '-') or '-'} | Trust: {meta.get('trust_level', '?')} | Status: {meta.get('status', '?')}")
        title = meta.get('title', '')
        if title:
            lines.append(f"Title: {title}")
        lines.append(r.content[:500])
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def list_semantic_memory(
    record_type: str = "",
    project: str = "",
    status: str = "active",
    limit: int = 50,
) -> str:
    """List curated semantic memory records.

    Args:
        record_type: Optional: fact | runbook | project_overview
        project: Optional project filter
        status: active | archived | superseded | expired
        limit: Max number of records
    """
    engine = get_engine()
    records = engine.list_semantic_records(
        record_type=record_type or None,
        project=project or None,
        status=status,
        limit=limit,
    )

    if not records:
        return "No semantic memory records found."

    lines = [f"Found {len(records)} semantic memory records:\n"]
    for r in records:
        lines.append(
            f"- {r.record_id[:8]}... | {r.record_type} | {r.title} | project={r.project or '-'} | status={r.status} | trust={r.trust_level} | confidence={r.confidence:.2f}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_semantic_audit() -> str:
    """Show lifecycle, linkback, stale-review, and coverage audit data for semantic memory."""
    engine = get_engine()
    audit = engine.get_semantic_audit()
    summary = audit["summary"]

    lines = ["Semantic Memory Audit:\n"]
    lines.append(f"- Active records: {summary['active_records']}")
    lines.append(f"- Archived records: {summary['archived_records']}")
    lines.append(f"- Superseded records: {summary['superseded_records']}")
    lines.append(f"- Records with discovery linkback: {summary['records_with_discovery_linkback']}")
    lines.append(f"- Discoveries with semantic linkback: {summary['discoveries_with_semantic_linkback']}")
    lines.append(f"- Records missing reviewed_at: {summary['records_missing_reviewed_at']}")
    lines.append(f"- Confirmed discoveries not promoted: {summary['confirmed_discoveries_not_promoted']}")
    lines.append(f"- Coverage gap projects: {summary['coverage_gap_projects']}")
    lines.append(f"- Superseded without replacement: {summary['superseded_without_replacement']}")
    lines.append("\nBy type:")
    for k, v in audit["by_type"].items():
        lines.append(f"- {k}: {v}")
    lines.append("\nBy project:")
    for k, v in audit["by_project"].items():
        lines.append(f"- {k}: {v}")
    if audit.get("coverage_gap_projects"):
        lines.append("\nCoverage gap projects:")
        for p in audit["coverage_gap_projects"]:
            lines.append(f"- {p}")
    if audit.get("promotion_candidates"):
        lines.append("\nPromotion candidates:")
        for d in audit["promotion_candidates"][:10]:
            lines.append(f"- {d['discovery_id'][:8]}... | {d['project'] or '-'} | {d['category']} | {d['severity']} | {d['content_preview']}")
    if audit.get("stale_candidates"):
        lines.append("\nStale review candidates:")
        for r in audit["stale_candidates"][:10]:
            lines.append(f"- {r['record_id'][:8]}... | {r['record_type']} | {r['project'] or '-'} | {r['title']}")
    return "\n".join(lines)


@mcp.tool()
def suggest_semantic_promotions(project: str = "", limit: int = 20) -> str:
    """Suggest confirmed discoveries that should likely be promoted into semantic memory."""
    engine = get_engine()
    suggestions = engine.suggest_promotions(project=project or None, limit=limit)
    if not suggestions:
        return "No promotion suggestions found."
    lines = [f"Found {len(suggestions)} promotion suggestions:\n"]
    for s in suggestions:
        lines.append(f"- {s['discovery_id'][:8]}... | project={s['project'] or '-'} | category={s['category']} | severity={s['severity']} | suggested={s['suggested_record_type']} | {s['title']}")
    return "\n".join(lines)


@mcp.tool()
def generate_project_overview(project: str) -> str:
    """Generate and store a first-pass semantic project overview."""
    engine = get_engine()
    record = engine.generate_project_overview(project)
    return f"Generated project overview: {record.title} (ID: {record.record_id})"


@mcp.tool()
def bootstrap_project_semantic_coverage(project: str) -> str:
    """Bootstrap semantic coverage for a project with an overview and promotion suggestions."""
    engine = get_engine()
    result = engine.bootstrap_project_semantic_coverage(project)
    lines = [f"Bootstrapped semantic coverage for {project}:"]
    lines.append(f"- Overview record: {result['overview_title']} ({result['overview_record_id']})")
    lines.append(f"- Suggested promotions: {len(result['suggested_promotions'])}")
    for s in result['suggested_promotions'][:10]:
        lines.append(f"  - {s['discovery_id'][:8]}... | {s['suggested_record_type']} | {s['title']}")
    return "\n".join(lines)


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
        source = r.metadata.get("file_path") or r.metadata.get("source_file", "?")
        lines.append(f"- [{source}] {r.content[:200]}")

    lines.append("\n## Code")
    for r in code.results:
        source = r.metadata.get("file_path") or r.metadata.get("source_file", "?")
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

    For directories: recursively walks all files, ingesting both markdown (.md)
    files into the documents collection and code files into the codebase collection.
    For single files: auto-detects collection based on file extension.

    Args:
        path: Path to file or directory to ingest
        project_name: Project name (used for code files; auto-detected from dirname if empty)
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


@mcp.tool()
def search_conversations(
    query: str,
    project: str = "",
    source_agent: str = "",
    after: str = "",
    before: str = "",
    n_results: int = 10
) -> str:
    """Search past conversations semantically. Finds relevant exchanges from
    Claude Code, Codex, and Gemini conversation history.

    Args:
        query: Search query text
        project: Filter by project name (optional)
        source_agent: Filter by agent: claude, codex, or gemini (optional)
        after: Only include results after this date, YYYY-MM-DD (optional)
        before: Only include results before this date, YYYY-MM-DD (optional)
        n_results: Number of results to return (default: 10)
    """
    engine = get_engine()

    response = engine.search_conversations(
        query=query,
        project=project or None,
        source_agent=source_agent or None,
        after=after or None,
        before=before or None,
        n_results=n_results,
    )

    if not response.results:
        return "No matching conversations found."

    lines = [f"Found {response.total_results} results ({response.search_time_ms}ms):\n"]
    for i, r in enumerate(response.results, 1):
        meta = r.metadata
        session = meta.get("session_id", "?")[:12]
        ts = meta.get("timestamp", "?")[:10]
        agent = meta.get("source_agent", "?")
        proj = meta.get("project", "?")
        tools = meta.get("tool_names", "")
        category = meta.get("category", "")
        lines.append(f"--- Result {i} (score: {r.score}) ---")
        lines.append(f"Session: {session}... | Date: {ts} | Agent: {agent} | Project: {proj}")
        if category:
            lines.append(f"Category: {category}")
        if tools:
            lines.append(f"Tools: {tools}")
        lines.append(f"{r.content[:600]}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def read_conversation(
    session_id: str,
    start_line: int = 0,
    end_line: int = 0
) -> str:
    """Read a specific conversation by session ID. Returns formatted markdown
    with human messages, assistant responses, and tool usage.

    Args:
        session_id: Session UUID to look up
        start_line: Starting line number, 1-indexed (optional, default: beginning)
        end_line: Ending line number, 1-indexed (optional, default: end of file)
    """
    engine = get_engine()
    return engine.get_conversation(
        session_id=session_id,
        start_line=start_line or None,
        end_line=end_line or None,
    )


def main():
    """Run the MCP server."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    transport = os.environ.get("KNOWLEDGEFORGE_MCP_TRANSPORT", "stdio").strip().lower()
    network_transports = {"sse", "streamable-http"}

    if transport in network_transports:
        # The FastMCP settings object is created at import time, so update it directly.
        host = (
            os.environ.get("KNOWLEDGEFORGE_MCP_HOST")
            or os.environ.get("FASTMCP_HOST")
            or "127.0.0.1"  # Bind to localhost; use auth gateway for remote access
        )
        raw_port = (
            os.environ.get("KNOWLEDGEFORGE_MCP_PORT")
            or os.environ.get("FASTMCP_PORT")
            or "8743"
        )
        try:
            port = int(raw_port)
        except ValueError:
            logger.warning("Invalid MCP port '%s', using 8743", raw_port)
            port = 8743

        mcp.settings.host = host
        mcp.settings.port = port

        mount_path = os.environ.get("KNOWLEDGEFORGE_MCP_MOUNT_PATH") or None
        mcp.run(transport=transport, mount_path=mount_path)
        return

    if transport != "stdio":
        logger.warning("Unknown transport '%s', falling back to stdio", transport)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
