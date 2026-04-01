#!/usr/bin/env python3
"""
Migrate episodic memory conversations into KnowledgeForge ChromaDB.

This script reads conversation JSONL files from:
  - ~/.claude/projects/           (Claude Code sessions)
  - ~/.config/superpowers/conversation-archive/_codex/   (Codex sessions)
  - ~/.config/superpowers/conversation-archive/_gemini/  (Gemini sessions)

Optionally loads Kimi-enriched metadata from data/enriched_conversations/
to produce richer embeddings.

Parses each file into exchanges, generates nomic-embed embeddings,
and stores everything in the 'conversations' ChromaDB collection.

Usage:
    python scripts/migrate_episodic_memory.py
    python scripts/migrate_episodic_memory.py --dry-run
    python scripts/migrate_episodic_memory.py --enrichment-dir data/enriched_conversations
    python scripts/migrate_episodic_memory.py --full-reindex
"""

import argparse
import logging
import os
import sys
import time

# Add src to path so we can import knowledgeforge
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.engine import KnowledgeForgeEngine
from knowledgeforge.ingestion.conversations import (
    scan_conversation_dirs,
    parse_jsonl_file,
    load_enrichment_data,
    chunk_exchange,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("migrate_episodic_memory")


def main():
    parser = argparse.ArgumentParser(description="Migrate conversations to KnowledgeForge")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and count without writing to ChromaDB")
    parser.add_argument("--full-reindex", action="store_true",
                        help="Clear conversations collection before indexing")
    parser.add_argument("--enrichment-dir", type=str, default="",
                        help="Path to Kimi-enriched JSON files")
    parser.add_argument("--source-dirs", nargs="*",
                        help="Override conversation source directories")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Embedding batch size (default: 50)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Set config env var
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    if os.path.exists(config_path):
        os.environ.setdefault("KNOWLEDGEFORGE_CONFIG", os.path.abspath(config_path))

    logger.info("Loading KnowledgeForge configuration...")
    config = KnowledgeForgeConfig.load_config()

    # Determine sources
    source_dirs = args.source_dirs or config.conversation_sources
    logger.info(f"Source directories: {source_dirs}")

    # Load enrichment data
    enrichment_dir = args.enrichment_dir or config.conversation_enrichment_dir
    enrichment_map = {}
    if enrichment_dir:
        enrichment_map = load_enrichment_data(enrichment_dir)
        logger.info(f"Loaded {len(enrichment_map)} enrichment records")

    # Scan for JSONL files
    jsonl_files = scan_conversation_dirs(source_dirs)
    logger.info(f"Found {len(jsonl_files)} JSONL conversation files")

    if not jsonl_files:
        logger.warning("No JSONL files found. Check your source directories.")
        return

    # Parse all files
    total_exchanges = 0
    total_chunks = 0
    all_chunks = []  # (id, content, metadata)
    errors = []

    for fpath in jsonl_files:
        try:
            exchanges = parse_jsonl_file(
                fpath,
                enrichment_map=enrichment_map,
                max_tool_result_chars=config.conversation_max_tool_result_chars,
            )

            for ex in exchanges:
                chunks = chunk_exchange(ex)
                all_chunks.extend(chunks)
                total_exchanges += 1

        except Exception as e:
            error_msg = f"Error parsing {fpath}: {e}"
            errors.append(error_msg)
            logger.error(error_msg)

    total_chunks = len(all_chunks)
    enriched_count = sum(1 for _, _, m in all_chunks if m.get("category"))

    logger.info(f"\n{'='*60}")
    logger.info(f"Migration Summary:")
    logger.info(f"  Files scanned:    {len(jsonl_files)}")
    logger.info(f"  Exchanges parsed: {total_exchanges}")
    logger.info(f"  Chunks to index:  {total_chunks}")
    logger.info(f"  Enriched:         {enriched_count} ({enriched_count*100//max(total_chunks,1)}%)")
    logger.info(f"  Errors:           {len(errors)}")
    logger.info(f"{'='*60}")

    if args.dry_run:
        logger.info("DRY RUN — no data written to ChromaDB.")
        if errors:
            for err in errors:
                logger.error(f"  {err}")
        return

    if not all_chunks:
        logger.warning("No chunks to index.")
        return

    # Initialize engine
    logger.info("Initializing KnowledgeForge engine (loading embedding model)...")
    engine = KnowledgeForgeEngine(config)
    collection = config.conversations_collection

    # Optionally clear collection
    if args.full_reindex:
        logger.info(f"Clearing '{collection}' collection for full reindex...")
        engine.store.clear_collection(collection)

    # Get existing IDs
    try:
        existing = engine.store.get(collection)
        existing_ids = set(existing.get("ids", []))
    except Exception:
        existing_ids = set()

    # Filter out already-indexed
    new_chunks = [(cid, content, meta) for cid, content, meta in all_chunks
                  if cid not in existing_ids]

    if not new_chunks:
        logger.info("All exchanges already indexed. Nothing to do.")
        return

    logger.info(f"Indexing {len(new_chunks)} new chunks (skipping {len(all_chunks) - len(new_chunks)} existing)...")

    # Batch embed and store
    batch_size = args.batch_size
    start_time = time.time()
    indexed = 0

    for i in range(0, len(new_chunks), batch_size):
        batch = new_chunks[i:i + batch_size]
        ids = [c[0] for c in batch]
        contents = [c[1] for c in batch]
        metadatas = [c[2] for c in batch]

        embeddings = engine.embedder.embed_documents(contents)
        engine.store.add(collection, ids, contents, embeddings, metadatas)

        indexed += len(batch)
        elapsed = time.time() - start_time
        rate = indexed / elapsed if elapsed > 0 else 0
        logger.info(f"  Progress: {indexed}/{len(new_chunks)} ({rate:.1f} chunks/sec)")

    duration = time.time() - start_time

    # Verify
    final_count = engine.store.count(collection)
    logger.info(f"\n{'='*60}")
    logger.info(f"Migration Complete!")
    logger.info(f"  New chunks indexed: {indexed}")
    logger.info(f"  Total in collection: {final_count}")
    logger.info(f"  Duration: {duration:.1f}s")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
