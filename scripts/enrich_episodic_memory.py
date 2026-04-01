#!/usr/bin/env python3
"""
Episodic Memory → KnowledgeForge Migration with Kimi-Powered Metadata Enrichment

Reads all exchanges from the episodic memory SQLite database, sends each to Kimi CLI
for metadata enrichment, then stores the enriched data as JSON ready for KnowledgeForge
ingestion into a ChromaDB "conversations" collection.

Usage:
    python scripts/enrich_episodic_memory.py                    # Full run
    python scripts/enrich_episodic_memory.py --dry-run          # Preview without Kimi calls
    python scripts/enrich_episodic_memory.py --resume           # Resume from last checkpoint
    python scripts/enrich_episodic_memory.py --batch-size 10    # Process N at a time
    python scripts/enrich_episodic_memory.py --skip-enrichment  # Migrate without Kimi (basic metadata only)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EPISODIC_DB = Path.home() / ".config/superpowers/conversation-index/db.sqlite"
OUTPUT_DIR = Path.home() / "knowledgeforge/data/enriched_conversations"
CHECKPOINT_FILE = OUTPUT_DIR / "_checkpoint.json"
ERRORS_FILE = OUTPUT_DIR / "_errors.jsonl"
STATS_FILE = OUTPUT_DIR / "_stats.json"

KIMI_CMD = shutil.which("kimi") or "kimi"
KIMI_TIMEOUT = 90  # seconds per exchange
KIMI_MAX_RETRIES = 2

# Truncation limits to keep Kimi costs low
MAX_USER_MSG_CHARS = 1500
MAX_ASST_MSG_CHARS = 2000
MAX_TOOL_RESULT_CHARS = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enrich")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class RawExchange:
    id: str
    project: str
    timestamp: str
    user_message: str
    assistant_message: str
    archive_path: str
    line_start: int
    line_end: int
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    claude_version: str = ""
    thinking_level: str = ""
    is_sidechain: bool = False
    parent_uuid: str = ""
    tool_names: list = field(default_factory=list)
    tool_count: int = 0
    tool_error_count: int = 0


@dataclass
class EnrichedMetadata:
    summary: str = ""
    category: str = ""
    topics: list = field(default_factory=list)
    technologies: list = field(default_factory=list)
    outcome: str = ""
    intent: str = ""
    complexity: str = ""
    key_files: list = field(default_factory=list)
    searchable_text: str = ""


@dataclass
class EnrichedExchange:
    """Final enriched record ready for ChromaDB ingestion."""
    exchange_id: str
    project: str
    timestamp: str
    user_message: str
    assistant_message: str
    session_id: str
    archive_path: str
    line_start: int
    line_end: int
    cwd: str
    git_branch: str
    claude_version: str
    thinking_level: str
    is_sidechain: bool
    parent_uuid: str
    source_agent: str  # claude | codex | gemini
    tool_names: str  # comma-separated
    tool_count: int
    tool_error_count: int
    # Enriched fields (from Kimi)
    summary: str = ""
    category: str = ""
    topics: str = ""  # comma-separated (ChromaDB can't store lists)
    technologies: str = ""  # comma-separated
    outcome: str = ""
    intent: str = ""
    complexity: str = ""
    key_files: str = ""  # comma-separated
    searchable_text: str = ""
    # Embedding content (what gets embedded into the vector)
    embedding_content: str = ""
    enriched_at: str = ""
    enrichment_model: str = "kimi-k2.5"


# ---------------------------------------------------------------------------
# Database Reading
# ---------------------------------------------------------------------------

def read_all_exchanges() -> list[RawExchange]:
    """Read all exchanges + tool call data from episodic memory SQLite."""
    if not EPISODIC_DB.exists():
        log.error(f"Database not found: {EPISODIC_DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(EPISODIC_DB))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get exchanges
    cursor.execute("""
        SELECT
            e.id, e.project, e.timestamp, e.user_message, e.assistant_message,
            e.archive_path, e.line_start, e.line_end, e.session_id,
            e.cwd, e.git_branch, e.claude_version, e.thinking_level,
            e.is_sidechain, e.parent_uuid
        FROM exchanges e
        ORDER BY e.timestamp ASC
    """)
    rows = cursor.fetchall()

    # Get tool calls grouped by exchange
    cursor.execute("""
        SELECT exchange_id,
               group_concat(DISTINCT tool_name) as tool_names,
               count(*) as tool_count,
               sum(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) as error_count
        FROM tool_calls
        GROUP BY exchange_id
    """)
    tool_map = {}
    for row in cursor.fetchall():
        tool_map[row["exchange_id"]] = {
            "tool_names": (row["tool_names"] or "").split(","),
            "tool_count": row["tool_count"],
            "tool_error_count": row["error_count"] or 0,
        }

    conn.close()

    exchanges = []
    for row in rows:
        tools = tool_map.get(row["id"], {"tool_names": [], "tool_count": 0, "tool_error_count": 0})
        exchanges.append(RawExchange(
            id=row["id"],
            project=row["project"],
            timestamp=row["timestamp"],
            user_message=row["user_message"],
            assistant_message=row["assistant_message"],
            archive_path=row["archive_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            session_id=row["session_id"] or "",
            cwd=row["cwd"] or "",
            git_branch=row["git_branch"] or "",
            claude_version=row["claude_version"] or "",
            thinking_level=row["thinking_level"] or "",
            is_sidechain=bool(row["is_sidechain"]),
            parent_uuid=row["parent_uuid"] or "",
            tool_names=[t for t in tools["tool_names"] if t],
            tool_count=tools["tool_count"],
            tool_error_count=tools["tool_error_count"],
        ))

    log.info(f"Loaded {len(exchanges)} exchanges from episodic memory DB")
    return exchanges


# ---------------------------------------------------------------------------
# Source Agent Detection
# ---------------------------------------------------------------------------

def detect_source_agent(archive_path: str) -> str:
    """Detect which AI agent produced the conversation."""
    if "_codex/" in archive_path or "_codex\\" in archive_path:
        return "codex"
    elif "_gemini/" in archive_path or "_gemini\\" in archive_path:
        return "gemini"
    return "claude"


# ---------------------------------------------------------------------------
# Message Cleaning
# ---------------------------------------------------------------------------

def clean_message(msg: str, max_chars: int) -> str:
    """Clean and truncate a message for Kimi input."""
    # Remove XML-like IDE tags that add noise
    msg = re.sub(r"<ide_opened_file>.*?</ide_opened_file>", "[IDE file opened]", msg, flags=re.DOTALL)
    msg = re.sub(r"<system-reminder>.*?</system-reminder>", "", msg, flags=re.DOTALL)
    msg = re.sub(r"<.*?>.*?</.*?>", "", msg, flags=re.DOTALL)
    # Collapse whitespace
    msg = re.sub(r"\n{3,}", "\n\n", msg)
    msg = msg.strip()
    if len(msg) > max_chars:
        msg = msg[:max_chars] + "... [truncated]"
    return msg


# ---------------------------------------------------------------------------
# Kimi Enrichment
# ---------------------------------------------------------------------------

ENRICHMENT_PROMPT_TEMPLATE = """You are a metadata extraction assistant. Given a conversation exchange between a user and an AI coding assistant, produce enriched metadata as JSON.

