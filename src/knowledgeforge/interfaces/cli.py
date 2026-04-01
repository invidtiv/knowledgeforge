"""Typer CLI for KnowledgeForge administration."""
import logging
import typer
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

from knowledgeforge.config import KnowledgeForgeConfig

app = typer.Typer(name="knowledgeforge", help="KnowledgeForge - Universal RAG Knowledge System")
console = Console()

# Sub-commands
index_app = typer.Typer(help="Index/reindex content")
discoveries_app = typer.Typer(help="Manage discoveries")
semantic_app = typer.Typer(help="Manage curated semantic memory")
queue_app = typer.Typer(help="Deterministic ingestion queue")
config_app = typer.Typer(help="Configuration management")

app.add_typer(index_app, name="index")
app.add_typer(discoveries_app, name="discoveries")
app.add_typer(semantic_app, name="semantic")
app.add_typer(queue_app, name="queue")
app.add_typer(config_app, name="config")


def _get_engine():
    """Lazy engine initialization."""
    from knowledgeforge.core.engine import KnowledgeForgeEngine
    config = KnowledgeForgeConfig.load_config()
    return KnowledgeForgeEngine(config)


# === INDEX COMMANDS ===

@index_app.command("vault")
def index_vault(full: bool = typer.Option(False, "--full", help="Full reindex")):
    """Index/reindex the Obsidian vault."""
    with console.status("[bold green]Indexing vault..."):
        engine = _get_engine()
        result = engine.ingest_obsidian_vault(full_reindex=full)

    console.print(Panel(
        f"Files processed: {result.files_processed}\n"
        f"Files skipped: {result.files_skipped}\n"
        f"Chunks created: {result.chunks_created}\n"
        f"Duration: {result.duration_seconds}s",
        title="Vault Indexing Complete"
    ))
    if result.errors:
        for err in result.errors:
            console.print(f"[red]Error: {err}[/red]")


@index_app.command("project")
def index_project(
    path: str = typer.Argument("", help="Project directory path"),
    name: str = typer.Option("", "--name", help="Project name"),
    full: bool = typer.Option(False, "--full", help="Full reindex")
):
    """Index a code project by path or by configured project name."""
    import os
    engine = _get_engine()

    if path:
        project_name = name or os.path.basename(path)
        with console.status(f"[bold green]Indexing project {project_name}..."):
            result = engine.ingest_project(path, project_name, full_reindex=full)
    else:
        if not name:
            raise typer.BadParameter("Provide either a project path argument or --name for a configured project")
        project_name = name
        with console.status(f"[bold green]Indexing configured project {project_name}..."):
            result = engine.ingest_registered_project(project_name, full_reindex=full)

    console.print(Panel(
        f"Files processed: {result.files_processed}\n"
        f"Files skipped: {result.files_skipped}\n"
        f"Chunks created: {result.chunks_created}\n"
        f"Duration: {result.duration_seconds}s",
        title=f"Project '{project_name}' Indexing Complete"
    ))


@index_app.command("all")
def index_all(full: bool = typer.Option(False, "--full", help="Full reindex")):
    """Reindex everything (vault + all configured projects)."""
    engine = _get_engine()

    console.print("[bold]Indexing Obsidian vault...[/bold]")
    vault_result = engine.ingest_obsidian_vault(full_reindex=full)
    console.print(f"  Vault: {vault_result.files_processed} files, {vault_result.chunks_created} chunks")

    for proj in engine.config.project_paths:
        proj_name = proj.get("name", proj["path"])
        console.print(f"[bold]Indexing project: {proj_name}...[/bold]")
        result = engine.ingest_project(proj["path"], proj_name, full_reindex=full)
        console.print(f"  {proj_name}: {result.files_processed} files, {result.chunks_created} chunks")

    console.print("[bold green]All indexing complete![/bold green]")


# === SEARCH ===

