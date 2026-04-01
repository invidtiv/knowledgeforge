#!/usr/bin/env python3
"""Presidio-based secret scrubber for KnowledgeForge ChromaDB collections.

This worker is intentionally non-LLM and regex-driven for fast execution.
It scans document text (and optionally string metadata values), detects likely
API keys/tokens via Microsoft Presidio recognizers, and redacts matches.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chromadb
from presidio_analyzer import Pattern, PatternRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

# Keep subprocess conservative in constrained VPS environments.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from knowledgeforge.config import KnowledgeForgeConfig  # noqa: E402


logger = logging.getLogger("knowledgeforge.presidio_scrub")


@dataclass
class ScrubStats:
    collections_total: int = 0
    collections_processed: int = 0
    collections_skipped: int = 0
    records_scanned: int = 0
    records_changed: int = 0
    text_values_scanned: int = 0
    metadata_values_scanned: int = 0
    metadata_values_changed: int = 0
    entities_found: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def add_entities(self, counts: dict[str, int]) -> None:
        for name, value in counts.items():
            self.entity_counts[name] = self.entity_counts.get(name, 0) + int(value)


def build_recognizers() -> list[PatternRecognizer]:
    """Create Presidio recognizers focused on API keys and auth tokens."""
    return [
        PatternRecognizer(
            supported_entity="OPENAI_API_KEY",
            patterns=[
                Pattern(name="openai_sk", regex=r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b", score=0.75),
            ],
        ),
        PatternRecognizer(
            supported_entity="GITHUB_TOKEN",
            patterns=[
                Pattern(name="gh_token", regex=r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", score=0.75),
            ],
        ),
        PatternRecognizer(
            supported_entity="AWS_ACCESS_KEY_ID",
            patterns=[
                Pattern(name="aws_akid", regex=r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", score=0.72),
            ],
        ),
        PatternRecognizer(
            supported_entity="SLACK_TOKEN",
            patterns=[
                Pattern(name="slack_token", regex=r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", score=0.7),
            ],
        ),
        PatternRecognizer(
            supported_entity="JWT_TOKEN",
            patterns=[
                Pattern(
                    name="jwt",
                    regex=r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                    score=0.65,
                ),
            ],
        ),
        PatternRecognizer(
            supported_entity="GENERIC_API_TOKEN",
            patterns=[
                Pattern(
                    name="generic_assignment",
                    regex=(
                        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|bearer)\b"
                        r"\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{10,}['\"]?"
                    ),
                    score=0.6,
                ),
            ],
            context=["api", "token", "secret", "auth", "bearer", "key"],
        ),
    ]


def build_operators(recognizers: list[PatternRecognizer]) -> dict[str, OperatorConfig]:
    operators: dict[str, OperatorConfig] = {
        "DEFAULT": OperatorConfig("replace", {"new_value": "<REDACTED_SECRET>"})
    }
    for recognizer in recognizers:
        entity_name = recognizer.supported_entities[0]
        operators[entity_name] = OperatorConfig("replace", {"new_value": f"<REDACTED_{entity_name}>"})
    return operators


def filter_overlaps(results: list[Any]) -> list[Any]:
    """Keep non-overlapping recognizer spans, preferring higher confidence."""
    ordered = sorted(
        results,
        key=lambda item: (
            int(getattr(item, "start", 0)),
            -(int(getattr(item, "end", 0)) - int(getattr(item, "start", 0))),
            -(float(getattr(item, "score", 0.0)) if getattr(item, "score", None) is not None else 0.0),
        ),
    )

    kept: list[Any] = []
    for candidate in ordered:
        c_start = int(getattr(candidate, "start", 0))
        c_end = int(getattr(candidate, "end", 0))
        if c_start >= c_end:
            continue

        overlaps = any(
            c_start < int(getattr(existing, "end", 0)) and c_end > int(getattr(existing, "start", 0))
            for existing in kept
        )
        if overlaps:
            continue
        kept.append(candidate)

    return kept


def analyze_text(text: str, recognizers: list[PatternRecognizer]) -> list[Any]:
    if not text:
        return []

    findings: list[Any] = []
    for recognizer in recognizers:
        entity_name = recognizer.supported_entities[0]
        matches = recognizer.analyze(text=text, entities=[entity_name], nlp_artifacts=None)
        findings.extend(matches)

    return filter_overlaps(findings)


def redact_text(
    text: str,
    recognizers: list[PatternRecognizer],
    anonymizer: AnonymizerEngine,
    operators: dict[str, OperatorConfig],
) -> tuple[str, int, dict[str, int]]:
    findings = analyze_text(text, recognizers)
    if not findings:
        return text, 0, {}

    anonymized = anonymizer.anonymize(
        text=text,
        analyzer_results=findings,
        operators=operators,
    )

    per_entity: dict[str, int] = {}
    for item in findings:
        name = str(getattr(item, "entity_type", "UNKNOWN"))
        per_entity[name] = per_entity.get(name, 0) + 1

    return anonymized.text, len(findings), per_entity


def normalize_requested_collections(config: KnowledgeForgeConfig, requested: list[str]) -> list[str]:
    alias_map = {
        "documents": config.docs_collection,
        "docs": config.docs_collection,
        "codebase": config.code_collection,
        "code": config.code_collection,
        "discoveries": config.discoveries_collection,
        "conversations": config.conversations_collection,
    }

    if not requested:
        requested = ["documents", "codebase", "discoveries", "conversations"]

    resolved: list[str] = []
    for raw_name in requested:
        clean = str(raw_name or "").strip()
        if not clean:
            continue
        resolved_name = alias_map.get(clean, clean)
        if resolved_name not in resolved:
            resolved.append(resolved_name)

    return resolved


def normalize_embedding(raw_embedding: Any) -> list[float] | None:
    """Normalize Chroma embedding payload into a plain list of floats."""
    if raw_embedding is None:
        return None

    if hasattr(raw_embedding, "tolist"):
        raw_embedding = raw_embedding.tolist()

    if not isinstance(raw_embedding, list):
        return None

    try:
        return [float(value) for value in raw_embedding]
    except Exception:
        return None


def get_collection_batch(collection: Any, limit: int, offset: int) -> dict[str, Any]:
    include = ["documents", "metadatas"]
    try:
        return collection.get(limit=limit, offset=offset, include=include)
    except Exception as exc:
        if hasattr(collection, "_client") and hasattr(collection, "id"):
            logger.warning(
                "collection.get failed for %s (offset=%s): %s; retrying via raw client _get",
                getattr(collection, "name", "<unknown>"),
                offset,
                exc,
            )
            return collection._client._get(
                collection_id=collection.id,
                limit=limit,
                offset=offset,
                include=include,
            )
        raise


def get_embedding_for_id(collection: Any, chunk_id: str) -> list[float] | None:
    """Fetch a single embedding by id, using raw client fallback when needed."""
    try:
        batch = collection.get(ids=[chunk_id], include=["embeddings"])
        embeddings = batch.get("embeddings") or []
        if embeddings:
            normalized = normalize_embedding(embeddings[0])
            if normalized is not None:
                return normalized
    except Exception:
        pass

    try:
        if hasattr(collection, "_client") and hasattr(collection, "id"):
            batch = collection._client._get(
                collection_id=collection.id,
                ids=[chunk_id],
                include=["embeddings"],
            )
            embeddings = batch.get("embeddings") or []
            if embeddings:
                return normalize_embedding(embeddings[0])
    except Exception:
        pass

    return None


def scrub_collection(
    collection: Any,
    recognizers: list[PatternRecognizer],
    anonymizer: AnonymizerEngine,
    operators: dict[str, OperatorConfig],
    include_metadata: bool,
    dry_run: bool,
    batch_size: int,
    stats: ScrubStats,
) -> None:
    total = int(collection.count())
    if total <= 0:
        return

    offset = 0
    while offset < total:
        batch = get_collection_batch(collection=collection, limit=batch_size, offset=offset)
        raw_ids = batch.get("ids")
        raw_docs = batch.get("documents")
        raw_metadatas = batch.get("metadatas")

        ids = list(raw_ids) if raw_ids is not None else []
        docs = list(raw_docs) if raw_docs is not None else []
        metadatas = list(raw_metadatas) if raw_metadatas is not None else []

        if not ids:
            break

        doc_update_ids: list[str] = []
        doc_update_docs: list[str] = []
        doc_update_embeddings: list[list[float]] = []
        doc_update_metadatas: list[dict[str, Any]] = []
        metadata_update_ids: list[str] = []
        metadata_update_metadatas: list[dict[str, Any]] = []

        for index, chunk_id in enumerate(ids):
            original_doc = docs[index] if index < len(docs) and isinstance(docs[index], str) else ""
            original_metadata = metadatas[index] if index < len(metadatas) and isinstance(metadatas[index], dict) else {}

            stats.records_scanned += 1
            stats.text_values_scanned += 1

            redacted_doc, found_in_doc, per_entity_doc = redact_text(
                original_doc,
                recognizers,
                anonymizer,
                operators,
            )
            stats.entities_found += found_in_doc
            stats.add_entities(per_entity_doc)
            doc_changed = redacted_doc != original_doc

            metadata_changed = False
            updated_metadata = dict(original_metadata)

            if include_metadata and original_metadata:
                for key, value in original_metadata.items():
                    if not isinstance(value, str):
                        continue
                    stats.metadata_values_scanned += 1
                    redacted_value, found_in_meta, per_entity_meta = redact_text(
                        value,
                        recognizers,
                        anonymizer,
                        operators,
                    )
                    stats.entities_found += found_in_meta
                    stats.add_entities(per_entity_meta)

                    if redacted_value != value:
                        updated_metadata[key] = redacted_value
                        metadata_changed = True
                        stats.metadata_values_changed += 1

            if doc_changed or metadata_changed:
                stats.records_changed += 1
                chunk_id_str = str(chunk_id)

                if doc_changed:
                    if dry_run:
                        continue

                    original_embedding = get_embedding_for_id(collection, chunk_id_str)
                    if original_embedding is None:
                        stats.warnings.append(
                            f"{collection.name}:{chunk_id_str}: skipped document update (missing embedding)"
                        )
                        continue

                    doc_update_ids.append(chunk_id_str)
                    doc_update_docs.append(redacted_doc)
                    doc_update_embeddings.append(original_embedding)
                    if include_metadata:
                        doc_update_metadatas.append(updated_metadata)
                elif metadata_changed:
                    metadata_update_ids.append(chunk_id_str)
                    metadata_update_metadatas.append(updated_metadata)

        if not dry_run:
            if doc_update_ids:
                update_payload: dict[str, Any] = {
                    "ids": doc_update_ids,
                    "documents": doc_update_docs,
                    "embeddings": doc_update_embeddings,
                }
                if include_metadata:
                    update_payload["metadatas"] = doc_update_metadatas
                collection.update(**update_payload)

            if metadata_update_ids:
                collection.update(
                    ids=metadata_update_ids,
                    metadatas=metadata_update_metadatas,
                )

        offset += len(ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrub API keys/tokens from KnowledgeForge ChromaDB using Microsoft Presidio.",
    )
    parser.add_argument(
        "--collection",
        action="append",
        default=[],
        help="Collection to scrub (repeatable). Aliases supported: documents, codebase, discoveries, conversations.",
    )
    parser.add_argument("--include-metadata", action="store_true", help="Also scrub string metadata values.")
    parser.add_argument("--dry-run", action="store_true", help="Detect and report findings without updating records.")
    parser.add_argument("--batch-size", type=int, default=200, help="Chroma batch size for get/update.")
    parser.add_argument("--config", default="", help="Optional explicit KnowledgeForge config.yaml path.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-collection progress.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    start = time.time()
    stats = ScrubStats()

    try:
        config = KnowledgeForgeConfig.load_config(args.config or None)
        selected_collections = normalize_requested_collections(config, args.collection)
        stats.collections_total = len(selected_collections)

        print("KnowledgeForge Presidio Scrub")
        print(f"Chroma path: {config.chroma_persist_dir}")
        print(
            f"Mode: {'DRY RUN' if args.dry_run else 'WRITE'} | "
            f"include_metadata={args.include_metadata} | batch_size={args.batch_size}"
        )
        print(f"Collections: {', '.join(selected_collections) if selected_collections else '(none)'}")
        print()

        if not selected_collections:
            stats.warnings.append("No collections selected")
            stats.duration_seconds = round(time.time() - start, 2)
            summary = {
                "status": "completed",
                "collections_total": stats.collections_total,
                "collections_processed": stats.collections_processed,
                "collections_skipped": stats.collections_skipped,
                "records_scanned": stats.records_scanned,
                "records_changed": stats.records_changed,
                "entities_found": stats.entities_found,
                "text_values_scanned": stats.text_values_scanned,
                "metadata_values_scanned": stats.metadata_values_scanned,
                "metadata_values_changed": stats.metadata_values_changed,
                "duration_seconds": stats.duration_seconds,
                "dry_run": bool(args.dry_run),
                "include_metadata": bool(args.include_metadata),
                "collections": selected_collections,
                "entity_counts": stats.entity_counts,
                "warnings": stats.warnings,
                "errors": stats.errors,
            }
            print(f"SUMMARY_JSON:{json.dumps(summary, sort_keys=True)}")
            return 0

        recognizers = build_recognizers()
        operators = build_operators(recognizers)
        anonymizer = AnonymizerEngine()
        client = chromadb.PersistentClient(path=config.chroma_persist_dir)

        for collection_name in selected_collections:
            try:
                collection = client.get_collection(name=collection_name)
            except Exception as exc:
                warning = f"{collection_name}: skipped ({exc})"
                stats.collections_skipped += 1
                stats.warnings.append(warning)
                print(f"[skip] {warning}")
                continue

            before_scanned = stats.records_scanned
            before_changed = stats.records_changed
            total = int(collection.count())
            print(f"[scan] {collection_name}: {total} records")

            try:
                scrub_collection(
                    collection=collection,
                    recognizers=recognizers,
                    anonymizer=anonymizer,
                    operators=operators,
                    include_metadata=bool(args.include_metadata),
                    dry_run=bool(args.dry_run),
                    batch_size=max(1, int(args.batch_size)),
                    stats=stats,
                )
                stats.collections_processed += 1
                scanned_delta = stats.records_scanned - before_scanned
                changed_delta = stats.records_changed - before_changed
                print(f"[done] {collection_name}: scanned={scanned_delta}, changed={changed_delta}")
            except Exception as exc:
                error = f"{collection_name}: failed ({exc})"
                stats.errors.append(error)
                print(f"[error] {error}")

        stats.duration_seconds = round(time.time() - start, 2)
        status = "completed"
        if stats.errors and stats.collections_processed:
            status = "partial"
        elif stats.errors and not stats.collections_processed:
            status = "failed"

        summary = {
            "status": status,
            "collections_total": stats.collections_total,
            "collections_processed": stats.collections_processed,
            "collections_skipped": stats.collections_skipped,
            "records_scanned": stats.records_scanned,
            "records_changed": stats.records_changed,
            "entities_found": stats.entities_found,
            "text_values_scanned": stats.text_values_scanned,
            "metadata_values_scanned": stats.metadata_values_scanned,
            "metadata_values_changed": stats.metadata_values_changed,
            "duration_seconds": stats.duration_seconds,
            "dry_run": bool(args.dry_run),
            "include_metadata": bool(args.include_metadata),
            "collections": selected_collections,
            "entity_counts": dict(sorted(stats.entity_counts.items(), key=lambda item: item[0])),
            "warnings": stats.warnings[:50],
            "errors": stats.errors[:50],
        }

        print()
        print("Presidio Scrub Complete")
        print(f"  Collections processed: {stats.collections_processed}/{stats.collections_total}")
        print(f"  Records scanned:       {stats.records_scanned}")
        print(f"  Records changed:       {stats.records_changed}")
        print(f"  Entities found:        {stats.entities_found}")
        print(f"  Duration:              {stats.duration_seconds}s")
        print(f"SUMMARY_JSON:{json.dumps(summary, sort_keys=True)}")
        return 0 if status in {"completed", "partial"} else 1
    except Exception as exc:
        stats.duration_seconds = round(time.time() - start, 2)
        error_summary = {
            "status": "failed",
            "error": str(exc),
            "duration_seconds": stats.duration_seconds,
            "dry_run": bool(args.dry_run),
            "include_metadata": bool(args.include_metadata),
            "collections": args.collection,
        }
        print(f"SUMMARY_JSON:{json.dumps(error_summary, sort_keys=True)}")
        logger.exception("Presidio scrub failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
