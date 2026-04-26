"""Metadata-only inventory helpers for historical session sources."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Iterable


SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".vscdb"}
SESSIONISH_SUFFIXES = {".jsonl", ".log", ".sqlite", ".sqlite3", ".db", ".vscdb"}
SESSIONISH_NAME_PATTERN = re.compile(
    r"(session|conversation|chat|transcript|history|rollout|thread|message)",
    re.IGNORECASE,
)
SECRET_PATH_PATTERN = re.compile(
    r"(secret|token|password|passwd|credential|credentials|apikey|api_key|private[_-]?key|auth)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceSpec:
    agent: str
    path: str
    adapter_status: str


def build_inventory(
    sources: Iterable[SourceSpec],
    host: str = "local",
    sample_limit: int = 20,
) -> dict:
    """Build a serializable metadata inventory without reading file contents."""
    return {
        "generated_at": _utc_now(),
        "host": host,
        "raw_files_read": False,
        "note": "Metadata-only inventory. Raw historical files were not opened or read.",
        "known_sources": [
            _inventory_source(source, max(sample_limit, 0))
            for source in sources
        ],
    }


def write_inventory(
    sources: Iterable[SourceSpec],
    output: str | Path,
    host: str = "local",
    sample_limit: int = 20,
) -> dict:
    """Write a UTF-8 JSON inventory artifact and return the payload."""
    payload = build_inventory(sources, host=host, sample_limit=sample_limit)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _inventory_source(source: SourceSpec, sample_limit: int) -> dict:
    root = Path(source.path).expanduser()
    payload = {
        "agent": source.agent,
        "path": source.path,
        "adapter_status": source.adapter_status,
        "exists": root.exists(),
        "total_files": 0,
        "json_files": 0,
        "jsonl_files": 0,
        "sqlite_db_files": 0,
        "log_files": 0,
        "candidate_files": 0,
        "total_bytes": 0,
        "newest_write": None,
        "likely_candidate_samples": [],
    }
    if not root.exists():
        return payload

    newest_timestamp: float | None = None
    for file_path in _iter_files(root):
        try:
            stat = file_path.stat()
        except OSError:
            continue

        suffix = file_path.suffix.lower()
        payload["total_files"] += 1
        payload["total_bytes"] += stat.st_size

        if suffix == ".json":
            payload["json_files"] += 1
        elif suffix == ".jsonl":
            payload["jsonl_files"] += 1
        if suffix in SQLITE_SUFFIXES:
            payload["sqlite_db_files"] += 1
        if suffix == ".log":
            payload["log_files"] += 1

        if newest_timestamp is None or stat.st_mtime > newest_timestamp:
            newest_timestamp = stat.st_mtime

        if _is_candidate(file_path):
            payload["candidate_files"] += 1
            if len(payload["likely_candidate_samples"]) < sample_limit:
                payload["likely_candidate_samples"].append(
                    {
                        "path": str(file_path),
                        "length": stat.st_size,
                        "last_write_time": _format_timestamp(stat.st_mtime),
                    }
                )

    if newest_timestamp is not None:
        payload["newest_write"] = _format_timestamp(newest_timestamp)
    return payload


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return

    try:
        iterator = root.rglob("*")
        for path in sorted(iterator, key=lambda item: str(item).lower()):
            if path.is_file():
                yield path
    except OSError:
        return


def _is_candidate(path: Path) -> bool:
    path_text = str(path)
    if SECRET_PATH_PATTERN.search(path_text):
        return False
    suffix = path.suffix.lower()
    if suffix in SESSIONISH_SUFFIXES:
        return True
    return bool(SESSIONISH_NAME_PATTERN.search(path.name))


def _format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
