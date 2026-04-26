"""Batch prompt generation for historical conversation extraction."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Any

from knowledgeforge.ingestion.conversations import parse_jsonl_file
from knowledgeforge.ingestion.memory_extraction import build_conversation_extraction_prompt


HISTORICAL_SAFE_STATUSES = {
    "historical",
    "failed",
    "resolved",
    "open_unverified",
    "active_unverified",
    "superseded",
    "deprecated",
    "cancelled",
}


def build_prompt_batch(
    session_paths: Iterable[str | Path],
    output_dir: str | Path,
    limit: int = 20,
    max_chars: int = 60000,
) -> dict[str, Any]:
    """Write bounded extraction prompts and a manifest for JSONL sessions."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "generated_at": _utc_now(),
        "output_dir": str(output_path),
        "session_count": 0,
        "sessions": [],
    }

    for index, session_path in enumerate(list(session_paths)[: max(limit, 0)], start=1):
        source_path = Path(session_path)
        exchanges = parse_jsonl_file(str(source_path))
        session_id = source_path.stem
        prompt = build_conversation_extraction_prompt(
            exchanges,
            title=session_id,
            max_chars=max_chars,
        )
        prompt_path = output_path / f"{index:03d}-{_safe_filename(session_id)}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        manifest["sessions"].append(
            {
                "source_path": str(source_path),
                "session_id": session_id,
                "exchange_count": len(exchanges),
                "prompt_path": str(prompt_path),
                "status": "prompt_ready",
            }
        )

    manifest["session_count"] = len(manifest["sessions"])
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def normalize_extraction_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """Force extracted historical memory cards to low-trust defaults."""
    normalized = deepcopy(payload)
    cards = normalized.get("memory_cards")
    if not isinstance(cards, list):
        return normalized

    for card in cards:
        if not isinstance(card, dict):
            continue

        status = str(card.get("status") or "")
        if status not in HISTORICAL_SAFE_STATUSES:
            card["status"] = "active_unverified"
        card["current_truth"] = False
        card["needs_repo_confirmation"] = True

    return normalized


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value)
    return safe.strip("-") or "session"


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