@app.command("search")
def search(
    query: str = typer.Argument(..., help="Search query"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    collection: Optional[str] = typer.Option(None, "--collection", "-c"),
    n: int = typer.Option(5, "--results", "-n"),
):
    """Search the knowledge base."""
    engine = _get_engine()
    collections = [collection] if collection else None

    with console.status("[bold green]Searching..."):
        response = engine.search(query=query, project=project, collections=collections, n_results=n)

    console.print(f"\n[bold]Found {response.total_results} results ({response.search_time_ms}ms)[/bold]\n")

    for i, r in enumerate(response.results, 1):
        source = r.metadata.get("source_file", "unknown")
        table = Table(title=f"Result {i} — {r.collection}", show_lines=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        table.add_row("Score", f"{r.score:.4f}")
        table.add_row("Source", source)
        table.add_row("Content", r.content[:300])
        console.print(table)


# === DISCOVERIES ===

@discoveries_app.command("list")
def discoveries_list(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    unconfirmed: bool = typer.Option(False, "--unconfirmed"),
    category: Optional[str] = typer.Option(None, "--category"),
):
    """List discoveries."""
    engine = _get_engine()
    discoveries = engine.get_discoveries(project=project, unconfirmed_only=unconfirmed, category=category)

    if not discoveries:
        console.print("[yellow]No discoveries found.[/yellow]")
        return

    table = Table(title=f"Discoveries ({len(discoveries)} total)")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Category", style="cyan")
    table.add_column("Severity")
    table.add_column("Status")
    table.add_column("Content", max_width=60)

    for d in discoveries:
        status = "[green]Confirmed[/green]" if d.confirmed else "[yellow]Pending[/yellow]"
        table.add_row(d.discovery_id[:8], d.category, d.severity, status, d.content[:60])

    console.print(table)


@discoveries_app.command("review")
def discoveries_review():
    """Interactive review of unconfirmed discoveries."""
    engine = _get_engine()
    discoveries = engine.get_discoveries(unconfirmed_only=True)

    if not discoveries:
        console.print("[green]No unconfirmed discoveries to review![/green]")
        return

    console.print(f"[bold]Reviewing {len(discoveries)} unconfirmed discoveries...[/bold]\n")

    for i, d in enumerate(discoveries, 1):
        console.print(Panel(
            f"[bold]{d.content}[/bold]\n\n"
            f"Context: {d.context}\n"
            f"Project: {d.project} | Category: {d.category} | Severity: {d.severity}\n"
            f"Agent: {d.source_agent} | Date: {d.created_at[:10]}",
            title=f"Discovery {i}/{len(discoveries)} — {d.discovery_id[:8]}"
        ))

        action = typer.prompt("[c]onfirm, [r]eject, [s]kip", default="s")

        if action.lower() == "c":
            engine.confirm_discovery(d.discovery_id)
            console.print("[green]Confirmed![/green]")
        elif action.lower() == "r":
            engine.reject_discovery(d.discovery_id)
            console.print("[red]Rejected![/red]")
        else:
            console.print("[dim]Skipped[/dim]")
        console.print()


@discoveries_app.command("confirm")
def discoveries_confirm(discovery_id: str):
    """Confirm a specific discovery."""
    engine = _get_engine()
    d = engine.confirm_discovery(discovery_id)
    console.print(f"[green]Confirmed discovery {d.discovery_id[:8]}[/green]")


@discoveries_app.command("reject")
def discoveries_reject(discovery_id: str):
    """Reject a specific discovery."""
    engine = _get_engine()
    engine.reject_discovery(discovery_id)
    console.print(f"[red]Rejected discovery {discovery_id[:8]}[/red]")


@discoveries_app.command("promote")
def discoveries_promote():
    """Write confirmed discoveries back to Obsidian."""
    engine = _get_engine()
    count = engine.promote_discoveries_to_obsidian()
    console.print(f"[green]Promoted {count} discoveries to Obsidian vault[/green]")


# === SEMANTIC MEMORY ===

@semantic_app.command("create")
def semantic_create(
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    title: str = typer.Argument(..., help="Record title"),
    content: str = typer.Argument(..., help="Record content"),
    project: str = typer.Option("", "--project", help="Associated project"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
    confidence: float = typer.Option(0.9, "--confidence", help="Confidence score 0-1"),
):
    """Create a curated semantic record."""
    from knowledgeforge.core.models import SemanticRecord

    engine = _get_engine()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    record = SemanticRecord(
        title=title,
        content=content,
        project=project,
        record_type=record_type,
        tags=tag_list,
        confidence=confidence,
    )
    stored = engine.store_semantic_record(record)
    console.print(Panel(
        f"ID: {stored.record_id}\n"
        f"Type: {stored.record_type}\n"
        f"Project: {stored.project or '-'}\n"
        f"Trust: {stored.trust_level}\n"
        f"Status: {stored.status}\n"
        f"Confidence: {stored.confidence}",
        title=f"Semantic record created: {stored.title}"
    ))


@semantic_app.command("promote-discovery")
def semantic_promote_discovery(
    discovery_id: str = typer.Argument(..., help="Confirmed discovery ID"),
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    title: str = typer.Option("", "--title", help="Optional semantic title override"),
):
    """Promote a confirmed discovery into semantic memory."""
    engine = _get_engine()
    stored = engine.promote_discovery_to_semantic(discovery_id, record_type, title)
    console.print(Panel(
        f"ID: {stored.record_id}\n"
        f"Type: {stored.record_type}\n"
        f"Project: {stored.project or '-'}\n"
        f"Trust: {stored.trust_level}\n"
        f"Status: {stored.status}\n"
        f"Confidence: {stored.confidence}",
        title=f"Discovery promoted to semantic memory: {stored.title}"
    ))


@semantic_app.command("list")
def semantic_list(
    record_type: str = typer.Option("", "--type", help="fact | runbook | project_overview"),
    project: str = typer.Option("", "--project", help="Filter by project"),
    status: str = typer.Option("active", "--status", help="active | archived | superseded | expired"),
    limit: int = typer.Option(100, "--limit", help="Max records to show"),
):
    """List semantic records."""
    engine = _get_engine()
    records = engine.list_semantic_records(
        record_type=record_type or None,
        project=project or None,
        status=status,
        limit=limit,
    )

    table = Table(title="Semantic Records")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Title")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Trust")
    table.add_column("Confidence", justify="right")

    for r in records:
        table.add_row(
            r.record_id[:8],
            r.record_type,
            r.title,
            r.project or "-",
            r.status,
            r.trust_level,
            f"{r.confidence:.2f}",
        )

    console.print(table)


@semantic_app.command("search")
def semantic_search(
    query: str = typer.Argument(..., help="Search query"),
    record_type: str = typer.Option("", "--type", help="fact | runbook | project_overview"),
    project: str = typer.Option("", "--project", help="Filter by project"),
    limit: int = typer.Option(6, "--limit", help="Max results"),
    min_score: float = typer.Option(0.35, "--min-score", help="Minimum score threshold"),
):
    """Search semantic memory only."""
    engine = _get_engine()
    response = engine.search_semantic_records(
        query=query,
        record_type=record_type or None,
        project=project or None,
        max_results=limit,
        min_score_threshold=min_score,
    )

    console.print(f"\n[bold]Found {response.total_results} semantic results ({response.search_time_ms}ms)[/bold]\n")
    for i, r in enumerate(response.results, 1):
        meta = r.metadata or {}
        table = Table(title=f"Semantic Result {i}", show_lines=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        table.add_row("Score", f"{r.score:.4f}")
        table.add_row("Type", str(meta.get("record_type", "")))
        table.add_row("Title", str(meta.get("title", "")))
        table.add_row("Project", str(meta.get("project", "")))
        table.add_row("Trust", str(meta.get("trust_level", "")))
        table.add_row("Content", r.content[:300])
        console.print(table)


@semantic_app.command("archive")
def semantic_archive(
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    record_id: str = typer.Argument(..., help="Semantic record ID"),
):
    """Archive a semantic record."""
    engine = _get_engine()
    updated = engine.update_semantic_record_status(record_id, record_type, "archived")
    if not updated:
        raise typer.BadParameter("Semantic record not found")
    console.print(f"[yellow]Archived semantic record {record_id} ({record_type})[/yellow]")


@semantic_app.command("supersede")
def semantic_supersede(
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    record_id: str = typer.Argument(..., help="Semantic record ID"),
    superseded_by: str = typer.Argument(..., help="Replacement record ID"),
):
    """Mark a semantic record as superseded."""
    engine = _get_engine()
    updated = engine.update_semantic_record_status(record_id, record_type, "superseded", superseded_by)
    if not updated:
        raise typer.BadParameter("Semantic record not found")
    console.print(f"[yellow]Superseded semantic record {record_id} with {superseded_by}[/yellow]")


@semantic_app.command("suggest-promotions")
def semantic_suggest_promotions(
    project: str = typer.Option("", "--project", help="Optional project filter"),
    limit: int = typer.Option(20, "--limit", help="Max suggestions"),
):
    """Suggest confirmed discoveries that should be promoted next."""
    engine = _get_engine()
    suggestions = engine.suggest_promotions(project=project or None, limit=limit)
    if not suggestions:
        console.print("[yellow]No promotion suggestions found.[/yellow]")
        return

    table = Table(title="Promotion Suggestions")
    table.add_column("Discovery ID", style="dim")
    table.add_column("Project")
    table.add_column("Category")
    table.add_column("Severity")
    table.add_column("Suggested Type")
    table.add_column("Title")
    for s in suggestions:
        table.add_row(
            s["discovery_id"][:8],
            s["project"] or "-",
            s["category"],
            s["severity"],
            s["suggested_record_type"],
            s["title"],
        )
    console.print(table)


@semantic_app.command("generate-overview")
def semantic_generate_overview(
    project: str = typer.Argument(..., help="Project name"),
):
    """Generate and store a first-pass project overview."""
    engine = _get_engine()
    record = engine.generate_project_overview(project)
    console.print(f"[green]Generated project overview {record.record_id} for {project}: {record.title}[/green]")


@semantic_app.command("bootstrap-project")
def semantic_bootstrap_project(
    project: str = typer.Argument(..., help="Project name"),
):
    """Bootstrap semantic coverage for a project."""
    engine = _get_engine()
    result = engine.bootstrap_project_semantic_coverage(project)
    console.print(Panel(
        f"Project: {result['project']}\n"
        f"Overview record: {result['overview_title']} ({result['overview_record_id']})\n"
        f"Suggested promotions: {len(result['suggested_promotions'])}",
        title="Semantic Coverage Bootstrap"
    ))


@semantic_app.command("audit")
def semantic_audit():
    """Show lifecycle, linkback, stale-review, and coverage audit data for semantic memory."""
    engine = _get_engine()
    audit = engine.get_semantic_audit()
    summary = audit["summary"]

    console.print(Panel(
        f"Active records: {summary['active_records']}\n"
        f"Archived records: {summary['archived_records']}\n"
        f"Superseded records: {summary['superseded_records']}\n"
        f"Records with discovery linkback: {summary['records_with_discovery_linkback']}\n"
        f"Discoveries with semantic linkback: {summary['discoveries_with_semantic_linkback']}\n"
        f"Records missing reviewed_at: {summary['records_missing_reviewed_at']}\n"
        f"Confirmed discoveries not promoted: {summary['confirmed_discoveries_not_promoted']}\n"
        f"Coverage gap projects: {summary['coverage_gap_projects']}\n"
        f"Superseded without replacement: {summary['superseded_without_replacement']}",
        title="Semantic Memory Audit"
    ))

    by_type = Table(title="Semantic Records by Type")
    by_type.add_column("Type", style="cyan")
    by_type.add_column("Count", justify="right")
    for k, v in audit["by_type"].items():
        by_type.add_row(k, str(v))
    console.print(by_type)

    by_project = Table(title="Semantic Records by Project")
    by_project.add_column("Project", style="cyan")
    by_project.add_column("Count", justify="right")
    for k, v in audit["by_project"].items():
        by_project.add_row(k, str(v))
    console.print(by_project)

    if audit.get("coverage_gap_projects"):
        gap_table = Table(title="Coverage Gap Projects")
        gap_table.add_column("Project", style="yellow")
        for p in audit["coverage_gap_projects"]:
            gap_table.add_row(p)
        console.print(gap_table)

    if audit.get("promotion_candidates"):
        promo_table = Table(title="Promotion Candidates")
        promo_table.add_column("Discovery ID", style="dim")
        promo_table.add_column("Project")
        promo_table.add_column("Category")
        promo_table.add_column("Severity")
        promo_table.add_column("Preview")
        for d in audit["promotion_candidates"]:
            promo_table.add_row(d["discovery_id"][:8], d["project"] or "-", d["category"], d["severity"], d["content_preview"])
        console.print(promo_table)

    if audit.get("stale_candidates"):
        stale_table = Table(title="Stale Review Candidates")
        stale_table.add_column("Record ID", style="dim")
        stale_table.add_column("Type")
        stale_table.add_column("Project")
        stale_table.add_column("Title")
        for r in audit["stale_candidates"]:
            stale_table.add_row(r["record_id"][:8], r["record_type"], r["project"] or "-", r["title"])
        console.print(stale_table)


# === INGESTION QUEUE ===

@queue_app.command("run-once")
def queue_run_once():
    """Run one deterministic project-ingest queue step."""
    from knowledgeforge.ingest_queue import run_once
    result = run_once()
    console.print_json(data=result)


# === PROJECTS ===

@app.command("projects")
def projects():
    """List indexed/configured projects."""
    engine = _get_engine()
    project_list = engine.list_projects()

    table = Table(title="Indexed Projects")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Chunks", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Path")

    for p in project_list:
        table.add_row(
            p.name,
            p.type,
            p.status,
            str(p.total_chunks),
            str(p.file_count),
            str(p.error_count),
            p.path,
        )

    console.print(table)


@app.command("project-audit")
def project_audit():
    """Show ingest/health audit summary for projects."""
    engine = _get_engine()
    audit = engine.get_project_audit()
    summary = audit["summary"]

    summary_panel = Panel(
        f"Total projects: {summary['total_projects']}\n"
        f"Code projects: {summary['code_projects']}\n"
        f"Indexed code projects: {summary['indexed_code_projects']}\n"
        f"Registered but unindexed: {summary['registered_unindexed_code_projects']}\n"
        f"Errored projects: {summary['errored_projects']}\n"
        f"Next unindexed project: {summary['next_unindexed_project'] or '-'}",
        title="Project Ingest Audit"
    )
    console.print(summary_panel)

    table = Table(title="Project Details")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Chunks", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Errors", justify="right")

    for p in audit["projects"]:
        table.add_row(
            p["name"],
            p["type"],
            p.get("status", "registered"),
            str(p["total_chunks"]),
            str(p["file_count"]),
            str(p.get("error_count", 0)),
        )

    console.print(table)


# === STATS ===

@app.command("stats")
def stats():
    """Show system statistics."""
    engine = _get_engine()
    s = engine.get_stats()

    table = Table(title="KnowledgeForge Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total Chunks", str(s["total_chunks"]))
    for col, count in s.get("collections", {}).items():
        table.add_row(f"  {col}", str(count))
    table.add_row("Embedding Model", s.get("embedding_model", ""))
    table.add_row("Data Directory", s.get("data_dir", ""))

    console.print(table)


# === SERVE ===

@app.command("serve")
def serve(
    rest_only: bool = typer.Option(False, "--rest-only"),
    mcp_only: bool = typer.Option(False, "--mcp-only"),
):
    """Start MCP + REST servers."""
    if mcp_only:
        from knowledgeforge.interfaces.mcp_server import main as mcp_main
        mcp_main()
    elif rest_only:
        from knowledgeforge.interfaces.rest_api import main as rest_main
        rest_main()
    else:
        # Start REST API (MCP runs via stdio separately)
        from knowledgeforge.interfaces.rest_api import main as rest_main
        rest_main()


# === WATCH ===

@app.command("watch")
def watch(
    lightweight: bool = typer.Option(True, "--lightweight/--full", help="Use lightweight watcher (low memory) or full engine"),
    api_url: str = typer.Option("http://127.0.0.1:8742", "--api-url", help="REST API URL for lightweight mode"),
):
    """Start filesystem watcher for live sync."""
    config = KnowledgeForgeConfig.load_config()

    if lightweight:
        # Use lightweight watcher (REST API mode) - ~100MB memory
        from knowledgeforge.interfaces.watcher_lightweight import LightweightWatcher

        console.print("[bold green]Starting lightweight watcher (REST API mode)...[/bold green]")
        console.print(f"API URL: {api_url}")

        watcher = LightweightWatcher(config, api_url=api_url)

        # Test API connectivity
        import requests
        try:
            response = requests.get(f"{api_url}/api/v1/health", timeout=5)
            if response.status_code == 200:
                console.print("[green]✓ REST API connected[/green]")
            else:
                console.print(f"[yellow]⚠ REST API returned HTTP {response.status_code}[/yellow]")
        except requests.exceptions.ConnectionError:
            console.print(f"[red]✗ REST API unavailable at {api_url}[/red]")
            console.print("[yellow]Make sure the REST API is running: knowledgeforge serve --rest-only[/yellow]")
            raise typer.Exit(1)

        console.print("Press Ctrl+C to stop.\n")

        try:
            watcher.start()
            import time
            while watcher.is_running():
                time.sleep(1)
        except KeyboardInterrupt:
            watcher.stop()
            console.print("\n[yellow]Watcher stopped.[/yellow]")
    else:
        # Use full engine watcher (legacy mode) - ~7GB memory
        from knowledgeforge.ingestion.watcher import VaultWatcher
        engine = _get_engine()
        watcher = VaultWatcher(engine, config)

        console.print("[bold yellow]Starting full engine watcher (high memory usage)...[/bold yellow]")
        console.print("Press Ctrl+C to stop.")

        try:
            watcher.start()
            import time
            while watcher.is_running():
                time.sleep(1)
        except KeyboardInterrupt:
            watcher.stop()
            console.print("\n[yellow]Watcher stopped.[/yellow]")


# === CONFIG ===

@config_app.command("show")
def config_show():
    """Display current configuration."""
    config = KnowledgeForgeConfig.load_config()
    console.print(Panel(config.to_yaml(), title="Current Configuration"))


@config_app.command("init")
def config_init():
    """Create default config file."""
    import os
    config_dir = os.path.expanduser("~/.config/knowledgeforge")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.yaml")

    if os.path.exists(config_path):
        overwrite = typer.confirm(f"Config already exists at {config_path}. Overwrite?")
        if not overwrite:
            raise typer.Abort()

    config = KnowledgeForgeConfig()
    with open(config_path, "w") as f:
        f.write(config.to_yaml())

    console.print(f"[green]Config created at {config_path}[/green]")


if __name__ == "__main__":
    app()
