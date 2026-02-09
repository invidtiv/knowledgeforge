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
config_app = typer.Typer(help="Configuration management")

app.add_typer(index_app, name="index")
app.add_typer(discoveries_app, name="discoveries")
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
    path: str = typer.Argument(..., help="Project directory path"),
    name: str = typer.Option("", "--name", help="Project name"),
    full: bool = typer.Option(False, "--full", help="Full reindex")
):
    """Index a code project."""
    import os
    project_name = name or os.path.basename(path)
    with console.status(f"[bold green]Indexing project {project_name}..."):
        engine = _get_engine()
        result = engine.ingest_project(path, project_name, full_reindex=full)

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


# === PROJECTS ===

@app.command("projects")
def projects():
    """List indexed projects."""
    engine = _get_engine()
    project_list = engine.list_projects()

    table = Table(title="Indexed Projects")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Chunks", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Path")

    for p in project_list:
        table.add_row(p.name, p.type, str(p.total_chunks), str(p.file_count), p.path)

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
def watch():
    """Start filesystem watcher for live sync."""
    from knowledgeforge.ingestion.watcher import VaultWatcher
    engine = _get_engine()
    watcher = VaultWatcher(engine, engine.config)

    console.print("[bold green]Starting filesystem watcher...[/bold green]")
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
