"""Utilities for historical AI session inventory and raw archiving."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SESSION_EXTENSIONS = {".json", ".jsonl", ".sqlite", ".sqlite3", ".db", ".log"}


@dataclass(frozen=True)
class HistoricalSource:
    """A local source that may contain historical agent sessions."""

    key: str
    label: str
    path: Path
    adapter: str
    source_agent: str
    notes: str = ""


def utc_now() -> str:
    """Return an ISO timestamp in UTC."""

    return datetime.now(timezone.utc).isoformat()


def local_sources(home: Path | None = None) -> list[HistoricalSource]:
    """Return known server-side historical session locations."""

    home = Path(home or Path.home()).expanduser()
    return [
        HistoricalSource(
            key="claude_projects",
            label="Claude Code JSONL",
            path=home / ".claude/projects",
            adapter="jsonl_conversation",
            source_agent="claude",
            notes="Supported by the ConversationExchange JSONL parser.",
        ),
        HistoricalSource(
            key="superpowers_archive",
            label="Superpowers conversation archive",
            path=home / ".config/superpowers/conversation-archive",
            adapter="jsonl_conversation",
            source_agent="mixed",
            notes="Supported JSONL archive; includes Claude plus _codex/_gemini imported sessions.",
        ),
        HistoricalSource(
            key="codex_logs",
            label="Codex local logs SQLite",
            path=home / ".codex/logs_2.sqlite",
            adapter="sqlite_log_inventory",
            source_agent="codex",
            notes="Operational logs only; inventory before deciding whether any transcript-like records exist.",
        ),
        HistoricalSource(
            key="kimi_logs",
            label="Kimi logs",
            path=home / ".kimi/logs",
            adapter="diagnostic_log_inventory",
            source_agent="kimi",
            notes="Diagnostic logs; do not ingest as conversations by default.",
        ),
        HistoricalSource(
            key="gemini_history",
            label="Gemini history",
            path=home / ".gemini/history",
            adapter="candidate_inventory",
            source_agent="gemini",
            notes="Candidate location; may be empty on this server.",
        ),
        HistoricalSource(
            key="antigravity_local",
            label="Antigravity local config",
            path=home / ".gemini/antigravity",
            adapter="candidate_inventory",
            source_agent="antigravity",
            notes="Local config only on this server; full history expected on Windows HomePC.",
        ),
        HistoricalSource(
            key="windsurf_config",
            label="Windsurf local config",
            path=home / ".config/Windsurf",
            adapter="candidate_inventory",
            source_agent="windsurf",
            notes="Sparse local config on this server; full history expected on Windows HomePC.",
        ),
        HistoricalSource(
            key="windsurf_codeium",
            label="Windsurf Codeium config",
            path=home / ".codeium/windsurf",
            adapter="candidate_inventory",
            source_agent="windsurf",
            notes="Sparse local memory/config files.",
        ),
        HistoricalSource(
            key="openclaw_agents",
            label="OpenClaw agent state",
            path=home / ".openclaw/agents",
            adapter="candidate_inventory",
            source_agent="openclaw",
            notes="Inspect deeper before treating as transcript source.",
        ),
    ]


def supported_conversation_source_dirs(home: Path | None = None) -> list[str]:
    """Return local directories safe to pass to the JSONL conversation indexer."""

    dirs = []
    for source in local_sources(home):
        if source.adapter != "jsonl_conversation":
            continue
        if source.path.is_dir():
            dirs.append(str(source.path))
    return dirs


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute a SHA-256 hash for a file without loading it all at once."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def _count_directory_files(path: Path) -> dict[str, Any]:
    counts_by_ext: dict[str, int] = {}
    session_file_count = 0
    total_file_count = 0
    total_bytes = 0
    newest_mtime = ""
    samples: list[str] = []

    for item in path.rglob("*"):
        if not item.is_file():
            continue
        total_file_count += 1
        try:
            stat = item.stat()
        except OSError:
            continue

        suffix = item.suffix.lower() or "<none>"
        counts_by_ext[suffix] = counts_by_ext.get(suffix, 0) + 1
        total_bytes += stat.st_size
        item_mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        if item_mtime > newest_mtime:
            newest_mtime = item_mtime

        if suffix in SESSION_EXTENSIONS:
            session_file_count += 1
            if len(samples) < 8:
                samples.append(str(item))

    return {
        "exists": True,
        "type": "directory",
        "total_files": total_file_count,
        "session_like_files": session_file_count,
        "bytes": total_bytes,
        "newest_mtime": newest_mtime,
        "counts_by_extension": dict(sorted(counts_by_ext.items())),
        "samples": samples,
    }


def _sqlite_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"tables": [], "errors": []}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        summary["errors"].append(str(exc))
        return summary

    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for row in rows:
            table = row["name"]
            table_info: dict[str, Any] = {"name": table}
            try:
                table_info["rows"] = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error as exc:
                table_info["row_count_error"] = str(exc)
            summary["tables"].append(table_info)

        if any(table["name"] == "logs" for table in summary["tables"]):
            row = conn.execute(
                """
                SELECT
                    count(*) AS row_count,
                    count(DISTINCT coalesce(thread_id, process_uuid)) AS group_count,
                    min(ts) AS min_ts,
                    max(ts) AS max_ts,
                    sum(CASE WHEN feedback_log_body IS NOT NULL
                              AND length(feedback_log_body) > 0
                             THEN 1 ELSE 0 END) AS body_rows
                FROM logs
                """
            ).fetchone()
            summary["codex_logs"] = {
                "rows": row["row_count"],
                "groups": row["group_count"],
                "feedback_body_rows": row["body_rows"],
                "min_ts": row["min_ts"],
                "max_ts": row["max_ts"],
            }
    except sqlite3.Error as exc:
        summary["errors"].append(str(exc))
    finally:
        conn.close()
    return summary


def inventory_source(source: HistoricalSource) -> dict[str, Any]:
    """Inventory one source without reading transcript contents."""

    path = source.path.expanduser()
    result = {
        **asdict(source),
        "path": str(path),
        "exists": path.exists(),
        "inventory_status": "missing",
        "inventoried_at": utc_now(),
    }

    if not path.exists():
        return result

    if path.is_dir():
        result.update(_count_directory_files(path))
        result["inventory_status"] = "ok"
        return result

    if path.is_file():
        stat = path.stat()
        result.update(
            {
                "type": "file",
                "total_files": 1,
                "session_like_files": 1 if path.suffix.lower() in SESSION_EXTENSIONS else 0,
                "bytes": stat.st_size,
                "newest_mtime": _mtime_iso(path),
                "counts_by_extension": {path.suffix.lower() or "<none>": 1},
                "samples": [str(path)],
                "sha256": file_sha256(path),
                "inventory_status": "ok",
            }
        )
        if source.adapter == "sqlite_log_inventory":
            result["sqlite"] = _sqlite_summary(path)
        return result

    result["inventory_status"] = "unsupported_path_type"
    return result


def build_local_inventory(home: Path | None = None) -> dict[str, Any]:
    """Build a full local historical-session inventory report."""

    sources = [inventory_source(source) for source in local_sources(home)]
    return {
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "home": str(Path(home or Path.home()).expanduser()),
        "created_at": utc_now(),
        "supported_conversation_source_dirs": supported_conversation_source_dirs(home),
        "sources": sources,
        "totals": {
            "sources": len(sources),
            "existing_sources": sum(1 for source in sources if source.get("exists")),
            "session_like_files": sum(int(source.get("session_like_files") or 0) for source in sources),
            "bytes": sum(int(source.get("bytes") or 0) for source in sources),
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file with stable formatting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def init_state_db(path: Path) -> None:
    """Create the historical ingestion state database if needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historical_session_files (
                host TEXT NOT NULL,
                source_agent TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL DEFAULT '',
                source_size INTEGER NOT NULL DEFAULT 0,
                source_mtime TEXT NOT NULL DEFAULT '',
                adapter TEXT NOT NULL DEFAULT '',
                raw_archive_path TEXT NOT NULL DEFAULT '',
                raw_index_status TEXT NOT NULL DEFAULT 'pending',
                extraction_status TEXT NOT NULL DEFAULT 'pending',
                cards_imported INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (host, source_path)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historical_session_status
            ON historical_session_files(host, source_agent, raw_index_status, extraction_status)
            """
        )


def upsert_state(path: Path, row: dict[str, Any]) -> None:
    """Insert or update one state row."""

    init_state_db(path)
    values = {
        "host": row.get("host", "server"),
        "source_agent": row.get("source_agent", ""),
        "source_path": row.get("source_path", ""),
        "source_hash": row.get("source_hash", ""),
        "source_size": int(row.get("source_size") or 0),
        "source_mtime": row.get("source_mtime", ""),
        "adapter": row.get("adapter", ""),
        "raw_archive_path": row.get("raw_archive_path", ""),
        "raw_index_status": row.get("raw_index_status", "pending"),
        "extraction_status": row.get("extraction_status", "pending"),
        "cards_imported": int(row.get("cards_imported") or 0),
        "last_error": row.get("last_error", ""),
        "updated_at": row.get("updated_at", utc_now()),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO historical_session_files (
                host, source_agent, source_path, source_hash, source_size,
                source_mtime, adapter, raw_archive_path, raw_index_status,
                extraction_status, cards_imported, last_error, updated_at
            ) VALUES (
                :host, :source_agent, :source_path, :source_hash, :source_size,
                :source_mtime, :adapter, :raw_archive_path, :raw_index_status,
                :extraction_status, :cards_imported, :last_error, :updated_at
            )
            ON CONFLICT(host, source_path) DO UPDATE SET
                source_agent = excluded.source_agent,
                source_hash = excluded.source_hash,
                source_size = excluded.source_size,
                source_mtime = excluded.source_mtime,
                adapter = excluded.adapter,
                raw_archive_path = excluded.raw_archive_path,
                raw_index_status = excluded.raw_index_status,
                extraction_status = excluded.extraction_status,
                cards_imported = excluded.cards_imported,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            values,
        )


def archive_file(
    source_path: Path,
    source_root: Path,
    archive_root: Path,
    host: str,
    source_key: str,
) -> tuple[Path, dict[str, Any]]:
    """Copy one raw file into the normalized archive and return manifest metadata."""

    source_path = source_path.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    try:
        relative_path = source_path.relative_to(source_root)
    except ValueError:
        relative_path = Path(source_path.name)

    destination = archive_root.expanduser() / host / source_key / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)

    stat = source_path.stat()
    digest = file_sha256(source_path)
    metadata = {
        "host": host,
        "source_key": source_key,
        "source_path": str(source_path),
        "archive_path": str(destination),
        "sha256": digest,
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "archived_at": utc_now(),
    }
    return destination, metadata


def write_manifest_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL manifest row."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
