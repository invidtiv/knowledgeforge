"""Typer CLI for KnowledgeForge administration."""
import json
import logging
import os
import sys
import typer
from pathlib import Path
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
memory_app = typer.Typer(help="Manage extracted structured memory cards")
queue_app = typer.Typer(help="Deterministic ingestion queue")
config_app = typer.Typer(help="Configuration management")
historical_app = typer.Typer(help="Historical session ingestion tooling")

app.add_typer(index_app, name="index")
app.add_typer(discoveries_app, name="discoveries")
app.add_typer(semantic_app, name="semantic")
app.add_typer(memory_app, name="memory")
app.add_typer(queue_app, name="queue")
app.add_typer(config_app, name="config")
app.add_typer(historical_app, name="historical")


def _configure_logging() -> None:
    """Configure CLI logging once for systemd and interactive runs."""
    level_name = os.getenv("KNOWLEDGEFORGE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )
    else:
        logging.getLogger().setLevel(level)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _get_engine():
    """Lazy engine initialization."""
    from knowledgeforge.core.engine import KnowledgeForgeEngine
    config = KnowledgeForgeConfig.load_config()
    return KnowledgeForgeEngine(config)


# === HISTORICAL INGESTION ===

@historical_app.command("inventory")
def historical_inventory(
    output: str = typer.Argument(..., help="Output JSON path"),
    host: str = typer.Option("local", "--host", help="Inventory host label"),
    source: list[str] = typer.Option([], "--source", help="agent=path=adapter source spec"),
):
    """Write a metadata-only inventory of historical session sources."""
    from knowledgeforge.ingestion.source_inventory import SourceSpec, write_inventory

    sources = [_parse_historical_source_spec(item) for item in source]
    payload = write_inventory(sources, output, host=host)
    console.print_json(data=payload)


@historical_app.command("batch-prompts")
def historical_batch_prompts(
    source_dir: str = typer.Argument(..., help="Directory containing historical JSONL sessions"),
    output_dir: str = typer.Argument(..., help="Directory for generated prompt files and manifest"),
    limit: int = typer.Option(20, "--limit", help="Maximum sessions to include"),
    max_chars: int = typer.Option(60000, "--max-chars", help="Maximum conversation chars per prompt"),
):
    """Write extraction prompts for a bounded batch of historical sessions."""
    from knowledgeforge.ingestion.batch_extraction import build_prompt_batch

    session_paths = _scan_historical_jsonl_sessions(Path(source_dir))
    payload = build_prompt_batch(
        session_paths,
        output_dir=output_dir,
        limit=limit,
        max_chars=max_chars,
    )
    console.print_json(data=payload)


@historical_app.command("extract-json")
def historical_extract_json(
    source_path: str = typer.Argument(..., help="Historical source path"),
    output: str = typer.Argument(..., help="Output extraction JSON path"),
    agent: str = typer.Option(..., "--agent", help="Source agent label, e.g. claude/codex/windsurf/antigravity"),
    adapter_status: str = typer.Option("jsonl-supported", "--adapter-status", help="Adapter status or unsupported reason"),
    limit_sessions: int = typer.Option(0, "--limit-sessions", help="Maximum JSONL sessions to scan; 0 means all"),
    max_cards: int = typer.Option(40, "--max-cards", help="Maximum atomic cards to emit"),
    max_sentence_chars: int = typer.Option(360, "--max-sentence-chars", help="Maximum chars copied into one card body"),
):
    """Write a no-upload atomic-card JSON artifact for one historical source."""
    from knowledgeforge.ingestion.historical_json import (
        HistoricalSource,
        write_source_extraction_json,
    )

    payload = write_source_extraction_json(
        HistoricalSource(agent=agent, path=source_path, adapter_status=adapter_status),
        output_path=output,
        limit_sessions=limit_sessions,
        max_cards=max_cards,
        max_sentence_chars=max_sentence_chars,
    )
    console.print_json(data=payload)


@historical_app.command("codex-sqlite-export")
def historical_codex_sqlite_export(
    db_path: str = typer.Argument(..., help="Codex SQLite database path"),
    output_dir: str = typer.Argument(..., help="Directory for grouped JSONL export"),
    limit_threads: int = typer.Option(20, "--limit-threads", help="Maximum threads to export"),
    schema_only: bool = typer.Option(False, "--schema-only", help="Print schema JSON without exporting rows"),
):
    """Inspect a Codex SQLite database or export grouped conversation JSONL."""
    from knowledgeforge.ingestion.codex_sqlite import export_codex_logs, inspect_sqlite_schema

    if schema_only:
        payload = inspect_sqlite_schema(db_path)
    else:
        payload = export_codex_logs(db_path, output_dir, limit_threads=limit_threads)
    console.print_json(data=payload)


def _parse_historical_source_spec(value: str):
    from knowledgeforge.ingestion.source_inventory import SourceSpec

    agent, separator, remainder = value.partition("=")
    path, final_separator, adapter_status = remainder.rpartition("=")
    if not separator or not final_separator or not agent or not path or not adapter_status:
        raise typer.BadParameter("--source must use agent=path=adapter format")
    return SourceSpec(agent=agent, path=path, adapter_status=adapter_status)


def _scan_historical_jsonl_sessions(source_dir: Path) -> list[str]:
    root = source_dir.expanduser()
    if not root.exists():
        raise typer.BadParameter(f"Source directory does not exist: {source_dir}")

    session_paths: list[str] = []
    for path in sorted(root.rglob("*.jsonl"), key=lambda item: str(item).lower()):
        if "subagents" in str(path).lower():
            continue
        if path.name.startswith("agent-"):
            continue
        session_paths.append(str(path))
    return session_paths


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
    show_commands: bool = typer.Option(False, "--commands", help="Show copy-paste promote commands"),
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

    if show_commands:
        console.print("\n[bold]Copy-paste commands:[/bold]\n")
        for s in suggestions:
            console.print(f"[dim]# {s['title']}[/dim]")
            console.print(f"{s['promote_command']}\n")


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


@semantic_app.command("mark-reviewed")
def semantic_mark_reviewed(
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    record_id: str = typer.Argument(..., help="Semantic record ID"),
):
    """Mark a semantic record as reviewed (touch reviewed_at timestamp)."""
    engine = _get_engine()
    updated = engine.mark_semantic_reviewed(record_id, record_type)
    if not updated:
        raise typer.BadParameter("Semantic record not found")
    console.print(f"[green]Marked {record_type} {record_id[:8]} as reviewed[/green]")


@semantic_app.command("review-stale")
def semantic_review_stale(
    stale_days: int = typer.Option(30, "--days", help="Days since last review to consider stale"),
    project: str = typer.Option("", "--project", help="Filter by project"),
    show_commands: bool = typer.Option(False, "--commands", help="Show actionable commands"),
):
    """List stale semantic records that need review."""
    engine = _get_engine()
    stale = engine.get_stale_records(stale_days=stale_days, project=project or None)

    if not stale:
        console.print(f"[green]No stale records found (threshold: {stale_days} days)[/green]")
        return

    table = Table(title=f"Stale Records (>{stale_days} days without review)")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Project")
    table.add_column("Title", max_width=50)
    table.add_column("Created", style="dim")
    table.add_column("Last Reviewed", style="yellow")

    for r in stale:
        table.add_row(
            r.record_id[:8],
            r.record_type,
            r.project or "-",
            r.title,
            r.created_at[:10] if r.created_at else "?",
            r.reviewed_at[:10] if r.reviewed_at else "never",
        )
    console.print(table)

    if show_commands:
        console.print("\n[bold]Actionable commands:[/bold]\n")
        for r in stale:
            console.print(f"[dim]# {r.title}[/dim]")
            console.print(f'knowledgeforge semantic mark-reviewed {r.record_type} "{r.record_id}"')
            console.print(f'knowledgeforge semantic archive {r.record_type} "{r.record_id}"')
            console.print()


@semantic_app.command("replace")
def semantic_replace(
    record_type: str = typer.Argument(..., help="fact | runbook | project_overview"),
    old_record_id: str = typer.Argument(..., help="ID of the record to supersede"),
    title: str = typer.Argument(..., help="Title for the new record"),
    content: str = typer.Argument(..., help="Content for the new record"),
    project: str = typer.Option("", "--project", help="Project (defaults to old record's project)"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
    confidence: float = typer.Option(0.9, "--confidence", help="Confidence score 0-1"),
):
    """Create a new record and supersede an old one in a single step."""
    engine = _get_engine()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    new_record, old_ok = engine.replace_semantic_record(
        old_record_id=old_record_id,
        record_type=record_type,
        new_title=title,
        new_content=content,
        new_project=project or "",
        new_tags=tag_list,
        new_confidence=confidence,
    )
    console.print(Panel(
        f"New ID: {new_record.record_id}\n"
        f"Type: {new_record.record_type}\n"
        f"Title: {new_record.title}\n"
        f"Project: {new_record.project or '-'}\n"
        f"Old record superseded: {'yes' if old_ok else 'FAILED (not found)'}",
        title="Semantic Record Replaced"
    ))


@semantic_app.command("audit")
def semantic_audit(
    show_commands: bool = typer.Option(False, "--commands", help="Show actionable commands for each issue"),
):
    """Show lifecycle, linkback, stale-review, and coverage audit data for semantic memory."""
    engine = _get_engine()
    audit = engine.get_semantic_audit()
    summary = audit["summary"]

    # Color-coded summary with health indicators
    health_lines = []
    health_lines.append(f"Active records: {summary['active_records']}")
    health_lines.append(f"Archived records: {summary['archived_records']}")
    health_lines.append(f"Superseded records: {summary['superseded_records']}")
    health_lines.append(f"Records with discovery linkback: {summary['records_with_discovery_linkback']}")
    health_lines.append(f"Discoveries with semantic linkback: {summary['discoveries_with_semantic_linkback']}")

    # Flag issues with warning colors
    missing_review = summary['records_missing_reviewed_at']
    if missing_review > 0:
        health_lines.append(f"[yellow]Records missing reviewed_at: {missing_review}[/yellow]")
    else:
        health_lines.append(f"[green]Records missing reviewed_at: 0[/green]")

    unpromo = summary['confirmed_discoveries_not_promoted']
    if unpromo > 0:
        health_lines.append(f"[yellow]Confirmed discoveries not promoted: {unpromo}[/yellow]")
    else:
        health_lines.append(f"[green]Confirmed discoveries not promoted: 0[/green]")

    gaps = summary['coverage_gap_projects']
    if gaps > 0:
        health_lines.append(f"[yellow]Coverage gap projects: {gaps}[/yellow]")
    else:
        health_lines.append(f"[green]Coverage gap projects: 0[/green]")

    orphaned = summary['superseded_without_replacement']
    if orphaned > 0:
        health_lines.append(f"[red]Superseded without replacement: {orphaned}[/red]")
    else:
        health_lines.append(f"[green]Superseded without replacement: 0[/green]")

    console.print(Panel("\n".join(health_lines), title="Semantic Memory Audit"))

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
        gap_table = Table(title="Coverage Gap Projects (indexed code but no semantic records)")
        gap_table.add_column("Project", style="yellow")
        if show_commands:
            gap_table.add_column("Bootstrap Command", style="dim")
        for p in audit["coverage_gap_projects"]:
            if show_commands:
                gap_table.add_row(p, f'knowledgeforge semantic bootstrap-project "{p}"')
            else:
                gap_table.add_row(p)
        console.print(gap_table)

    if audit.get("promotion_candidates"):
        promo_table = Table(title="Promotion Candidates (confirmed discoveries not yet promoted)")
        promo_table.add_column("Discovery ID", style="dim")
        promo_table.add_column("Project")
        promo_table.add_column("Category")
        promo_table.add_column("Severity")
        promo_table.add_column("Preview", max_width=50)
        for d in audit["promotion_candidates"]:
            promo_table.add_row(d["discovery_id"][:8], d["project"] or "-", d["category"], d["severity"], d["content_preview"])
        console.print(promo_table)
        if show_commands:
            console.print("\n[bold]Run for detailed promotion commands:[/bold]")
            console.print("knowledgeforge semantic suggest-promotions --commands\n")

    if audit.get("stale_candidates"):
        stale_table = Table(title="Stale Review Candidates (never reviewed)")
        stale_table.add_column("Record ID", style="dim")
        stale_table.add_column("Type")
        stale_table.add_column("Project")
        stale_table.add_column("Title", max_width=40)
        stale_table.add_column("Created", style="dim")
        stale_table.add_column("Age (days)", justify="right", style="yellow")
        for r in audit["stale_candidates"]:
            stale_table.add_row(
                r["record_id"][:8],
                r["record_type"],
                r["project"] or "-",
                r["title"],
                r.get("created_at", ""),
                str(r.get("age_days", "?")),
            )
        console.print(stale_table)
        if show_commands:
            console.print("\n[bold]Run for detailed stale review commands:[/bold]")
            console.print("knowledgeforge semantic review-stale --commands\n")


# === STRUCTURED MEMORY CARDS ===

@memory_app.command("create")
def memory_create(
    memory_type: str = typer.Argument(..., help="decision | constraint | failed_attempt | resolution | todo | ..."),
    title: str = typer.Argument(..., help="Atomic memory title"),
    body: str = typer.Argument(..., help="Concise durable memory body"),
    project: str = typer.Option("unknown", "--project", help="Associated project"),
    why: str = typer.Option("", "--why", help="Reasoning or context behind the memory"),
    status: str = typer.Option("active_unverified", "--status", help="Lifecycle status"),
    confidence: str = typer.Option("medium", "--confidence", help="high | medium | low"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
    source_conversation: str = typer.Option("", "--source-conversation", help="Conversation title or session"),
    source_date: str = typer.Option("", "--source-date", help="Source date, ideally YYYY-MM-DD"),
    current_truth: bool = typer.Option(False, "--current-truth", help="Mark as confirmed current truth"),
    needs_repo_confirmation: bool = typer.Option(True, "--needs-repo-confirmation/--no-repo-confirmation"),
):
    """Create one structured memory card."""
    from knowledgeforge.core.models import MemoryCard

    engine = _get_engine()
    card = MemoryCard(
        type=memory_type,
        project=project,
        title=title,
        body=body,
        why=why,
        status=status,
        confidence=confidence,
        source_conversation=source_conversation,
        source_date=source_date,
        current_truth=current_truth,
        needs_repo_confirmation=needs_repo_confirmation,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    stored = engine.store_memory_card(card)
    console.print(Panel(
        f"ID: {stored.card_id}\n"
        f"Type: {stored.type}\n"
        f"Project: {stored.project}\n"
        f"Status: {stored.status}\n"
        f"Current truth: {stored.current_truth}\n"
        f"Confidence: {stored.confidence}",
        title=f"Memory card created: {stored.title}",
    ))


@memory_app.command("list")
def memory_list(
    project: str = typer.Option("", "--project", help="Filter by project"),
    memory_type: str = typer.Option("", "--type", help="Filter by memory type"),
    status: str = typer.Option("", "--status", help="Filter by lifecycle status"),
    current_truth: bool = typer.Option(False, "--current-truth", help="Only show current-truth cards"),
    limit: int = typer.Option(100, "--limit", help="Max records"),
):
    """List structured memory cards."""
    engine = _get_engine()
    cards = engine.list_memory_cards(
        project=project or None,
        memory_type=memory_type or None,
        status=status or None,
        current_truth=True if current_truth else None,
        limit=limit,
    )

    table = Table(title="Structured Memory Cards")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Truth")
    table.add_column("Confidence")
    table.add_column("Title", max_width=60)
    for card in cards:
        table.add_row(
            card.card_id[:12],
            card.type,
            card.project,
            card.status,
            "yes" if card.current_truth else "no",
            card.confidence,
            card.title,
        )
    console.print(table)


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="Search query"),
    project: str = typer.Option("", "--project", help="Filter by project"),
    memory_type: str = typer.Option("", "--type", help="Filter by memory type"),
    limit: int = typer.Option(8, "--limit", help="Max results"),
    min_score: float = typer.Option(0.25, "--min-score", help="Minimum score threshold"),
):
    """Search extracted memory cards only."""
    engine = _get_engine()
    response = engine.search_memory_cards(
        query=query,
        project=project or None,
        memory_type=memory_type or None,
        max_results=limit,
        min_score_threshold=min_score,
    )

    console.print(f"\n[bold]Found {response.total_results} memory results ({response.search_time_ms}ms)[/bold]\n")
    for i, result in enumerate(response.results, 1):
        meta = result.metadata or {}
        table = Table(title=f"Memory Result {i}", show_lines=True)
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        table.add_row("Score", f"{result.score:.4f}")
        table.add_row("Type", str(meta.get("type", "")))
        table.add_row("Title", str(meta.get("title", "")))
        table.add_row("Project", str(meta.get("project", "")))
        table.add_row("Status", str(meta.get("status", "")))
        table.add_row("Current Truth", str(meta.get("current_truth", False)))
        table.add_row("Content", result.content[:700])
        console.print(table)


@memory_app.command("status")
def memory_status(
    card_id: str = typer.Argument(..., help="Memory card ID"),
    status: str = typer.Argument(..., help="New lifecycle status"),
    current_truth: bool = typer.Option(False, "--current-truth", help="Also promote to current truth"),
):
    """Update a memory card lifecycle status."""
    engine = _get_engine()
    card = engine.update_memory_card_status(
        card_id,
        status,
        current_truth=True if current_truth else None,
    )
    if not card:
        raise typer.BadParameter("Memory card not found")
    console.print(f"[green]Updated {card.card_id}: status={card.status}, current_truth={card.current_truth}[/green]")


@memory_app.command("prompt-file")
def memory_prompt_file(
    path: str = typer.Argument(..., help="Conversation JSONL file"),
    title: str = typer.Option("", "--title", help="Conversation title override"),
    max_chars: int = typer.Option(60000, "--max-chars", help="Max conversation chars included"),
):
    """Render the extraction prompt for a conversation file."""
    from knowledgeforge.ingestion.conversations import parse_jsonl_file
    from knowledgeforge.ingestion.memory_extraction import build_conversation_extraction_prompt

    exchanges = parse_jsonl_file(path)
    prompt = build_conversation_extraction_prompt(
        exchanges,
        title=title or Path(path).stem,
        max_chars=max_chars,
    )
    console.print(prompt)


@memory_app.command("import-json")
def memory_import_json(
    path: str = typer.Argument(..., help="Extraction JSON file"),
    source_path: str = typer.Option("", "--source-path", help="Original conversation path"),
    no_summary_card: bool = typer.Option(False, "--no-summary-card", help="Do not create the conversation summary card"),
):
    """Import extraction JSON produced from the past-conversation prompt."""
    from knowledgeforge.ingestion.memory_extraction import (
        memory_cards_from_extraction,
        parse_extraction_json,
    )

    engine = _get_engine()
    raw = Path(path).read_text(encoding="utf-8")
    payload = parse_extraction_json(raw)
    cards = memory_cards_from_extraction(
        payload,
        source_path=source_path,
        include_summary_card=not no_summary_card,
    )
    stored = engine.import_memory_cards(cards)
    console.print(f"[green]Imported {len(stored)} memory cards[/green]")


@memory_app.command("audit")
def memory_audit():
    """Show structured memory card counts by status/type/project."""
    engine = _get_engine()
    audit = engine.get_memory_audit()
    console.print(Panel(
        f"Total cards: {audit['total_cards']}\n"
        f"Current truth cards: {audit['current_truth_cards']}\n"
        f"Needs repo confirmation: {audit['needs_repo_confirmation']}\n"
        f"Registry: {audit['registry_path']}",
        title="Structured Memory Audit",
    ))

    for title, key in [
        ("By Status", "by_status"),
        ("By Type", "by_type"),
        ("By Project", "by_project"),
    ]:
        table = Table(title=title)
        table.add_column("Key", style="cyan")
        table.add_column("Count", justify="right")
        for item, count in audit[key].items():
            table.add_row(item, str(count))
        console.print(table)


# === INGESTION QUEUE ===

@queue_app.command("run-once")
def queue_run_once():
    """Run one deterministic project-ingest queue step."""
    from knowledgeforge.ingest_queue import run_once
    result = run_once()
    console.print_json(data=result)


@queue_app.command("status")
def queue_status():
    """Show operator-friendly queue status dashboard."""
    import time as _time
    from knowledgeforge.ingest_queue import load_state, _state_path

    config = KnowledgeForgeConfig.load_config()
    state_path = _state_path(config)

    if not state_path.exists():
        console.print("[yellow]No queue state file found. Run 'knowledgeforge queue run-once' to initialise.[/yellow]")
        raise typer.Exit(0)

    state = load_state(config)
    projects = state.get("projects", [])

    if not projects:
        console.print("[yellow]Queue exists but contains no projects.[/yellow]")
        raise typer.Exit(0)

    # --- Counts ---
    counts = {"pending": 0, "running": 0, "retry": 0, "done": 0}
    for p in projects:
        s = p.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1

    total = len(projects)
    pct_done = (counts["done"] / total * 100) if total else 0

    def _ts(epoch):
        if not epoch:
            return "-"
        return _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(epoch))

    def _ago(epoch):
        if not epoch:
            return ""
        delta = _time.time() - epoch
        if delta < 60:
            return f"({int(delta)}s ago)"
        if delta < 3600:
            return f"({int(delta / 60)}m ago)"
        if delta < 86400:
            return f"({delta / 3600:.1f}h ago)"
        return f"({delta / 86400:.1f}d ago)"

    summary_lines = (
        f"[bold]Total projects:[/bold] {total}\n"
        f"[green]Done:[/green]    {counts['done']}\n"
        f"[yellow]Pending:[/yellow] {counts['pending']}\n"
        f"[blue]Running:[/blue] {counts['running']}\n"
        f"[red]Retry:[/red]   {counts['retry']}\n"
        f"\n[bold]Progress:[/bold] {pct_done:.0f}% complete"
    )
    console.print(Panel(summary_lines, title="Ingestion Queue Status"))

    # --- Per-project table ---
    table = Table(title="Project Details")
    table.add_column("Project", style="cyan")
    table.add_column("Status")
    table.add_column("Attempts", justify="right")
    table.add_column("Progress")
    table.add_column("Last Attempt")
    table.add_column("Last Success")
    table.add_column("Error", max_width=50)

    status_style = {"done": "green", "pending": "yellow", "retry": "red", "running": "blue"}

    for p in sorted(projects, key=lambda x: ({"running": 0, "retry": 1, "pending": 2, "done": 3}.get(x["status"], 4), x["name"])):
        st = p.get("status", "pending")
        style = status_style.get(st, "white")
        table.add_row(
            p["name"],
            f"[{style}]{st}[/{style}]",
            str(p.get("attempts", 0)),
            f"{p.get('phase', 'markdown')} {p.get('markdown_index', 0)}/{p.get('markdown_total', 0)} md, {p.get('code_index', 0)}/{p.get('code_total', 0)} code",
            f"{_ts(p.get('last_attempt_at'))} {_ago(p.get('last_attempt_at'))}",
            f"{_ts(p.get('last_success_at'))} {_ago(p.get('last_success_at'))}",
            (p.get("last_error") or "")[:50],
        )

    console.print(table)

    # --- Last success / last attempt globally ---
    last_success = max((p.get("last_success_at") or 0 for p in projects), default=0)
    last_attempt = max((p.get("last_attempt_at") or 0 for p in projects), default=0)

    footer_lines = (
        f"[bold]Last successful ingest:[/bold] {_ts(last_success)} {_ago(last_success)}\n"
        f"[bold]Last attempt:[/bold]           {_ts(last_attempt)} {_ago(last_attempt)}\n"
        f"[bold]Queue file:[/bold]             {state_path}"
    )
    console.print(Panel(footer_lines, title="Timeline"))


@queue_app.command("retry-audit")
def queue_retry_audit():
    """Show top retry causes and high-attempt projects."""
    import time as _time
    from collections import Counter
    from knowledgeforge.ingest_queue import load_state, _state_path

    config = KnowledgeForgeConfig.load_config()
    state_path = _state_path(config)

    if not state_path.exists():
        console.print("[yellow]No queue state file found.[/yellow]")
        raise typer.Exit(0)

    state = load_state(config)
    projects = state.get("projects", [])

    # Retry / failed projects
    retries = [p for p in projects if p.get("status") == "retry"]
    high_attempt = [p for p in projects if p.get("attempts", 0) >= 3]

    if not retries and not high_attempt:
        console.print("[green]No retry projects and no high-attempt projects. Queue is healthy.[/green]")
        return

    # --- Error frequency ---
    error_counter = Counter()
    for p in projects:
        err = (p.get("last_error") or "").strip()
        if err:
            # Normalise long tracebacks to first line
            first_line = err.split(";")[0].split("\n")[0][:120]
            error_counter[first_line] += 1

    if error_counter:
        err_table = Table(title="Top Retry Causes")
        err_table.add_column("Count", justify="right", style="red")
        err_table.add_column("Error")
        for err_msg, count in error_counter.most_common(10):
            err_table.add_row(str(count), err_msg)
        console.print(err_table)

    # --- High-attempt projects ---
    if high_attempt:
        ha_table = Table(title="High-Attempt Projects (>=3 attempts)")
        ha_table.add_column("Project", style="cyan")
        ha_table.add_column("Status")
        ha_table.add_column("Attempts", justify="right", style="red")
        ha_table.add_column("Last Error", max_width=70)

        for p in sorted(high_attempt, key=lambda x: -x.get("attempts", 0)):
            ha_table.add_row(
                p["name"],
                p.get("status", "?"),
                str(p.get("attempts", 0)),
                (p.get("last_error") or "-")[:70],
            )
        console.print(ha_table)

    # --- Summary ---
    console.print(f"\n[bold]Retry projects:[/bold] {len(retries)} / {len(projects)}")
    console.print(f"[bold]High-attempt projects:[/bold] {len(high_attempt)} / {len(projects)}")


@queue_app.command("reset")
def queue_reset(
    project_name: str = typer.Argument("", help="Reset a specific project (or all if omitted)"),
    status_filter: str = typer.Option("retry", "--status", help="Only reset projects with this status"),
):
    """Reset retry/failed projects back to pending so they are re-queued."""
    from knowledgeforge.ingest_queue import load_state, save_state, _state_path

    config = KnowledgeForgeConfig.load_config()
    state_path = _state_path(config)

    if not state_path.exists():
        console.print("[yellow]No queue state file found.[/yellow]")
        raise typer.Exit(0)

    state = load_state(config)
    count = 0

    for p in state["projects"]:
        if project_name and p["name"] != project_name:
            continue
        if p["status"] == status_filter:
            p["status"] = "pending"
            p["last_error"] = ""
            p["phase"] = "markdown"
            p["markdown_index"] = 0
            p["code_index"] = 0
            p["markdown_total"] = 0
            p["code_total"] = 0
            count += 1

    if count:
        save_state(config, state)
        console.print(f"[green]Reset {count} project(s) from '{status_filter}' to 'pending'.[/green]")
    else:
        console.print(f"[yellow]No projects matched (name='{project_name or '*'}', status='{status_filter}').[/yellow]")


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
    table.add_row("Embedding Provider", s.get("embedding_provider", ""))
    table.add_row("Embedding Model", s.get("embedding_model", ""))
    table.add_row("Embedding Dimension", str(s.get("embedding_dimension", "")))
    table.add_row("Memory Registry Cards", str(s.get("memory_registry_cards", "")))
    table.add_row("Memory Registry", s.get("memory_registry_path", ""))
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
    _configure_logging()
    app()
