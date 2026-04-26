"""Safe SQLite inspection and JSONL export helpers for Codex logs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


SECRET_NAME_PATTERN = re.compile(
    r"(secret|token|cookie|credential|password|private_key|api_key|auth)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b(secret|token|cookie|credential|password|private[_-]?key|api[_-]?key|auth)\b"
    r"\s*[:=]\s*([^\s,;]+)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})\b"
)
BODY_COLUMNS = ("feedback_log_body", "body", "content", "message", "text")
TIME_COLUMNS = ("timestamp", "created_at", "time", "datetime")
METADATA_COLUMNS = ("module_path", "target", "module")


def inspect_sqlite_schema(db_path: str | Path) -> dict[str, Any]:
    """Return serializable SQLite schema metadata without row values."""
    path = Path(db_path)
    conn = _connect_readonly(path)
    try:
        tables = []
        for name, sql in conn.execute(
            "select name, sql from sqlite_master "
            "where type = 'table' and name not like 'sqlite_%' order by name"
        ):
            columns = [
                row[1]
                for row in conn.execute(f"pragma table_info({_quote_string_literal(name)})")
            ]
            row_count = conn.execute(
                f"select count(*) from {_quote_identifier(name)}"
            ).fetchone()[0]
            tables.append(
                {
                    "name": name,
                    "columns": columns,
                    "row_count": row_count,
                    "sql": sql,
                }
            )
    finally:
        conn.close()

    return {
        "generated_at": _utc_now(),
        "db_path": str(path),
        "tables": tables,
    }


def export_codex_logs(
    db_path: str | Path,
    output_dir: str | Path,
    limit_threads: int = 20,
) -> dict[str, Any]:
    """Export Codex SQLite rows as grouped, low-risk conversation JSONL."""
    path = Path(db_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    schema = inspect_sqlite_schema(path)
    schema_path = output_path / "schema.json"
    schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    thread_rows: dict[str, list[dict[str, Any]]] = {}
    thread_order: list[str] = []
    redacted_rows = 0

    conn = _connect_readonly(path)
    try:
        conn.row_factory = sqlite3.Row
        for table in schema["tables"]:
            table_name = table["name"]
            columns = [column for column in table["columns"] if not _is_secret_name(column)]
            if not columns:
                continue

            selected = ", ".join(_quote_identifier(column) for column in columns)
            query = (
                f"select rowid as __kf_rowid, {selected} "
                f"from {_quote_identifier(table_name)} order by rowid"
            )
            try:
                rows = conn.execute(query)
            except sqlite3.DatabaseError:
                rows = conn.execute(
                    f"select {selected} from {_quote_identifier(table_name)}"
                )

            for fallback_index, row in enumerate(rows, start=1):
                row_data = dict(row)
                normalized, redacted = _normalize_row(table_name, row_data, fallback_index)
                if normalized is None:
                    continue
                if redacted:
                    redacted_rows += 1

                thread_id = normalized["thread_id"]
                if thread_id not in thread_rows:
                    thread_rows[thread_id] = []
                    thread_order.append(thread_id)
                thread_rows[thread_id].append(normalized)
    finally:
        conn.close()

    manifest_threads = []
    exported_rows = 0
    for thread_id in thread_order[: max(limit_threads, 0)]:
        rows = thread_rows[thread_id]
        rows.sort(key=_row_sort_key)
        jsonl_path = output_path / f"{_safe_filename(thread_id)}.jsonl"
        jsonl_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        manifest_threads.append(
            {
                "thread_id": thread_id,
                "jsonl_path": str(jsonl_path),
                "row_count": len(rows),
            }
        )
        exported_rows += len(rows)

    manifest = {
        "generated_at": _utc_now(),
        "db_path": str(path),
        "output_dir": str(output_path),
        "schema_path": str(schema_path),
        "thread_count": len(manifest_threads),
        "exported_rows": exported_rows,
        "redacted_rows": redacted_rows,
        "threads": manifest_threads,
    }
    manifest_path = output_path / "manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _normalize_row(
    table_name: str,
    row: dict[str, Any],
    fallback_index: int,
) -> tuple[dict[str, Any] | None, bool]:
    lookup = {key.lower(): key for key in row if key != "__kf_rowid"}
    thread_key = lookup.get("thread_id")
    row_id = row.get("__kf_rowid", fallback_index)
    thread_id = _value_as_text(row.get(thread_key)) if thread_key else ""
    if not thread_id:
        thread_id = f"{table_name}-row-{row_id}"

    body_value = None
    for candidate in BODY_COLUMNS:
        key = lookup.get(candidate)
        if key and row.get(key) not in (None, ""):
            body_value = row.get(key)
            break
    if body_value in (None, ""):
        return None, False

    content, redacted = _redact_text(_value_as_text(body_value))
    normalized: dict[str, Any] = {
        "type": "user",
        "source_agent": "codex",
        "thread_id": thread_id,
        "message": {"content": content},
    }

    timestamp = _first_present(row, lookup, TIME_COLUMNS)
    if timestamp:
        timestamp_text = _value_as_text(timestamp)
        normalized["timestamp"] = timestamp_text
        if lookup.get("created_at"):
            normalized["created_at"] = timestamp_text

    for metadata_column in METADATA_COLUMNS:
        key = lookup.get(metadata_column)
        if not key:
            continue
        value = row.get(key)
        value_text = _value_as_text(value)
        if value_text and not _looks_secret_value(value_text):
            normalized[metadata_column] = value_text

    return normalized, redacted


def _connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_PATTERN.search(name))


def _looks_secret_value(value: str) -> bool:
    return bool(SECRET_ASSIGNMENT_PATTERN.search(value) or SECRET_VALUE_PATTERN.search(value))


def _redact_text(value: str) -> tuple[str, bool]:
    redacted = SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    redacted = SECRET_VALUE_PATTERN.sub("[REDACTED]", redacted)
    return redacted, redacted != value


def _first_present(row: dict[str, Any], lookup: dict[str, str], columns: tuple[str, ...]) -> Any:
    for candidate in columns:
        key = lookup.get(candidate)
        if key and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _value_as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _row_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("timestamp") or row.get("created_at") or ""), row["thread_id"])


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "-" for char in value)
    return safe.strip("-") or "thread"


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
