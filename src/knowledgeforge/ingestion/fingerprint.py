"""SHA-256 content fingerprinting for deduplication.

Ported from OB1's content fingerprinting approach. Used across
KnowledgeForge ingestion to detect duplicate content.
"""

import hashlib


def content_fingerprint(content: str) -> str:
    """Generate SHA-256 fingerprint for content deduplication.

    Normalizes content before hashing:
    - Strip leading/trailing whitespace
    - Normalize line endings to \\n
    - Collapse multiple blank lines to single
    """
    normalized = content.strip()
    normalized = normalized.replace('\r\n', '\n')
    # Collapse multiple blank lines
    while '\n\n\n' in normalized:
        normalized = normalized.replace('\n\n\n', '\n\n')
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def check_duplicate(fingerprint: str, existing_fingerprints: set[str]) -> bool:
    """Check if content fingerprint already exists."""
    return fingerprint in existing_fingerprints


def fingerprint_batch(contents: list[str]) -> dict[int, str]:
    """Generate fingerprints for a batch of content strings.

    Returns dict mapping index to fingerprint.
    """
    return {i: content_fingerprint(c) for i, c in enumerate(contents)}
