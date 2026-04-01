"""Parser for Claude Code JSONL conversation files.

Reads JSONL session files from Claude Code, Codex, and Gemini archives,
pairs human+assistant messages into exchanges, extracts tool usage,
and produces ConversationExchange objects ready for embedding.
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from knowledgeforge.core.models import ConversationExchange, ToolCallRecord
from knowledgeforge.ingestion.chunker import count_tokens, split_by_tokens

logger = logging.getLogger(__name__)

# Max chars to keep from tool results in embedding content
MAX_TOOL_RESULT_CHARS = 500


def detect_source_agent(path: str) -> str:
    """Detect which AI agent produced a conversation based on its archive path.

    Args:
        path: Path to the JSONL file

    Returns:
        "codex", "gemini", or "claude"
    """
    if "/_codex/" in path or "/_codex\\" in path:
        return "codex"
    if "/_gemini/" in path or "/_gemini\\" in path:
        return "gemini"
    return "claude"


def detect_project(path: str) -> str:
    """Extract project name from JSONL file path.

    Claude Code paths: ~/.claude/projects/{project}/{session}.jsonl
    Archive paths: ~/.config/superpowers/conversation-archive/{project}/{session}.jsonl

    Args:
        path: Path to JSONL file

    Returns:
        Project name string
    """
    parts = Path(path).parts

    # Look for "projects" or "conversation-archive" directory
    for i, part in enumerate(parts):
        if part in ("projects", "conversation-archive") and i + 1 < len(parts):
            return parts[i + 1]

    return "unknown"


def generate_exchange_id(archive_path: str, line_start: int, line_end: int) -> str:
    """Generate deterministic exchange ID from file path and line range.

    Args:
        archive_path: Path to source JSONL file
        line_start: Starting line number (1-indexed)
        line_end: Ending line number (1-indexed)

    Returns:
        SHA256 hex digest (first 32 chars)
    """
    raw = f"{archive_path}:{line_start}-{line_end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def clean_message(text: str, max_chars: int = 2000) -> str:
    """Clean a user or assistant message for embedding.

    Strips IDE tags, system reminders, and truncates.

    Args:
        text: Raw message text
        max_chars: Maximum characters to keep

    Returns:
        Cleaned text string
    """
    if not text:
        return ""

    # Strip common noise patterns
    text = re.sub(r'<ide_opened_file>.*?</ide_opened_file>', '', text, flags=re.DOTALL)
    text = re.sub(r'<ide_selection>.*?</ide_selection>', '', text, flags=re.DOTALL)
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.DOTALL)
    text = re.sub(r'<local-command-caveat>.*?</local-command-caveat>', '', text, flags=re.DOTALL)
    text = re.sub(r'<command-name>.*?</command-name>', '', text, flags=re.DOTALL)
    text = re.sub(r'<command-message>.*?</command-message>', '', text, flags=re.DOTALL)
    text = re.sub(r'<command-args>.*?</command-args>', '', text, flags=re.DOTALL)
    text = re.sub(r'<local-command-stdout>.*?</local-command-stdout>', '', text, flags=re.DOTALL)

    # Collapse whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text[:max_chars]


def _extract_text_from_content(content) -> str:
    """Extract plain text from assistant message content blocks.

    Assistant content can be a string or a list of content blocks.
    We extract text blocks and skip thinking/tool_use blocks.

    Args:
        content: Message content (str or list of dicts)

    Returns:
        Concatenated text content
    """
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return str(content)

    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)

    return "\n".join(parts)


def _extract_tool_uses(content) -> list[dict]:
    """Extract tool_use blocks from assistant message content.

    Args:
        content: Message content (str or list of dicts)

    Returns:
        List of dicts with tool_name and tool_input
    """
    if not isinstance(content, list):
        return []

    tools = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_input = block.get("input", {})
            if isinstance(tool_input, dict):
                # Compact representation
                input_str = json.dumps(tool_input, separators=(",", ":"))[:500]
            else:
                input_str = str(tool_input)[:500]
            tools.append({
                "id": block.get("id", ""),
                "name": block.get("name", "unknown"),
                "input": input_str,
            })

    return tools


def _extract_tool_result(line_data: dict) -> tuple[str, str, bool]:
    """Extract tool result from a tool_result line.

    Args:
        line_data: Parsed JSON line

    Returns:
        Tuple of (tool_use_id, result_text, is_error)
    """
    tool_use_id = line_data.get("tool_use_id", "")
    is_error = line_data.get("is_error", False)

    content = line_data.get("content", "")
    if isinstance(content, list):
        # Content can be a list of blocks
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        content = "\n".join(parts)

    # Strip <result> tags
    content = re.sub(r'^<result>\n?', '', str(content))
    content = re.sub(r'\n?</result>$', '', content)

    return tool_use_id, content[:MAX_TOOL_RESULT_CHARS], is_error


def parse_jsonl_file(
    file_path: str,
    enrichment_map: Optional[dict] = None,
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
) -> list[ConversationExchange]:
    """Parse a single JSONL conversation file into exchanges.

    Reads line by line, pairs human+assistant messages, extracts tool usage,
    and produces ConversationExchange objects.

    Args:
        file_path: Absolute path to JSONL file
        enrichment_map: Optional dict mapping exchange_id -> enrichment dict
        max_tool_result_chars: Max chars to keep from tool results

    Returns:
        List of ConversationExchange objects
    """
    if not os.path.isfile(file_path):
        logger.warning(f"JSONL file not found: {file_path}")
        return []

    source_agent = detect_source_agent(file_path)
    project = detect_project(file_path)
    enrichment_map = enrichment_map or {}

    # Read all lines
    lines = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line_num, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
                data["_line_num"] = line_num
                lines.append(data)
            except json.JSONDecodeError:
                logger.debug(f"Skipping malformed JSON at {file_path}:{line_num}")

    if not lines:
        return []

    # Extract session_id from the first message that has one
    session_id = ""
    for line in lines:
        if line.get("sessionId"):
            session_id = line["sessionId"]
            break

    # Build exchanges by pairing human + assistant messages
    exchanges = []
    i = 0
    while i < len(lines):
        line = lines[i]
        msg_type = line.get("type", "")

        # Look for "user" type messages
        if msg_type != "user":
            i += 1
            continue

        user_line_num = line["_line_num"]
        user_msg = line.get("message", {})
        user_content = user_msg.get("content", "")
        user_text = _extract_text_from_content(user_content)

        # Extract metadata from user message
        cwd = line.get("cwd", "")
        git_branch = line.get("gitBranch", "")
        version = line.get("version", "")
        timestamp = line.get("timestamp", "")
        is_sidechain = line.get("isSidechain", False)
        parent_uuid = line.get("parentUuid", "") or ""

        # Collect subsequent assistant messages and tool results
        assistant_texts = []
        tool_uses = []  # {id, name, input}
        tool_results = {}  # id -> (result, is_error)
        thinking_level = ""
        last_line_num = user_line_num

        j = i + 1
        while j < len(lines):
            next_line = lines[j]
            next_type = next_line.get("type", "")

            if next_type == "user":
                # Next exchange starts
                break
            elif next_type == "assistant":
                last_line_num = next_line["_line_num"]
                msg = next_line.get("message", {})
                content = msg.get("content", [])

                # Extract text
                text = _extract_text_from_content(content)
                if text.strip():
                    assistant_texts.append(text)

                # Extract tool uses
                uses = _extract_tool_uses(content)
                tool_uses.extend(uses)

                # Check for thinking
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            thinking_level = "extended"

            elif next_type == "tool_result":
                last_line_num = next_line["_line_num"]
                tid, result, is_err = _extract_tool_result(next_line)
                tool_results[tid] = (result, is_err)
            else:
                # progress, system, queue-operation, etc. — skip
                last_line_num = max(last_line_num, next_line.get("_line_num", last_line_num))

            j += 1

        # Skip empty exchanges
        assistant_text = "\n".join(assistant_texts).strip()
        if not user_text.strip() and not assistant_text:
            i = j
            continue

        # Build tool call records
        tool_call_records = []
        tool_name_set = []
        error_count = 0
        for tu in tool_uses:
            result_text = ""
            is_err = False
            if tu["id"] in tool_results:
                result_text, is_err = tool_results[tu["id"]]
            if is_err:
                error_count += 1
            tool_call_records.append(ToolCallRecord(
                tool_name=tu["name"],
                tool_input=tu["input"],
                tool_result=result_text[:max_tool_result_chars],
                is_error=is_err,
            ))
            if tu["name"] not in tool_name_set:
                tool_name_set.append(tu["name"])

        # Generate exchange ID
        exchange_id = generate_exchange_id(file_path, user_line_num, last_line_num)

        # Clean messages for embedding
        clean_user = clean_message(user_text, max_chars=2000)
        clean_assistant = clean_message(assistant_text, max_chars=3000)

        # Load enrichment if available
        enrichment = enrichment_map.get(exchange_id, {})

        exchange = ConversationExchange(
            exchange_id=exchange_id,
            session_id=session_id or os.path.splitext(os.path.basename(file_path))[0],
            project=project,
            timestamp=timestamp,
            user_message=clean_user,
            assistant_message=clean_assistant,
            source_agent=source_agent,
            archive_path=file_path,
            line_start=user_line_num,
            line_end=last_line_num,
            cwd=cwd,
            git_branch=git_branch,
            claude_version=version,
            thinking_level=thinking_level,
            tool_calls=tool_call_records,
            tool_names=tool_name_set,
            tool_error_count=error_count,
            is_sidechain=is_sidechain,
            parent_uuid=parent_uuid,
            enrichment=enrichment,
        )
        exchanges.append(exchange)

        i = j

    logger.debug(f"Parsed {len(exchanges)} exchanges from {file_path}")
    return exchanges


def scan_conversation_dirs(source_dirs: list[str]) -> list[str]:
    """Find all JSONL files across conversation source directories.

    Recursively scans directories for .jsonl files, skipping
    files in subagents/ directories (those are sub-agent sessions).

    Args:
        source_dirs: List of directory paths to scan

    Returns:
        Sorted list of absolute paths to JSONL files
    """
    jsonl_files = []
    for dir_path in source_dirs:
        dir_path = os.path.expanduser(dir_path)
        if not os.path.isdir(dir_path):
            logger.warning(f"Conversation source dir not found: {dir_path}")
            continue

        for root, dirs, files in os.walk(dir_path):
            # Skip subagent directories
            if "subagents" in root.split(os.sep):
                continue
            for fname in files:
                if fname.endswith(".jsonl"):
                    # Skip agent-* files (sub-agent sessions in projects dir)
                    if fname.startswith("agent-"):
                        continue
                    jsonl_files.append(os.path.join(root, fname))

    jsonl_files.sort()
    logger.info(f"Found {len(jsonl_files)} JSONL conversation files across {len(source_dirs)} dirs")
    return jsonl_files


def load_enrichment_data(enrichment_dir: str) -> dict:
    """Load Kimi-enriched metadata from JSON files.

    Creates mappings by both the original exchange_id (from episodic memory)
    and by archive_path+line_range (to match the new parser's IDs).

    Args:
        enrichment_dir: Directory containing per-exchange JSON files

    Returns:
        Dict mapping exchange_id -> enrichment dict
    """
    enrichment_map = {}
    enrichment_dir = os.path.expanduser(enrichment_dir)

    if not os.path.isdir(enrichment_dir):
        logger.debug(f"Enrichment directory not found: {enrichment_dir}")
        return enrichment_map

    for fname in os.listdir(enrichment_dir):
        if fname.startswith("_") or not fname.endswith(".json"):
            continue
        try:
            fpath = os.path.join(enrichment_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)

            enrichment = {
                "summary": data.get("summary", ""),
                "category": data.get("category", ""),
                "topics": data.get("topics", ""),
                "technologies": data.get("technologies", ""),
                "outcome": data.get("outcome", ""),
                "intent": data.get("intent", ""),
                "complexity": data.get("complexity", ""),
                "key_files": data.get("key_files", ""),
                "searchable_text": data.get("searchable_text", ""),
            }

            # Map by original exchange_id
            eid = data.get("exchange_id", "")
            if eid:
                enrichment_map[eid] = enrichment

            # Also map by archive_path + line range (new-style ID)
            archive_path = data.get("archive_path", "")
            line_start = data.get("line_start", 0)
            line_end = data.get("line_end", 0)
            if archive_path and line_start:
                new_id = generate_exchange_id(archive_path, line_start, line_end)
                enrichment_map[new_id] = enrichment

        except Exception as e:
            logger.warning(f"Failed to load enrichment file {fname}: {e}")

    logger.info(f"Loaded {len(enrichment_map)} enrichment records")
    return enrichment_map


def chunk_exchange(
    exchange: ConversationExchange,
    max_tokens: int = 2000,
    overlap_tokens: int = 100
) -> list[tuple[str, str, dict]]:
    """Chunk an exchange into (id, content, metadata) tuples for ChromaDB.

    If the embedding content fits in max_tokens, returns a single chunk.
    Otherwise splits into sub-chunks with overlap.

    Args:
        exchange: ConversationExchange to chunk
        max_tokens: Max tokens per chunk
        overlap_tokens: Token overlap between chunks

    Returns:
        List of (chunk_id, content, metadata) tuples
    """
    content = exchange.build_embedding_content()
    metadata = exchange.to_metadata()

    if count_tokens(content) <= max_tokens:
        return [(exchange.exchange_id, content, metadata)]

    # Split into sub-chunks
    sub_texts = split_by_tokens(content, max_tokens, overlap_tokens)
    chunks = []
    for idx, sub_text in enumerate(sub_texts):
        chunk_id = f"{exchange.exchange_id}_{idx}"
        chunk_meta = dict(metadata)
        chunk_meta["chunk_index"] = idx
        chunks.append((chunk_id, sub_text, chunk_meta))

    return chunks
