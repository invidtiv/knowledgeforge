"""Common token-based chunking utilities for ingestion pipelines."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

# Hybrid-search chunking defaults requested by design.
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_TOKENIZER_MODEL = "nomic-ai/nomic-embed-text-v1.5"


@dataclass
class TokenChunk:
    """A token-window chunk and its source line span."""

    text: str
    start_line: int
    end_line: int
    token_count: int


@lru_cache(maxsize=4)
def _get_tokenizer(model_name: str) -> object | None:
    """Load and cache a HuggingFace tokenizer."""
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(
            model_name,
            trust_remote_code=True,
            use_fast=True,
        )
    except Exception as exc:  # pragma: no cover - fallback path
        logger.warning("Tokenizer load failed for %s: %s", model_name, exc)
        return None


def _tokenizer_model_name(tokenizer_model: str | None = None) -> str:
    return (
        tokenizer_model
        or os.getenv("KNOWLEDGEFORGE_TOKENIZER_MODEL")
        or DEFAULT_TOKENIZER_MODEL
    )


def count_tokens(text: str, tokenizer_model: str | None = None) -> int:
    """Count tokens with sentence-transformers-compatible tokenizer."""
    model_name = _tokenizer_model_name(tokenizer_model)
    tokenizer = _get_tokenizer(model_name)
    if tokenizer is None:
        # conservative fallback when tokenizer cannot be loaded
        return max(1, len(text.split()))

    try:
        token_ids = tokenizer.encode(text, add_special_tokens=False)
        return len(token_ids)
    except Exception as exc:  # pragma: no cover - fallback path
        logger.warning("Token counting failed with %s: %s", model_name, exc)
        return max(1, len(text.split()))


def split_by_tokens_with_lines(
    text: str,
    max_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    start_line: int = 1,
    tokenizer_model: str | None = None,
) -> list[TokenChunk]:
    """Split text into token windows while tracking source line ranges.

    Notes:
    - Uses line boundaries as the primary split unit to keep line mapping exact.
    - If a single line exceeds max_size, it is token-split and keeps the same
      start/end line number.
    """
    if not text.strip():
        return []

    model_name = _tokenizer_model_name(tokenizer_model)
    tokenizer = _get_tokenizer(model_name)
    if tokenizer is None:
        return _fallback_split_by_words(text, max_size, overlap, start_line)

    lines = text.splitlines()
    if not lines:
        lines = [text]

    line_token_counts = [
        len(tokenizer.encode(line, add_special_tokens=False)) for line in lines
    ]

    chunks: list[TokenChunk] = []
    cursor = 0
    total_lines = len(lines)

    while cursor < total_lines:
        chunk_start = cursor
        token_total = 0

        while cursor < total_lines:
            line_tokens = line_token_counts[cursor]

            # Handle pathological long single-line content.
            if line_tokens > max_size and token_total == 0:
                line_chunks = _split_single_line_by_tokens(
                    lines[cursor],
                    tokenizer=tokenizer,
                    max_size=max_size,
                    overlap=overlap,
                )
                for part in line_chunks:
                    chunks.append(
                        TokenChunk(
                            text=part,
                            start_line=start_line + cursor,
                            end_line=start_line + cursor,
                            token_count=count_tokens(part, model_name),
                        )
                    )
                cursor += 1
                break

            if token_total > 0 and (token_total + line_tokens) > max_size:
                break

            token_total += line_tokens
            cursor += 1

            if token_total >= max_size:
                break

        if chunk_start < cursor:
            chunk_lines = lines[chunk_start:cursor]
            chunk_text = "\n".join(chunk_lines).strip("\n")
            if chunk_text.strip():
                chunks.append(
                    TokenChunk(
                        text=chunk_text,
                        start_line=start_line + chunk_start,
                        end_line=start_line + cursor - 1,
                        token_count=count_tokens(chunk_text, model_name),
                    )
                )

        if cursor >= total_lines:
            break

        # Build line-level overlap for next window.
        overlap_tokens = 0
        overlap_start = cursor
        i = cursor - 1
        while i >= chunk_start:
            lt = line_token_counts[i]
            if overlap_tokens > 0 and (overlap_tokens + lt) > overlap:
                break
            overlap_tokens += lt
            overlap_start = i
            if overlap_tokens >= overlap:
                break
            i -= 1

        if overlap_start == cursor:
            # Guarantee forward progress in degenerate cases.
            overlap_start = max(chunk_start, cursor - 1)
        cursor = overlap_start

    return [c for c in chunks if c.text.strip()]


def split_by_tokens(
    text: str,
    max_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    tokenizer_model: str | None = None,
) -> list[str]:
    """Backward-compatible splitter returning text chunks only."""
    return [
        c.text
        for c in split_by_tokens_with_lines(
            text=text,
            max_size=max_size,
            overlap=overlap,
            start_line=1,
            tokenizer_model=tokenizer_model,
        )
    ]


def _split_single_line_by_tokens(
    line: str,
    tokenizer: object,
    max_size: int,
    overlap: int,
) -> list[str]:
    token_ids = tokenizer.encode(line, add_special_tokens=False)
    if len(token_ids) <= max_size:
        return [line]

    pieces: list[str] = []
    step = max(1, max_size - overlap)
    for i in range(0, len(token_ids), step):
        window = token_ids[i : i + max_size]
        if not window:
            continue
        text = tokenizer.decode(window, skip_special_tokens=True).strip()
        if text:
            pieces.append(text)
        if i + max_size >= len(token_ids):
            break
    return pieces


def _fallback_split_by_words(
    text: str,
    max_size: int,
    overlap: int,
    start_line: int,
) -> list[TokenChunk]:
    """Fallback when tokenizer cannot be loaded."""
    lines = text.splitlines()
    if not lines:
        lines = [text]

    chunks: list[TokenChunk] = []
    cursor = 0
    while cursor < len(lines):
        words = 0
        begin = cursor
        while cursor < len(lines):
            next_words = len(lines[cursor].split())
            if words > 0 and (words + next_words) > max_size:
                break
            words += next_words
            cursor += 1

        chunk_text = "\n".join(lines[begin:cursor]).strip("\n")
        if chunk_text.strip():
            chunks.append(
                TokenChunk(
                    text=chunk_text,
                    start_line=start_line + begin,
                    end_line=start_line + cursor - 1,
                    token_count=max(1, words),
                )
            )

        if cursor >= len(lines):
            break

        overlap_words = 0
        overlap_start = cursor
        i = cursor - 1
        while i >= begin:
            nw = len(lines[i].split())
            if overlap_words > 0 and (overlap_words + nw) > overlap:
                break
            overlap_words += nw
            overlap_start = i
            if overlap_words >= overlap:
                break
            i -= 1
        cursor = overlap_start

    return chunks


def merge_small_chunks(chunks: list[str], min_size: int = 50) -> list[str]:
    """Merge chunks smaller than min_size tokens into their neighbors."""
    if not chunks:
        return []

    merged: list[str] = []
    buffer = ""

    for chunk in chunks:
        if buffer:
            combined = buffer + "\n\n" + chunk
            buffer = ""
            if count_tokens(combined) < min_size:
                buffer = combined
            else:
                merged.append(combined)
        elif count_tokens(chunk) < min_size:
            buffer = chunk
        else:
            merged.append(chunk)

    if buffer:
        if merged:
            merged[-1] = merged[-1] + "\n\n" + buffer
        else:
            merged.append(buffer)

    return merged


def compute_file_hash(file_path: str) -> str:
    """Compute SHA256 hash of file contents for change detection."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()


def generate_chunk_id(source_file: str, chunk_index: int) -> str:
    """Generate deterministic chunk ID for upserts."""
    raw = f"{source_file}::{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
