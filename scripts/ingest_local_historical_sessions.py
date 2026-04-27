#!/usr/bin/env python3
"""Archive and ingest local historical AI sessions on the server.

The default path is intentionally conservative:

- inventory every known local source;
- copy supported JSONL conversations into the raw archive;
- index supported JSONL into the low-trust `conversations` collection;
- leave structured memory extraction opt-in behind `--extract-memory`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.engine import KnowledgeForgeEngine
from knowledgeforge.ingestion.conversations import (
    chunk_exchange,
    detect_source_agent,
    parse_jsonl_file,
    scan_conversation_dirs,
)
from knowledgeforge.ingestion.historical_sessions import (
    archive_file,
    build_local_inventory,
    file_sha256,
    supported_conversation_source_dirs,
    upsert_state,
    utc_now,
    write_json,
    write_manifest_line,
)
from knowledgeforge.ingestion.memory_extraction import (
    build_conversation_extraction_prompt,
    memory_cards_from_extraction,
    parse_extraction_json,
)


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[^'\"\s]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def has_secret_like_text(value: Any) -> bool:
    """Return True when a nested payload appears to include credentials."""

    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def matching_source_root(path: str, source_dirs: list[str]) -> tuple[str, str]:
    """Return the source key/root that best matches a JSONL path."""

    resolved = Path(path).expanduser().resolve()
    best_root = ""
    for source_dir in source_dirs:
        root = Path(source_dir).expanduser().resolve()
        try:
            resolved.relative_to(root)
            if len(str(root)) > len(best_root):
                best_root = str(root)
        except ValueError:
            continue

    if not best_root:
        return "unknown", str(resolved.parent)

    root_path = Path(best_root)
    if root_path.name == "projects" and root_path.parent.name == ".claude":
        return "claude_projects", best_root
    if root_path.name == "conversation-archive":
        return "superpowers_archive", best_root
    return root_path.name.replace(".", "_"), best_root


def parse_dry_run(
    jsonl_files: list[str],
    max_tool_result_chars: int,
    count_chunks: bool = False,
) -> dict[str, Any]:
    """Parse JSONL sessions and count what would be indexed."""

    started = time.time()
    result = {
        "files_scanned": len(jsonl_files),
        "files_with_exchanges": 0,
        "files_skipped": 0,
        "exchanges": 0,
        "chunks": 0,
        "chunk_count_mode": "token_chunker" if count_chunks else "estimated_one_per_exchange",
        "errors": [],
        "by_agent": {},
        "by_project": {},
    }

    for fpath in jsonl_files:
        try:
            exchanges = parse_jsonl_file(fpath, max_tool_result_chars=max_tool_result_chars)
            if not exchanges:
                result["files_skipped"] += 1
                continue

            result["files_with_exchanges"] += 1
            result["exchanges"] += len(exchanges)
            for exchange in exchanges:
                result["chunks"] += len(chunk_exchange(exchange)) if count_chunks else 1
                result["by_agent"][exchange.source_agent] = (
                    result["by_agent"].get(exchange.source_agent, 0) + 1
                )
                result["by_project"][exchange.project] = (
                    result["by_project"].get(exchange.project, 0) + 1
                )
        except Exception as exc:
            result["errors"].append({"path": fpath, "error": str(exc)})

    result["duration_seconds"] = round(time.time() - started, 2)
    result["by_agent"] = dict(sorted(result["by_agent"].items()))
    result["by_project"] = dict(
        sorted(result["by_project"].items(), key=lambda item: item[1], reverse=True)
    )
    return result


def archive_jsonl_files(
    jsonl_files: list[str],
    source_dirs: list[str],
    archive_root: Path,
    state_db: Path,
    host: str,
) -> dict[str, Any]:
    """Copy raw JSONL files into the normalized raw archive."""

    manifest_path = (
        archive_root.expanduser()
        / host
        / "_manifests"
        / f"local-jsonl-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    result = {
        "files_archived": 0,
        "manifest_path": str(manifest_path),
        "errors": [],
    }

    for fpath in jsonl_files:
        try:
            source_key, source_root = matching_source_root(fpath, source_dirs)
            destination, metadata = archive_file(
                Path(fpath),
                Path(source_root),
                archive_root,
                host=host,
                source_key=source_key,
            )
            write_manifest_line(manifest_path, metadata)
            stat = Path(fpath).stat()
            upsert_state(
                state_db,
                {
                    "host": host,
                    "source_agent": detect_source_agent(fpath),
                    "source_path": str(Path(fpath).resolve()),
                    "source_hash": metadata["sha256"],
                    "source_size": stat.st_size,
                    "source_mtime": metadata["mtime"],
                    "adapter": "jsonl_conversation",
                    "raw_archive_path": str(destination),
                    "raw_index_status": "archived",
                    "extraction_status": "pending",
                },
            )
            result["files_archived"] += 1
        except Exception as exc:
            result["errors"].append({"path": fpath, "error": str(exc)})

    return result


def call_openrouter_json(prompt: str, api_key: str, model: str, api_base: str) -> dict[str, Any]:
    """Call an OpenAI-compatible chat completion endpoint and parse JSON output."""

    response = httpx.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/invidtiv/knowledgeforge",
            "X-Title": "KnowledgeForge historical ingestion",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only valid JSON. Do not include secrets from the input.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Extraction request failed: HTTP {response.status_code}")

    data = response.json()
    raw = data["choices"][0]["message"]["content"] or "{}"
    return parse_extraction_json(raw)


def normalize_historical_cards(cards):
    """Force batch-imported historical session cards into low-trust defaults."""

    promoted_statuses = {"active", "active_verified", "verified", "current"}
    for card in cards:
        card.current_truth = False
        card.needs_repo_confirmation = True
        if card.status in promoted_statuses:
            card.status = "active_unverified"
    return cards


def extract_memory_cards(
    jsonl_files: list[str],
    config: KnowledgeForgeConfig,
    engine: KnowledgeForgeEngine,
    output_root: Path,
    state_db: Path,
    host: str,
    limit: int,
    model: str,
    api_base: str,
    max_chars: int,
    allow_secret_like_output: bool = False,
) -> dict[str, Any]:
    """Run guarded structured-memory extraction for a limited set of sessions."""

    if limit <= 0:
        raise ValueError("--extraction-limit must be > 0 when --extract-memory is set")
    if not model:
        raise ValueError("--extraction-model or KNOWLEDGEFORGE_MEMORY_EXTRACTION_MODEL is required")

    api_key = config.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required for structured memory extraction")

    result = {
        "enabled": True,
        "model": model,
        "limit": limit,
        "sessions_attempted": 0,
        "sessions_imported": 0,
        "cards_imported": 0,
        "skipped_for_secret_like_output": 0,
        "errors": [],
    }

    candidates = sorted(jsonl_files, key=lambda p: Path(p).stat().st_mtime, reverse=True)[:limit]
    for fpath in candidates:
        result["sessions_attempted"] += 1
        source_agent = detect_source_agent(fpath)
        session_id = Path(fpath).stem
        extraction_path = output_root / host / source_agent / f"{session_id}.json"
        try:
            exchanges = parse_jsonl_file(
                fpath,
                max_tool_result_chars=config.conversation_max_tool_result_chars,
            )
            if not exchanges:
                upsert_state(
                    state_db,
                    {
                        "host": host,
                        "source_agent": source_agent,
                        "source_path": str(Path(fpath).resolve()),
                        "source_hash": file_sha256(Path(fpath)),
                        "adapter": "jsonl_conversation",
                        "raw_index_status": "indexed",
                        "extraction_status": "skipped_empty",
                    },
                )
                continue

            prompt = build_conversation_extraction_prompt(
                exchanges,
                title=session_id,
                max_chars=max_chars,
            )
            payload = call_openrouter_json(prompt, api_key=api_key, model=model, api_base=api_base)
            extraction_path.parent.mkdir(parents=True, exist_ok=True)
            extraction_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            if has_secret_like_text(payload) and not allow_secret_like_output:
                result["skipped_for_secret_like_output"] += 1
                upsert_state(
                    state_db,
                    {
                        "host": host,
                        "source_agent": source_agent,
                        "source_path": str(Path(fpath).resolve()),
                        "source_hash": file_sha256(Path(fpath)),
                        "adapter": "jsonl_conversation",
                        "raw_index_status": "indexed",
                        "extraction_status": "skipped_secret_like_output",
                        "last_error": "Secret-like output detected",
                    },
                )
                continue

            cards = memory_cards_from_extraction(payload, source_path=str(Path(fpath).resolve()))
            cards = normalize_historical_cards(cards)
            stored = engine.import_memory_cards(cards)
            result["sessions_imported"] += 1
            result["cards_imported"] += len(stored)
            stat = Path(fpath).stat()
            upsert_state(
                state_db,
                {
                    "host": host,
                    "source_agent": source_agent,
                    "source_path": str(Path(fpath).resolve()),
                    "source_hash": file_sha256(Path(fpath)),
                    "source_size": stat.st_size,
                    "source_mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "adapter": "jsonl_conversation",
                    "raw_index_status": "indexed",
                    "extraction_status": "imported",
                    "cards_imported": len(stored),
                },
            )
        except Exception as exc:
            result["errors"].append({"path": fpath, "error": str(exc)})
            upsert_state(
                state_db,
                {
                    "host": host,
                    "source_agent": source_agent,
                    "source_path": str(Path(fpath).resolve()),
                    "source_hash": file_sha256(Path(fpath)),
                    "adapter": "jsonl_conversation",
                    "raw_index_status": "indexed",
                    "extraction_status": "error",
                    "last_error": str(exc)[:500],
                },
            )

    return result


def default_report_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return ROOT / "data/historical_ingestion" / f"local_ingest_{stamp}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(Path.home()), help="Home directory to inspect")
    parser.add_argument("--host", default="server", help="Host label for archive/state records")
    parser.add_argument("--dry-run", action="store_true", help="Parse/count only; do not write archive or index")
    parser.add_argument(
        "--dry-run-count-chunks",
        action="store_true",
        help="Use the token chunker during dry-run. Slower, but exact.",
    )
    parser.add_argument("--full-reindex", action="store_true", help="Clear conversations collection before indexing")
    parser.add_argument("--skip-raw-archive", action="store_true", help="Do not copy raw JSONL files")
    parser.add_argument("--skip-conversation-index", action="store_true", help="Do not index conversations")
    parser.add_argument("--source-dir", action="append", default=[], help="Override/add JSONL source dir")
    parser.add_argument(
        "--archive-root",
        default=str(Path.home() / ".local/share/knowledgeforge/raw_sessions"),
        help="Raw session archive root",
    )
    parser.add_argument(
        "--state-db",
        default=str(ROOT / "data/historical_ingestion/state.sqlite3"),
        help="Historical ingestion state SQLite path",
    )
    parser.add_argument("--report", default="", help="JSON run report path")
    parser.add_argument("--extract-memory", action="store_true", help="Run structured memory extraction pilot")
    parser.add_argument("--extraction-limit", type=int, default=20, help="Max sessions for extraction pilot")
    parser.add_argument(
        "--extraction-model",
        default=os.getenv("KNOWLEDGEFORGE_MEMORY_EXTRACTION_MODEL", ""),
        help="OpenRouter chat model for extraction",
    )
    parser.add_argument(
        "--extraction-api-base",
        default=os.getenv("KNOWLEDGEFORGE_MEMORY_EXTRACTION_API_BASE", "https://openrouter.ai/api/v1"),
        help="OpenAI-compatible API base for extraction",
    )
    parser.add_argument("--extraction-max-chars", type=int, default=60000)
    parser.add_argument("--allow-secret-like-output", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("KNOWLEDGEFORGE_CONFIG", str(ROOT / "config.yaml"))
    config = KnowledgeForgeConfig.load_config()
    source_dirs = args.source_dir or supported_conversation_source_dirs(Path(args.home))
    report_path = Path(args.report) if args.report else default_report_path()
    state_db = Path(args.state_db).expanduser()
    archive_root = Path(args.archive_root).expanduser()

    started = time.time()
    jsonl_files = scan_conversation_dirs(source_dirs)
    inventory = build_local_inventory(Path(args.home))
    dry_run = parse_dry_run(
        jsonl_files,
        config.conversation_max_tool_result_chars,
        count_chunks=args.dry_run_count_chunks,
    )

    report: dict[str, Any] = {
        "host": args.host,
        "started_at": utc_now(),
        "dry_run": args.dry_run,
        "source_dirs": source_dirs,
        "jsonl_files": len(jsonl_files),
        "inventory": inventory,
        "parse_summary": dry_run,
        "raw_archive": {"skipped": True},
        "conversation_index": {"skipped": True},
        "memory_extraction": {"skipped": True},
        "state_db": str(state_db),
        "archive_root": str(archive_root),
    }

    if args.dry_run:
        report["completed_at"] = utc_now()
        report["duration_seconds"] = round(time.time() - started, 2)
        write_json(report_path, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if not args.skip_raw_archive:
        report["raw_archive"] = archive_jsonl_files(
            jsonl_files,
            source_dirs,
            archive_root,
            state_db,
            args.host,
        )

    engine = None
    if not args.skip_conversation_index:
        engine = KnowledgeForgeEngine(config)
        ingest_result = engine.ingest_conversations(
            source_dirs=source_dirs,
            full_reindex=args.full_reindex,
        )
        report["conversation_index"] = ingest_result.model_dump()

    if args.extract_memory:
        if engine is None:
            engine = KnowledgeForgeEngine(config)
        report["memory_extraction"] = extract_memory_cards(
            jsonl_files=jsonl_files,
            config=config,
            engine=engine,
            output_root=ROOT / "data/memory_extractions",
            state_db=state_db,
            host=args.host,
            limit=args.extraction_limit,
            model=args.extraction_model,
            api_base=args.extraction_api_base,
            max_chars=args.extraction_max_chars,
            allow_secret_like_output=args.allow_secret_like_output,
        )

    report["completed_at"] = utc_now()
    report["duration_seconds"] = round(time.time() - started, 2)
    write_json(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