INPUT EXCHANGE:
- Project: {project}
- Timestamp: {timestamp}
- Source agent: {source_agent}
- Working directory: {cwd}
- Git branch: {git_branch}
- Tools used: {tools_used}
- Tool calls: {tool_count} (errors: {tool_error_count})

USER MESSAGE:
{user_message}

ASSISTANT RESPONSE:
{assistant_message}

RESPOND WITH ONLY VALID JSON (no markdown fencing, no explanation, no commentary):
{{
  "summary": "1-2 sentence summary of what happened",
  "category": "one of: setup, bugfix, feature, refactor, config, debug, research, deployment, documentation, maintenance, conversation",
  "topics": ["3-5 key technical topics"],
  "technologies": ["specific technologies/tools/frameworks mentioned"],
  "outcome": "one of: success, partial, failure, ongoing, informational",
  "intent": "what the user wanted in 5-10 words",
  "complexity": "one of: trivial, simple, moderate, complex, very_complex",
  "key_files": ["specific file paths mentioned or worked on"],
  "searchable_text": "dense paragraph combining all key concepts, terms, technologies, and context for semantic search"
}}"""


def call_kimi(prompt: str, retries: int = KIMI_MAX_RETRIES) -> Optional[str]:
    """Call Kimi CLI and return the output text."""
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                [KIMI_CMD, "--print", "--final-message-only", "--output-format", "text",
                 "--no-thinking", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=KIMI_TIMEOUT,
            )
            output = result.stdout.strip()
            if output:
                return output
            if result.returncode != 0:
                log.warning(f"Kimi returned code {result.returncode}: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            log.warning(f"Kimi timed out (attempt {attempt + 1}/{retries + 1})")
        except FileNotFoundError:
            log.error(f"Kimi CLI not found at: {KIMI_CMD}")
            return None

        if attempt < retries:
            time.sleep(2 ** attempt)  # exponential backoff

    return None


def parse_kimi_json(raw: str) -> Optional[dict]:
    """Extract JSON from Kimi's response (handles markdown fencing)."""
    # Strip markdown code fencing if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove first line (```json or ```) and last line (```)
        lines = cleaned.split("\n")
        lines = lines[1:]  # remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # remove closing fence
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def enrich_exchange(raw: RawExchange) -> EnrichedMetadata:
    """Send an exchange to Kimi for metadata enrichment."""
    prompt = ENRICHMENT_PROMPT_TEMPLATE.format(
        project=raw.project,
        timestamp=raw.timestamp,
        source_agent=detect_source_agent(raw.archive_path),
        cwd=raw.cwd or "(unknown)",
        git_branch=raw.git_branch or "(none)",
        tools_used=", ".join(raw.tool_names) if raw.tool_names else "(none)",
        tool_count=raw.tool_count,
        tool_error_count=raw.tool_error_count,
        user_message=clean_message(raw.user_message, MAX_USER_MSG_CHARS),
        assistant_message=clean_message(raw.assistant_message, MAX_ASST_MSG_CHARS),
    )

    output = call_kimi(prompt)
    if not output:
        log.warning(f"No output from Kimi for exchange {raw.id[:12]}")
        return EnrichedMetadata()

    data = parse_kimi_json(output)
    if not data:
        log.warning(f"Failed to parse Kimi JSON for exchange {raw.id[:12]}: {output[:200]}")
        return EnrichedMetadata()

    return EnrichedMetadata(
        summary=data.get("summary", ""),
        category=data.get("category", ""),
        topics=data.get("topics", []),
        technologies=data.get("technologies", []),
        outcome=data.get("outcome", ""),
        intent=data.get("intent", ""),
        complexity=data.get("complexity", ""),
        key_files=data.get("key_files", []),
        searchable_text=data.get("searchable_text", ""),
    )


