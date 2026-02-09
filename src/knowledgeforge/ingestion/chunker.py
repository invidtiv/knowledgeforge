"""
Common chunking utilities used by both Obsidian and code parsers.
"""
import hashlib
from pathlib import Path


def count_tokens(text: str) -> int:
    """Approximate token count using whitespace splitting.
    Approximation: 1 token ≈ 0.75 words.
    """
    words = len(text.split())
    return int(words / 0.75)


def split_by_tokens(text: str, max_size: int = 1000, overlap: int = 100) -> list[str]:
    """Split text into chunks of approximately max_size tokens with overlap.

    Strategy:
    1. Split by paragraphs (double newline)
    2. Accumulate paragraphs until max_size is reached
    3. When a chunk is full, start new chunk with overlap from previous
    4. Never split mid-paragraph if possible
    5. If a single paragraph exceeds max_size, split by sentences
    """
    if count_tokens(text) <= max_size:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = count_tokens(para)

        if para_tokens > max_size:
            # Paragraph too large, split by sentences
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_tokens = 0

            sentences = _split_sentences(para)
            sent_chunk = []
            sent_tokens = 0
            for sent in sentences:
                st = count_tokens(sent)
                if sent_tokens + st > max_size and sent_chunk:
                    chunks.append(" ".join(sent_chunk))
                    # Keep overlap
                    overlap_sents = []
                    overlap_tokens = 0
                    for s in reversed(sent_chunk):
                        t = count_tokens(s)
                        if overlap_tokens + t > overlap:
                            break
                        overlap_sents.insert(0, s)
                        overlap_tokens += t
                    sent_chunk = overlap_sents + [sent]
                    sent_tokens = overlap_tokens + st
                else:
                    sent_chunk.append(sent)
                    sent_tokens += st
            if sent_chunk:
                chunks.append(" ".join(sent_chunk))
            continue

        if current_tokens + para_tokens > max_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            # Keep overlap paragraphs
            overlap_paras = []
            overlap_tokens = 0
            for p in reversed(current_chunk):
                t = count_tokens(p)
                if overlap_tokens + t > overlap:
                    break
                overlap_paras.insert(0, p)
                overlap_tokens += t
            current_chunk = overlap_paras + [para]
            current_tokens = overlap_tokens + para_tokens
        else:
            current_chunk.append(para)
            current_tokens += para_tokens

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return [c for c in chunks if c.strip()]


def _split_sentences(text: str) -> list[str]:
    """Simple sentence splitting."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s for s in sentences if s.strip()]


def merge_small_chunks(chunks: list[str], min_size: int = 50) -> list[str]:
    """Merge chunks smaller than min_size tokens into their neighbors."""
    if not chunks:
        return []

    merged = []
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
    """Generate deterministic chunk ID for upserts.

    Uses hash of source_file + chunk_index for stable IDs.
    """
    raw = f"{source_file}::{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
