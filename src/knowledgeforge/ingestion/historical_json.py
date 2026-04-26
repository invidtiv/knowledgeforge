"""Write low-trust JSON memory extraction artifacts for historical sessions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import base64
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse

from knowledgeforge.core.models import ConversationExchange
from knowledgeforge.ingestion.conversations import (
    clean_message,
    detect_project,
    generate_exchange_id,
    parse_jsonl_file,
)


SUPPORTED_TYPES = {
    "decision",
    "constraint",
    "failed_attempt",
    "resolution",
    "todo",
    "api_contract",
    "data_schema",
    "security_rule",
    "user_preference",
    "environment",
    "command",
    "file_path",
    "blocker",
    "objective",
    "definition_of_done",
}

SECRET_NAME_PATTERN = re.compile(
    r"(secret|token|password|passwd|credential|credentials|apikey|api_key|private[_-]?key|cookie|auth)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b([A-Za-z0-9_.-]*(?:secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|cookie|auth)[A-Za-z0-9_.-]*)\b"
    r"\s*[:=]\s*([^\s,;`\"']+)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"("
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"ya29\.[A-Za-z0-9_.-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(r"\+?\d[\d\s().-]{8,}\d")
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ADB_SERIAL_PATTERN = re.compile(r"\badb\s+-s\s+([A-Za-z0-9._:-]{8,})", re.IGNORECASE)
PATH_PATTERN = re.compile(
    r"([A-Za-z]:\\[^\s`\"']+|file://[^\s`\"']+|vscode-remote://[^\s`\"']+|/(?:home|root|Users|var|etc|opt|tmp|mnt|workspace)\b[^\s`\"']*)"
)
BASE64ISH_PATTERN = re.compile(r"^[A-Za-z0-9+/=_-]{80,}$")
COMMAND_PATTERN = re.compile(
    r"^\s*((adb\s+(-[sde]\b|shell\b|devices\b|install\b|pull\b|push\b|logcat\b|start-server\b|kill-server\b))|python3?\b|pytest\b|uv\b|npm\b|pnpm\b|yarn\b|git\b|docker\b|kubectl\b|knowledgeforge\b|vk\b|powershell\b|pwsh\b|bash\b)",
    re.IGNORECASE,
)
STACK_TRACE_PATTERN = re.compile(
    r"(Traceback \(most recent call last\)|\bat .+\(.+:\d+\)|^\s*File \".+\", line \d+)",
    re.IGNORECASE | re.MULTILINE,
)
CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RAW_LOG_PATTERN = re.compile(
    r"^(<w>|<e>|\[?warn(?:ing)?[:\s-]|error[:\s-]|failed to compile|webpack\.cache)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HistoricalSource:
    """Input source for JSON artifact generation."""

    agent: str
    path: str
    adapter_status: str


@dataclass
class CandidateCard:
    """Internal scored candidate before JSON serialization."""

    type: str
    project: str
    title: str
    body: str
    why: str
    status: str
    confidence: str
    source_title: str
    source_date: str
    message_refs: list[str]
    tags: list[str]
    score: int
    identity: str


def write_source_extraction_json(
    source: HistoricalSource,
    output_path: str | Path,
    limit_sessions: int = 0,
    max_cards: int = 40,
    max_sentence_chars: int = 360,
) -> dict[str, Any]:
    """Write a compact JSON extraction artifact for one historical source.

    The artifact follows the kfreport/import-json shape but does not import
    anything into KnowledgeForge. Historical conversation-derived cards are
    always low trust: ``current_truth=false`` and
    ``needs_repo_confirmation=true``.
    """
    adapter = source.adapter_status.lower().strip()
    if adapter in {"jsonl-supported", "jsonl", "claude-jsonl", "codex-jsonl"}:
        payload = build_jsonl_source_extraction(
            source,
            limit_sessions=limit_sessions,
            max_cards=max_cards,
            max_sentence_chars=max_sentence_chars,
        )
    elif adapter in {"vscode-storage", "windsurf-vscode-storage", "antigravity-vscode-storage"}:
        payload = build_vscode_storage_source_extraction(
            source,
            limit_sessions=limit_sessions,
            max_cards=max_cards,
            max_sentence_chars=max_sentence_chars,
        )
    else:
        payload = build_unsupported_source_extraction(source)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def build_jsonl_source_extraction(
    source: HistoricalSource,
    limit_sessions: int = 0,
    max_cards: int = 40,
    max_sentence_chars: int = 360,
) -> dict[str, Any]:
    """Build a compact source-level extraction JSON from supported JSONL files."""
    root = Path(source.path).expanduser()
    session_paths = _scan_jsonl_sessions(root)
    if limit_sessions > 0:
        session_paths = session_paths[:limit_sessions]

    candidates: list[CandidateCard] = []
    discarded: dict[str, int] = {
        "sessions_without_durable_candidates": 0,
        "sentences_skipped_for_secret_risk": 0,
        "sentences_skipped_as_noise": 0,
        "parse_errors": 0,
    }
    project_counts: dict[str, int] = {}

    parser = _parser_for_agent(source.agent)
    keep_pool = max(max_cards * 8, 20)

    for session_path in session_paths:
        try:
            exchanges = parser(session_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            discarded["parse_errors"] += 1
            continue

        before = len(candidates)
        for exchange in exchanges:
            project = _normalized_project(exchange.project)
            project_counts[project] = project_counts.get(project, 0) + 1
            candidates.extend(
                _cards_from_exchange(
                    exchange,
                    source.agent,
                    max_sentence_chars=max_sentence_chars,
                    discarded=discarded,
                )
            )

        if len(candidates) == before:
            discarded["sessions_without_durable_candidates"] += 1

        if len(candidates) > keep_pool:
            candidates = _rank_candidates(candidates)[:keep_pool]

    ranked = _rank_candidates(candidates)[: max(max_cards, 0)]
    cards = [_candidate_to_json(card) for card in ranked]
    source_label = source.agent.capitalize()
    projects = _top_projects(project_counts)

    if not cards:
        discarded["no_durable_cards_created"] = 1

    return {
        "conversation_summary": {
            "title": f"{source_label} historical session atomic extraction",
            "date": _today(),
            "projects_detected": projects or [source_label],
            "summary": (
                f"Scanned {len(session_paths)} {source.agent} JSONL session file(s) from "
                f"{source.path} and wrote only low-trust atomic memory candidates. "
                "Raw transcript text was not embedded wholesale and no KnowledgeForge import was attempted."
            ),
            "key_takeaways": [
                "Historical session candidates remain current_truth=false and need repo confirmation.",
                "Credential-like lines, private-key material, phone-number-like values, and noisy traces were skipped.",
                f"Generated {len(cards)} reusable candidate card(s) from {len(session_paths)} session file(s).",
            ],
        },
        "memory_cards": cards,
        "possible_conflicts": [],
        "discarded_noise": _discarded_noise(discarded),
    }


def build_unsupported_source_extraction(source: HistoricalSource) -> dict[str, Any]:
    """Build a JSON artifact for sources that need an adapter first."""
    source_label = source.agent.capitalize()
    title = f"{source_label} historical source pending adapter"
    return {
        "conversation_summary": {
            "title": title,
            "date": _today(),
            "projects_detected": [source_label],
            "summary": (
                f"{source_label} history at {source.path} was inventoried as "
                f"{source.adapter_status}. No raw logs were read into memory cards because "
                "a dedicated adapter is required before durable extraction."
            ),
            "key_takeaways": [
                "Unsupported VS Code-style storage must be adapter-inspected before atomic extraction.",
                "Raw logs and credential-bearing storage are not safe memory-card inputs.",
            ],
        },
        "memory_cards": [
            _manual_card(
                source,
                "blocker",
                f"{source_label} requires an adapter before durable cards can be extracted",
                (
                    f"{source_label} history is stored under {source.path} as "
                    f"{source.adapter_status}; do not treat raw storage/log files as memory cards."
                ),
                "This prevents accidental ingestion of VS Code storage internals, raw logs, or credential-bearing data.",
                "open_unverified",
                ["unsupported-adapter", source.agent.lower(), "historical-ingestion"],
            ),
            _manual_card(
                source,
                "todo",
                f"Build {source_label} VS Code-style history adapter",
                (
                    f"Inspect {source_label} state databases and logs schema-first, redact sensitive fields, "
                    "then emit the same atomic-card JSON shape before any KnowledgeForge import."
                ),
                "The source may contain useful historical sessions, but extraction needs a source-specific parser.",
                "open_unverified",
                ["adapter-work", source.agent.lower(), "vscode-storage"],
            ),
        ],
        "possible_conflicts": [],
        "discarded_noise": [
            {
                "content_type": "unsupported_source",
                "reason": "Adapter not built; raw source files intentionally left unread for memory extraction.",
            }
        ],
    }


def build_vscode_storage_source_extraction(
    source: HistoricalSource,
    limit_sessions: int = 0,
    max_cards: int = 40,
    max_sentence_chars: int = 360,
) -> dict[str, Any]:
    """Build extraction JSON from VS Code-style agent storage."""
    agent = source.agent.lower()
    if agent == "windsurf":
        exchanges, stats = _windsurf_exchanges(Path(source.path), limit_sessions=limit_sessions)
    elif agent == "antigravity":
        exchanges, stats = _antigravity_exchanges(Path(source.path), limit_sessions=limit_sessions)
    else:
        return build_unsupported_source_extraction(source)

    discarded: dict[str, int] = {
        "records_without_durable_candidates": 0,
        "sentences_skipped_for_secret_risk": 0,
        "sentences_skipped_as_noise": 0,
        "parse_errors": int(stats.get("parse_errors", 0)),
    }
    candidates: list[CandidateCard] = []
    keep_pool = max(max_cards * 8, 20)

    for exchange in exchanges:
        before = len(candidates)
        candidates.extend(
            _cards_from_exchange(
                exchange,
                source.agent,
                max_sentence_chars=max_sentence_chars,
                discarded=discarded,
            )
        )
        if len(candidates) == before:
            discarded["records_without_durable_candidates"] += 1
        if len(candidates) > keep_pool:
            candidates = _rank_candidates(candidates)[:keep_pool]

    ranked = _rank_candidates(candidates)[: max(max_cards, 0)]
    cards = [_candidate_to_json(card) for card in ranked]
    label = source.agent.capitalize()
    if not cards:
        discarded["no_durable_cards_created"] = 1

    return {
        "conversation_summary": {
            "title": f"{label} VS Code storage atomic extraction",
            "date": _today(),
            "projects_detected": stats.get("projects") or [label],
            "summary": (
                f"Scanned {stats.get('records_scanned', 0)} {source.agent} historical record(s) "
                f"from {source.path} and adapter-discovered companion stores. "
                "Only low-trust atomic memory candidates were written; no KnowledgeForge import was attempted."
            ),
            "key_takeaways": [
                "VS Code-style storage was read schema-first and selected by known non-secret conversation/history carriers.",
                "Historical candidates remain current_truth=false and need repo confirmation.",
                f"Generated {len(cards)} reusable candidate card(s) from {stats.get('records_scanned', 0)} extracted record(s).",
            ],
        },
        "memory_cards": cards,
        "possible_conflicts": [],
        "discarded_noise": _discarded_noise(discarded),
    }


def _parser_for_agent(agent: str) -> Callable[[Path], list[ConversationExchange]]:
    if agent.lower() == "codex":
        return parse_codex_jsonl_file
    return lambda path: parse_jsonl_file(str(path))


def parse_codex_jsonl_file(path: str | Path) -> list[ConversationExchange]:
    """Parse Codex session JSONL message records into exchanges."""
    source = Path(path)
    if not source.is_file():
        return []

    lines: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            data["_line_num"] = line_number
            lines.append(data)

    if not lines:
        return []

    exchanges: list[ConversationExchange] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.get("type") != "message" or line.get("role") != "user":
            i += 1
            continue

        user_line = int(line.get("_line_num") or i + 1)
        user_text = _extract_codex_text(line.get("content"))
        assistant_parts: list[str] = []
        last_line = user_line

        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            if next_line.get("type") == "message" and next_line.get("role") == "user":
                break
            if next_line.get("type") == "message" and next_line.get("role") in {"assistant", "system"}:
                text = _extract_codex_text(next_line.get("content") or next_line.get("summary"))
                if text.strip():
                    assistant_parts.append(text)
                last_line = int(next_line.get("_line_num") or last_line)
            j += 1

        assistant_text = "\n".join(assistant_parts).strip()
        if user_text.strip() or assistant_text:
            exchange_id = generate_exchange_id(str(source), user_line, last_line)
            exchanges.append(
                ConversationExchange(
                    exchange_id=exchange_id,
                    session_id=source.stem,
                    project=_codex_project(source),
                    timestamp=_first_timestamp(lines),
                    user_message=clean_message(user_text, max_chars=2000),
                    assistant_message=clean_message(assistant_text, max_chars=3000),
                    source_agent="codex",
                    archive_path=str(source),
                    line_start=user_line,
                    line_end=last_line,
                    tool_calls=[],
                    tool_names=[],
                    tool_error_count=0,
                )
            )

        i = j

    return exchanges


def _windsurf_exchanges(root: Path, limit_sessions: int = 0) -> tuple[list[ConversationExchange], dict[str, Any]]:
    root = root.expanduser()
    exchanges: list[ConversationExchange] = []
    stats: dict[str, Any] = {"records_scanned": 0, "parse_errors": 0, "projects": ["Windsurf"]}

    state_db = root / "User" / "globalStorage" / "state.vscdb"
    state = _read_vscdb_json_item(state_db, "google.geminicodeassist")
    thread_container = state.get("geminiCodeAssist.chatThreads") if isinstance(state, dict) else None
    if isinstance(thread_container, dict):
        for account_threads in thread_container.values():
            if not isinstance(account_threads, dict):
                continue
            for thread in account_threads.values():
                if not isinstance(thread, dict):
                    continue
                if limit_sessions and stats["records_scanned"] >= limit_sessions:
                    return exchanges, stats
                exchange = _exchange_from_windsurf_thread(thread)
                if exchange:
                    exchanges.append(exchange)
                    stats["records_scanned"] += 1

    return exchanges, stats


def _antigravity_exchanges(root: Path, limit_sessions: int = 0) -> tuple[list[ConversationExchange], dict[str, Any]]:
    root = root.expanduser()
    exchanges: list[ConversationExchange] = []
    stats: dict[str, Any] = {"records_scanned": 0, "parse_errors": 0, "projects": ["Antigravity"]}

    state_db = root / "User" / "globalStorage" / "state.vscdb"
    for key in (
        "antigravityUnifiedStateSync.trajectorySummaries",
        "antigravityUnifiedStateSync.artifactReview",
    ):
        if limit_sessions and stats["records_scanned"] >= limit_sessions:
            break
        try:
            texts = _texts_from_vscdb_base64_proto(state_db, key)
        except (OSError, sqlite3.DatabaseError, UnicodeError):
            stats["parse_errors"] += 1
            continue
        for index, text in enumerate(texts):
            if limit_sessions and stats["records_scanned"] >= limit_sessions:
                break
            cleaned = _clean_proto_text(text)
            if not cleaned:
                continue
            exchanges.append(
                _synthetic_exchange(
                    agent="antigravity",
                    project="Antigravity",
                    source_title=f"{key} #{index + 1}",
                    timestamp="",
                    text=cleaned,
                    source_path=str(state_db),
                    line_ref=key,
                )
            )
            stats["records_scanned"] += 1

    companion = _antigravity_companion_root()
    brain = companion / "brain"
    if brain.exists():
        for path in _iter_antigravity_markdown(brain):
            if limit_sessions and stats["records_scanned"] >= limit_sessions:
                break
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stats["parse_errors"] += 1
                continue
            text = _redact_sensitive_text(raw)
            if not text.strip():
                continue
            title = _first_markdown_heading(text) or f"{path.parent.name}/{path.name}"
            exchanges.append(
                _synthetic_exchange(
                    agent="antigravity",
                    project="Antigravity",
                    source_title=title[:120],
                    timestamp=_file_mtime_iso(path),
                    text=text,
                    source_path=str(path),
                    line_ref=path.name,
                )
            )
            stats["records_scanned"] += 1

    conversations = companion / "conversations"
    if conversations.exists():
        for path in sorted(conversations.glob("*.pb"), key=lambda item: (-_mtime(item), str(item).lower())):
            if limit_sessions and stats["records_scanned"] >= limit_sessions:
                break
            try:
                text = "\n".join(_extract_printable_strings(path.read_bytes()))
            except OSError:
                stats["parse_errors"] += 1
                continue
            cleaned = _clean_proto_text(text)
            if not cleaned:
                continue
            exchanges.append(
                _synthetic_exchange(
                    agent="antigravity",
                    project="Antigravity",
                    source_title=path.stem,
                    timestamp=_file_mtime_iso(path),
                    text=cleaned,
                    source_path=str(path),
                    line_ref=path.name,
                )
            )
            stats["records_scanned"] += 1

    return exchanges, stats


def _antigravity_companion_root() -> Path:
    return Path(os.getenv("KNOWLEDGEFORGE_ANTIGRAVITY_HOME", str(Path.home() / ".gemini" / "antigravity"))).expanduser()


def _exchange_from_windsurf_thread(thread: dict[str, Any]) -> ConversationExchange | None:
    history = thread.get("history")
    if not isinstance(history, list):
        return None

    parts: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        text = str(item.get("markdownText") or item.get("text") or "").strip()
        if text:
            entity = str(item.get("entity") or "message").lower()
            parts.append(f"{entity}: {text}")

    if not parts:
        return None

    title = str(thread.get("title") or thread.get("id") or "Windsurf chat thread").strip()
    project = _project_from_windsurf_thread(thread) or "Windsurf"
    return _synthetic_exchange(
        agent="windsurf",
        project=project,
        source_title=title[:120],
        timestamp=str(thread.get("update_time") or thread.get("create_time") or ""),
        text=f"{title}\n" + "\n".join(parts),
        source_path="Windsurf/User/globalStorage/state.vscdb:google.geminicodeassist",
        line_ref=str(thread.get("id") or "thread"),
    )


def _synthetic_exchange(
    agent: str,
    project: str,
    source_title: str,
    timestamp: str,
    text: str,
    source_path: str,
    line_ref: str,
) -> ConversationExchange:
    clean = clean_message(_redact_sensitive_text(text), max_chars=8000)
    exchange_id = hashlib.sha256(
        f"{agent}:{source_title}:{timestamp}:{line_ref}:{clean[:200]}".encode("utf-8")
    ).hexdigest()[:32]
    return ConversationExchange(
        exchange_id=exchange_id,
        session_id=source_title or exchange_id,
        project=project,
        timestamp=timestamp,
        user_message=clean,
        assistant_message="",
        source_agent=agent,
        archive_path=source_path,
        line_start=1,
        line_end=1,
        tool_calls=[],
        tool_names=[],
        tool_error_count=0,
    )


def _read_vscdb_json_item(db_path: Path, key: str) -> Any:
    raw = _read_vscdb_item(db_path, key)
    if raw == "":
        return {}
    return json.loads(raw)


def _read_vscdb_item(db_path: Path, key: str) -> str:
    resolved = db_path.expanduser().resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    value = row[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _texts_from_vscdb_base64_proto(db_path: Path, key: str) -> list[str]:
    raw = _read_vscdb_item(db_path, key)
    if not raw:
        return []
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        data = raw.encode("utf-8", errors="replace")
    return _extract_printable_strings(data)


def _extract_printable_strings(data: bytes, depth: int = 0) -> list[str]:
    found: list[str] = []
    for raw in re.findall(rb"[\x20-\x7E]{5,}", data):
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if _credential_risk(text):
            continue
        if BASE64ISH_PATTERN.fullmatch(text) and depth < 2:
            try:
                decoded = base64.b64decode(text, validate=True)
            except Exception:
                decoded = b""
            if decoded:
                found.extend(_extract_printable_strings(decoded, depth=depth + 1))
            continue
        found.append(text)
    return found


def _clean_proto_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _redact_sensitive_text(raw_line).strip()
        if not line or _credential_risk(line):
            continue
        if BASE64ISH_PATTERN.fullmatch(line):
            continue
        if line.startswith(("file://", "vscode-remote://")):
            continue
        if len(line) < 8:
            continue
        lines.append(line)
    return "\n".join(lines[:200])


def _iter_antigravity_markdown(brain_root: Path) -> list[Path]:
    names = {"task.md", "implementation_plan.md", "walkthrough.md"}
    paths = [
        path
        for path in brain_root.rglob("*.md")
        if path.name in names and not SECRET_NAME_PATTERN.search(str(path))
    ]
    return sorted(paths, key=lambda item: (-_mtime(item), str(item).lower()))


def _first_markdown_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _project_from_windsurf_thread(thread: dict[str, Any]) -> str:
    for key in ("ideContextFolderUris", "ideContextFileUris"):
        values = thread.get(key)
        if isinstance(values, list) and values:
            candidate = _project_from_uri(str(values[0]))
            if candidate:
                return candidate
    for item in thread.get("history") or []:
        if not isinstance(item, dict):
            continue
        context = item.get("ideContext")
        if not isinstance(context, dict):
            continue
        folder = context.get("currentFolder") or context.get("workspaceFolder")
        if isinstance(folder, dict):
            candidate = _project_from_uri(str(folder.get("path") or folder.get("uri") or ""))
            if candidate:
                return candidate
        current_file = context.get("currentFile")
        if isinstance(current_file, dict):
            candidate = _project_from_uri(str(current_file.get("path") or current_file.get("uri") or ""))
            if candidate:
                return candidate
    return ""


def _project_from_uri(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    raw_path = unquote(parsed.path if parsed.scheme else value)
    raw_path = raw_path.replace("\\", "/").rstrip("/")
    if not raw_path:
        return ""
    parts = [part for part in raw_path.split("/") if part and ":" not in part]
    if not parts:
        return ""
    if "." in parts[-1] and len(parts) > 1:
        return parts[-2][:120]
    return parts[-1][:120]


def _millis_to_iso(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 10_000_000_000:
        number = number / 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(_mtime(path), tz=timezone.utc).isoformat() if _mtime(path) else ""


def _extract_codex_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type in {"input_text", "output_text", "text", "summary_text"} or "text" in item:
                parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part.strip())


def _cards_from_exchange(
    exchange: ConversationExchange,
    agent: str,
    max_sentence_chars: int,
    discarded: dict[str, int],
) -> list[CandidateCard]:
    cards: list[CandidateCard] = []
    source_title = exchange.session_id or Path(exchange.archive_path).stem
    source_date = _date_only(exchange.timestamp)
    project = _normalized_project(exchange.project)

    for role, text in (("user", exchange.user_message), ("assistant", exchange.assistant_message)):
        for sentence in _candidate_sentences(text):
            if _credential_risk(sentence):
                discarded["sentences_skipped_for_secret_risk"] += 1
                continue
            if _looks_like_noise(sentence):
                discarded["sentences_skipped_as_noise"] += 1
                continue

            redacted = _redact_sensitive_text(sentence).strip()
            if not _has_reusable_signal(redacted):
                continue

            kind = _classify(redacted)
            if not kind:
                continue

            card_type, why, tags, score = kind
            clipped = _clip_sentence(redacted, max_sentence_chars)
            identity = _identity(card_type, project, clipped)
            cards.append(
                CandidateCard(
                    type=card_type,
                    project=project,
                    title=_title_for(card_type, clipped),
                    body=(
                        f"Historical {agent} session {source_title} recorded this reusable item: "
                        f"{clipped}"
                    ),
                    why=why,
                    status=_status_for(card_type),
                    confidence="medium" if role == "user" else "low",
                    source_title=source_title,
                    source_date=source_date,
                    message_refs=[f"exchange:{exchange.exchange_id}"],
                    tags=sorted(set(tags + [agent.lower(), "historical-session"])),
                    score=score + (2 if role == "user" else 0),
                    identity=identity,
                )
            )
    return cards


def _candidate_sentences(text: str) -> Iterable[str]:
    text = CODE_FENCE_PATTERN.sub(" ", text or "")
    text = ANSI_ESCAPE_PATTERN.sub("", text)
    text = text.replace("\r\n", "\n")
    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip(" -*>\t")
        if not line:
            continue
        if len(line) > 700:
            chunks.extend(re.split(r"(?<=[.!?])\s+", line))
        else:
            chunks.append(line)

    for chunk in chunks:
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if 24 <= len(chunk) <= 700:
            yield chunk


def _classify(text: str) -> tuple[str, str, list[str], int] | None:
    lowered = text.lower()

    rules: list[tuple[str, str, list[str], int, tuple[str, ...]]] = [
        (
            "security_rule",
            "Security handling rules prevent accidental exposure during future ingestion or development work.",
            ["security", "secret-handling"],
            95,
            ("never store secret", "do not store secret", "tokens", "private keys", "credential-bearing", "cookies"),
        ),
        (
            "definition_of_done",
            "Definitions of done help a future agent know when the task is actually complete.",
            ["done-criteria", "acceptance"],
            92,
            ("done criteria", "definition of done", "acceptance criteria"),
        ),
        (
            "objective",
            "Objectives preserve the intended outcome without importing the whole conversation.",
            ["objective", "intent"],
            90,
            ("goal:", "objective:", "we are trying to", "the goal is", "need to build", "trying to build"),
        ),
        (
            "failed_attempt",
            "Failed attempts keep future agents from repeating known unproductive paths.",
            ["failed-attempt", "avoid-repeat"],
            88,
            (
                "failed because",
                "failed due to",
                "failed attempt",
                "did not work",
                "didn't work",
                "does not work",
                "doesn't work",
                "the error was",
                "blocked by",
            ),
        ),
        (
            "resolution",
            "Resolutions capture what fixed or verified a previous problem.",
            ["resolution", "fix"],
            86,
            ("fixed", "resolved", "verified", "tests pass", "passed", "working now", "solution"),
        ),
        (
            "api_contract",
            "API contracts are durable integration facts that future work must preserve or confirm.",
            ["api-contract", "interface"],
            82,
            ("endpoint", "api contract", "request body", "response body", "route", "status code"),
        ),
        (
            "data_schema",
            "Schemas and field contracts are useful for adapter and import work.",
            ["schema", "data-shape"],
            80,
            ("schema", "json shape", "fields", "columns", "table", "property"),
        ),
        (
            "todo",
            "Open work should stay visible without being treated as verified implementation state.",
            ["todo", "open-work"],
            78,
            ("todo", "next step", "still need", "needs to", "remaining", "follow up", "pending"),
        ),
        (
            "blocker",
            "Blockers explain why a task could not continue and what must be resolved first.",
            ["blocker", "risk"],
            76,
            ("blocked", "blocker", "cannot continue", "can't continue", "missing"),
        ),
        (
            "decision",
            "Decisions preserve why a particular implementation path was chosen.",
            ["decision", "rationale"],
            74,
            ("decided", "decision", "we chose", "use ", "prefer ", "approach is", "will use"),
        ),
        (
            "constraint",
            "Constraints prevent future work from violating known project rules.",
            ["constraint", "guardrail"],
            72,
            ("must ", "must not", "only ", "do not", "don't", "required", "non-negotiable", "constraint"),
        ),
        (
            "user_preference",
            "User preferences should shape future agent behavior across sessions.",
            ["user-preference", "workflow"],
            70,
            ("user wants", "user prefers", "always ", "do not ask", "keep ", "prefer fewer"),
        ),
        (
            "environment",
            "Environment facts prevent setup drift and wrong-path command execution.",
            ["environment", "setup"],
            62,
            ("windows", "linux", "pythonpath", "venv", "config", "environment", "localhost", "path is"),
        ),
    ]

    if COMMAND_PATTERN.search(text):
        return (
            "command",
            "Reusable commands can help future agents operate the project safely.",
            ["command", "operator-note"],
            64,
        )
    if PATH_PATTERN.search(text):
        return (
            "file_path",
            "File paths are useful only when they identify durable project locations.",
            ["file-path", "environment"],
            55,
        )

    for card_type, why, tags, score, needles in rules:
        if any(needle in lowered for needle in needles):
            return card_type, why, tags, score
    return None


def _candidate_to_json(card: CandidateCard) -> dict[str, Any]:
    return {
        "type": card.type,
        "project": card.project,
        "title": card.title,
        "body": card.body,
        "why": card.why,
        "status": card.status,
        "confidence": card.confidence,
        "current_truth": False,
        "needs_repo_confirmation": True,
        "source": {
            "conversation_title": card.source_title,
            "conversation_date": card.source_date,
            "message_refs": card.message_refs,
        },
        "tags": card.tags,
    }


def _manual_card(
    source: HistoricalSource,
    card_type: str,
    title: str,
    body: str,
    why: str,
    status: str,
    tags: list[str],
) -> dict[str, Any]:
    return {
        "type": card_type,
        "project": source.agent.capitalize(),
        "title": title,
        "body": body,
        "why": why,
        "status": status,
        "confidence": "high",
        "current_truth": False,
        "needs_repo_confirmation": True,
        "source": {
            "conversation_title": f"{source.agent.capitalize()} historical source inventory",
            "conversation_date": _today(),
            "message_refs": [],
        },
        "tags": tags,
    }


def _rank_candidates(candidates: list[CandidateCard]) -> list[CandidateCard]:
    by_identity: dict[str, CandidateCard] = {}
    for card in candidates:
        existing = by_identity.get(card.identity)
        if existing is None or (card.score, card.source_date) > (existing.score, existing.source_date):
            by_identity[card.identity] = card
    return sorted(
        by_identity.values(),
        key=lambda item: (item.score, item.source_date, item.title),
        reverse=True,
    )


def _scan_jsonl_sessions(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".jsonl" else []
    if not root.exists():
        return []
    paths = list(root.rglob("*.jsonl"))
    return sorted(paths, key=lambda path: (-_mtime(path), str(path).lower()))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _credential_risk(text: str) -> bool:
    if SECRET_ASSIGNMENT_PATTERN.search(text) or SECRET_VALUE_PATTERN.search(text):
        return True
    if "-----BEGIN" in text and "KEY-----" in text:
        return True
    return False


def _redact_sensitive_text(text: str) -> str:
    text = ANSI_ESCAPE_PATTERN.sub("", text)
    text = SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = SECRET_VALUE_PATTERN.sub("[REDACTED]", text)
    text = ADB_SERIAL_PATTERN.sub("adb -s [REDACTED_DEVICE]", text)
    text = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    return text


def _looks_like_noise(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if RAW_LOG_PATTERN.search(stripped):
        return True
    if "webpack.cache" in lowered or "caching failed for pack" in lowered:
        return True
    if STACK_TRACE_PATTERN.search(text):
        return True
    if len(text) > 120 and (text.count("{") + text.count("}") + text.count(";")) > 18:
        return True
    if text.count("\\n") > 5:
        return True
    return False


def _has_reusable_signal(text: str) -> bool:
    lowered = text.lower()
    if len(text.split()) < 5:
        return False
    if SECRET_NAME_PATTERN.search(text) and not any(
        phrase in lowered
        for phrase in ("never store", "do not store", "exclude secrets", "secret-handling")
    ):
        return False
    signal_words = (
        "must",
        "never",
        "do not",
        "don't",
        "todo",
        "failed",
        "fixed",
        "resolved",
        "schema",
        "endpoint",
        "decision",
        "objective",
        "done criteria",
        "blocked",
        "path",
        "command",
        "prefer",
        "user wants",
        "next step",
        "verified",
    )
    return any(word in lowered for word in signal_words) or COMMAND_PATTERN.search(text) is not None


def _status_for(card_type: str) -> str:
    if card_type == "failed_attempt":
        return "failed"
    if card_type == "resolution":
        return "resolved"
    if card_type in {"todo", "blocker"}:
        return "open_unverified"
    return "historical"


def _title_for(card_type: str, sentence: str) -> str:
    cleaned = re.sub(r"[`*_#>]", "", sentence).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    words = cleaned.split()
    title = " ".join(words[:10]).rstrip(".,:;")
    if len(title) > 88:
        title = title[:85].rstrip() + "..."
    if not title:
        title = card_type.replace("_", " ").title()
    return title[:120]


def _clip_sentence(sentence: str, max_chars: int) -> str:
    sentence = re.sub(r"\s+", " ", sentence).strip()
    if len(sentence) <= max_chars:
        return sentence
    return sentence[: max(max_chars - 3, 20)].rstrip() + "..."


def _identity(card_type: str, project: str, body: str) -> str:
    normalized = re.sub(r"\W+", " ", body.lower()).strip()
    normalized = " ".join(normalized.split()[:24])
    raw = f"{card_type}:{project.lower()}:{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_project(project: str) -> str:
    project = (project or "").strip()
    if not project or project == "unknown":
        return "Unknown"
    return project[:120]


def _codex_project(path: Path) -> str:
    return "Codex"


def _first_timestamp(lines: list[dict[str, Any]]) -> str:
    for line in lines:
        value = line.get("timestamp")
        if value:
            return str(value)
    return ""


def _date_only(value: str) -> str:
    if not value:
        return ""
    return str(value)[:10]


def _top_projects(project_counts: dict[str, int], limit: int = 10) -> list[str]:
    return [
        project
        for project, _count in sorted(
            project_counts.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[:limit]
    ]


def _discarded_noise(discarded: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"content_type": key, "reason": "count", "count": value}
        for key, value in sorted(discarded.items())
        if value
    ]


def _today() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()