def generate_basic_metadata(raw: RawExchange) -> EnrichedMetadata:
    """Generate basic metadata without Kimi (for --skip-enrichment mode)."""
    user_clean = clean_message(raw.user_message, 200)
    asst_clean = clean_message(raw.assistant_message, 200)

    # Basic category detection from tool usage
    category = "conversation"
    tools_set = set(raw.tool_names)
    if {"Write", "Edit"} & tools_set:
        category = "feature"
    if raw.tool_error_count > 0:
        category = "debug"
    if "npm" in raw.assistant_message.lower() or "pip" in raw.assistant_message.lower():
        category = "setup"

    return EnrichedMetadata(
        summary=f"User: {user_clean[:100]}",
        category=category,
        topics=[],
        technologies=list(raw.tool_names)[:5],
        outcome="informational",
        intent=user_clean[:50],
        complexity="moderate" if raw.tool_count > 5 else "simple",
        key_files=[],
        searchable_text=f"{user_clean} {asst_clean} {' '.join(raw.tool_names)}",
    )


# ---------------------------------------------------------------------------
# Embedding Content Assembly
# ---------------------------------------------------------------------------

def build_embedding_content(raw: RawExchange, meta: EnrichedMetadata) -> str:
    """
    Build the text that will be embedded by KnowledgeForge's nomic-embed.

    This is the KEY VALUE of the enrichment -- instead of just embedding
    the raw user+assistant text, we embed a semantically rich document
    that includes the AI-generated summary, topics, intent, and searchable text.
    """
    parts = []

    # Summary and intent (most important for search)
    if meta.summary:
        parts.append(f"Summary: {meta.summary}")
    if meta.intent:
        parts.append(f"Intent: {meta.intent}")

    # Category and topics
    if meta.category:
        parts.append(f"Category: {meta.category}")
    if meta.topics:
        parts.append(f"Topics: {', '.join(meta.topics)}")
    if meta.technologies:
        parts.append(f"Technologies: {', '.join(meta.technologies)}")

    # Core messages (truncated)
    user_clean = clean_message(raw.user_message, 800)
    asst_clean = clean_message(raw.assistant_message, 800)
    parts.append(f"User: {user_clean}")
    parts.append(f"Assistant: {asst_clean}")

    # Tools context
    if raw.tool_names:
        parts.append(f"Tools: {', '.join(raw.tool_names)}")

    # Searchable text enrichment
    if meta.searchable_text:
        parts.append(f"Context: {meta.searchable_text}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output & Checkpointing
# ---------------------------------------------------------------------------

def load_checkpoint() -> set:
    """Load set of already-processed exchange IDs."""
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        return set(data.get("processed_ids", []))
    return set()


def save_checkpoint(processed_ids: set, stats: dict):
    """Save checkpoint with processed IDs and stats."""
    CHECKPOINT_FILE.write_text(json.dumps({
        "processed_ids": list(processed_ids),
        "last_updated": datetime.now().isoformat(),
        "stats": stats,
    }, indent=2))

    STATS_FILE.write_text(json.dumps(stats, indent=2))


def save_enriched(enriched: EnrichedExchange):
    """Save a single enriched exchange as a JSON file."""
    output_path = OUTPUT_DIR / f"{enriched.exchange_id}.json"
    output_path.write_text(json.dumps(asdict(enriched), indent=2, default=str))


def log_error(exchange_id: str, error: str):
    """Append error to errors file."""
    with open(ERRORS_FILE, "a") as f:
        f.write(json.dumps({"id": exchange_id, "error": error, "ts": datetime.now().isoformat()}) + "\n")


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    dry_run: bool = False,
    resume: bool = False,
    batch_size: int = 0,
    skip_enrichment: bool = False,
    specific_ids: Optional[list] = None,
):
    """Main enrichment pipeline."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load data
    exchanges = read_all_exchanges()
    if not exchanges:
        log.error("No exchanges found")
        return

    # Resume support
    processed_ids = load_checkpoint() if resume else set()
    if processed_ids:
        log.info(f"Resuming: {len(processed_ids)} already processed, {len(exchanges) - len(processed_ids)} remaining")

    # Filter
    if specific_ids:
        exchanges = [e for e in exchanges if e.id in specific_ids]
        log.info(f"Filtered to {len(exchanges)} specific exchanges")

    pending = [e for e in exchanges if e.id not in processed_ids]
    if batch_size > 0:
        pending = pending[:batch_size]
        log.info(f"Batch mode: processing {len(pending)} exchanges")

    # Stats
    stats = {
        "total_exchanges": len(exchanges),
        "already_processed": len(processed_ids),
        "to_process": len(pending),
        "enriched": 0,
        "basic_metadata": 0,
        "errors": 0,
        "started_at": datetime.now().isoformat(),
        "enrichment_model": "none" if skip_enrichment else "kimi-k2.5",
    }

    if dry_run:
        log.info("=== DRY RUN ===")
        log.info(f"Would process {len(pending)} exchanges")
        log.info(f"Sample exchange: {pending[0].id[:12]} | {pending[0].project} | {pending[0].timestamp}")
        log.info(f"User msg length: {len(pending[0].user_message)} chars")
        log.info(f"Asst msg length: {len(pending[0].assistant_message)} chars")
        log.info(f"Tools: {', '.join(pending[0].tool_names)}")
        for i, ex in enumerate(pending[:5]):
            log.info(f"  [{i+1}] {ex.id[:12]} | {ex.project} | {ex.timestamp[:10]} | {len(ex.user_message)}c user | {len(ex.assistant_message)}c asst | {ex.tool_count} tools")
        return

    # Process
    for i, raw in enumerate(pending):
        exchange_label = f"[{i+1}/{len(pending)}] {raw.id[:12]} | {raw.project} | {raw.timestamp[:10]}"

        try:
            # Enrich
            if skip_enrichment:
                meta = generate_basic_metadata(raw)
                stats["basic_metadata"] += 1
                log.info(f"{exchange_label} -> basic metadata")
            else:
                log.info(f"{exchange_label} -> calling Kimi...")
                meta = enrich_exchange(raw)
                if meta.summary:
                    stats["enriched"] += 1
                    log.info(f"  OK: {meta.category} | {meta.outcome} | {meta.summary[:80]}")
                else:
                    # Fallback to basic
                    meta = generate_basic_metadata(raw)
                    stats["basic_metadata"] += 1
                    log.warning(f"  Fallback to basic metadata")

            # Build enriched record
            enriched = EnrichedExchange(
                exchange_id=raw.id,
                project=raw.project,
                timestamp=raw.timestamp,
                user_message=raw.user_message,
                assistant_message=raw.assistant_message,
                session_id=raw.session_id,
                archive_path=raw.archive_path,
                line_start=raw.line_start,
                line_end=raw.line_end,
                cwd=raw.cwd,
                git_branch=raw.git_branch,
                claude_version=raw.claude_version,
                thinking_level=raw.thinking_level,
                is_sidechain=raw.is_sidechain,
                parent_uuid=raw.parent_uuid,
                source_agent=detect_source_agent(raw.archive_path),
                tool_names=",".join(raw.tool_names),
                tool_count=raw.tool_count,
                tool_error_count=raw.tool_error_count,
                summary=meta.summary,
                category=meta.category,
                topics=",".join(meta.topics) if meta.topics else "",
                technologies=",".join(meta.technologies) if meta.technologies else "",
                outcome=meta.outcome,
                intent=meta.intent,
                complexity=meta.complexity,
                key_files=",".join(meta.key_files) if meta.key_files else "",
                searchable_text=meta.searchable_text,
                embedding_content=build_embedding_content(raw, meta),
                enriched_at=datetime.now().isoformat(),
                enrichment_model="none" if skip_enrichment else "kimi-k2.5",
            )

            save_enriched(enriched)
            processed_ids.add(raw.id)

            # Checkpoint every 10 exchanges
            if (i + 1) % 10 == 0:
                save_checkpoint(processed_ids, stats)
                log.info(f"  Checkpoint saved ({len(processed_ids)} total)")

            # Rate limiting for Kimi (be nice to the API)
            if not skip_enrichment:
                time.sleep(1)

        except Exception as e:
            stats["errors"] += 1
            log.error(f"{exchange_label} -> ERROR: {e}")
            log_error(raw.id, str(e))
            continue

    # Final save
    stats["completed_at"] = datetime.now().isoformat()
    save_checkpoint(processed_ids, stats)

    # Report
    log.info("=" * 60)
    log.info("ENRICHMENT COMPLETE")
    log.info(f"  Total exchanges:    {stats['total_exchanges']}")
    log.info(f"  Enriched (Kimi):    {stats['enriched']}")
    log.info(f"  Basic metadata:     {stats['basic_metadata']}")
    log.info(f"  Errors:             {stats['errors']}")
    log.info(f"  Output directory:   {OUTPUT_DIR}")
    log.info(f"  Stats file:         {STATS_FILE}")
    if stats["errors"] > 0:
        log.info(f"  Error log:          {ERRORS_FILE}")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Enrich episodic memory exchanges with Kimi-generated metadata"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be processed without calling Kimi")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint (skip already-processed exchanges)")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Process only N exchanges (0 = all)")
    parser.add_argument("--skip-enrichment", action="store_true",
                        help="Generate basic metadata without calling Kimi")
    parser.add_argument("--ids", nargs="+",
                        help="Process only specific exchange IDs")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run_pipeline(
        dry_run=args.dry_run,
        resume=args.resume,
        batch_size=args.batch_size,
        skip_enrichment=args.skip_enrichment,
        specific_ids=args.ids,
    )


if __name__ == "__main__":
    main()
