"""Central KnowledgeForge engine that orchestrates all components.

This is the main entry point for both MCP and REST interfaces.
It knows nothing about transport layers - it's a pure Python class that
coordinates the vector store, embedder, parsers, and discovery system.
"""

import time
import logging
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.embedder import Embedder
from knowledgeforge.core.keyword_index import KeywordIndex
from knowledgeforge.core.memory_registry import MemoryRegistry
from knowledgeforge.core.store import VectorStore
from knowledgeforge.core.models import (
    Chunk, Discovery, SearchResult, SearchResponse, IngestResult, ProjectInfo,
    SearchSnippet, SemanticRecord, MemoryCard,
    ConversationExchange
)
from knowledgeforge.ingestion.obsidian import ObsidianParser
from knowledgeforge.ingestion.code import CodeParser
from knowledgeforge.discovery.manager import DiscoveryManager
from knowledgeforge.discovery.promoter import DiscoveryPromoter
from knowledgeforge.ingestion.chunker import compute_file_hash
from knowledgeforge.ingestion.ob1 import OB1Parser
from knowledgeforge.ingestion.fingerprint import content_fingerprint
from knowledgeforge.ingestion.enrichment import MetadataEnricher

logger = logging.getLogger(__name__)

# Synonym expansion for search enrichment
SYNONYMS = {
    "auth": ["authentication", "authorization", "login", "JWT", "token", "session", "OAuth"],
    "db": ["database", "SQL", "query", "connection", "pool", "migration"],
    "api": ["endpoint", "route", "handler", "REST", "request", "response"],
    "ui": ["frontend", "component", "render", "DOM", "CSS", "layout"],
    "test": ["testing", "unittest", "pytest", "assertion", "mock", "fixture"],
    "deploy": ["deployment", "CI/CD", "Docker", "Kubernetes", "production"],
    "perf": ["performance", "optimization", "speed", "latency", "throughput"],
    "err": ["error", "exception", "bug", "crash", "failure", "traceback"],
}


class KnowledgeForgeEngine:
    """Central engine that ties all KnowledgeForge components together.

    This is the single entry point that both MCP and REST interfaces call.
    It knows nothing about transport layers - it's a pure Python class.
    """

    def __init__(self, config: KnowledgeForgeConfig):
        """Initialize the KnowledgeForge engine with all components.

        Args:
            config: KnowledgeForge configuration instance
        """
        self.config = config
        self.embedder = Embedder(
            config.embedding_model,
            config.embedding_device,
            provider=config.embedding_provider,
            openai_api_key=config.openai_api_key,
            openrouter_api_key=config.openrouter_api_key,
        )
        self.store = VectorStore(config.chroma_persist_dir)
        self.keyword_index = KeywordIndex(config.keyword_index_path)
        self.memory_registry = MemoryRegistry(config.memory_registry_path)

        # Initialize parsers only if paths are configured
        self.obsidian_parser = None
        if config.obsidian_vault_path:
            self.obsidian_parser = ObsidianParser(config.obsidian_vault_path, config)

        self.code_parser = CodeParser(config)

        self.ob1_parser = None
        if getattr(config, 'ob1_supabase_url', None) and getattr(config, 'ob1_supabase_key', None):
            self.ob1_parser = OB1Parser(
                supabase_url=config.ob1_supabase_url,
                supabase_key=config.ob1_supabase_key,
                access_key=getattr(config, 'ob1_access_key', ''),
            )

        self.metadata_enricher = None
        enrichment_key = getattr(config, 'enrichment_api_key', None)
        if enrichment_key:
            self.metadata_enricher = MetadataEnricher(
                api_key=enrichment_key,
                model=getattr(config, 'enrichment_model', 'gpt-4o-mini'),
            )

        self.discovery_manager = DiscoveryManager(self.store, self.embedder, config)

        self.discovery_promoter = None
        if config.obsidian_vault_path:
            self.discovery_promoter = DiscoveryPromoter(
                config.obsidian_vault_path,
                config.obsidian_discoveries_folder
            )

        self._bootstrap_keyword_index()
        logger.info("KnowledgeForgeEngine initialized successfully")

    def _bootstrap_keyword_index(self) -> None:
        """Backfill keyword index from existing Chroma collections when needed."""
        collections = [
            self.config.facts_collection,
            self.config.runbooks_collection,
            self.config.project_overviews_collection,
            self.config.memory_cards_collection,
            self.config.docs_collection,
            self.config.code_collection,
            self.config.discoveries_collection,
            self.config.conversations_collection,
        ]

        for collection in collections:
            try:
                keyword_count = self.keyword_index.count(collection)
                if keyword_count > 0:
                    continue

                chroma_count = self.store.count(collection)
                if chroma_count == 0:
                    continue

                data = self.store.get(collection)
                ids = data.get("ids", [])
                documents = data.get("documents", [])
                metadatas = data.get("metadatas", [])
                if not ids:
                    continue

                self.keyword_index.upsert_chunks(collection, ids, documents, metadatas)
                logger.info(
                    "Bootstrapped keyword index for '%s' with %s chunks",
                    collection,
                    len(ids),
                )
            except Exception as exc:
                logger.warning(
                    "Keyword index bootstrap skipped for '%s': %s",
                    collection,
                    exc,
                )

    def _embed_for_ingest(self, texts: list[str]) -> list[list[float]]:
        """Embed documents using the configured ingest batch size."""
        if not texts:
            return []

        batch_size = max(1, int(self.config.embedding_batch_size or 1))
        return self.embedder.embed_batch(
            texts,
            batch_size=batch_size,
            is_query=False,
        )

    def _should_ignore_project_path(
        self,
        path: Path,
        project_root: Path,
        ignore_patterns: list[str],
    ) -> bool:
        """Check whether a project-relative path should be excluded from ingest."""
        try:
            rel_path = path.relative_to(project_root)
        except ValueError:
            rel_path = path

        rel_norm = str(rel_path).replace(os.sep, "/")
        rel_parts = {part for part in rel_path.parts if part}
        name = path.name

        for pattern in ignore_patterns:
            normalized = str(pattern).strip().replace("\\", "/").strip("/")
            if not normalized:
                continue

            if "/" in normalized:
                if rel_norm == normalized or rel_norm.startswith(f"{normalized}/"):
                    return True
                continue

            if normalized == name or normalized in rel_parts:
                return True

        return False

    def _scan_project_files(self, project_path: str, project_name: str) -> dict[str, list[str]]:
        """Scan a project once and return sorted markdown/code file lists."""
        root = Path(project_path)
        override = self.config.get_project_ingest_override(project_name)
        ignore_patterns = list(self.config.ignore_patterns) + list(override.ignore_patterns)
        obsidian_exts = set(self.config.obsidian_extensions)
        code_exts = set(self.config.code_extensions)

        markdown_files: list[str] = []
        code_files: list[str] = []

        for dirpath, dirnames, filenames in os.walk(project_path):
            current = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not self._should_ignore_project_path(current / d, root, ignore_patterns)
            )

            for fname in sorted(filenames):
                file_path = current / fname
                if self._should_ignore_project_path(file_path, root, ignore_patterns):
                    continue

                ext = file_path.suffix.lower()
                if not override.skip_markdown and ext in obsidian_exts:
                    markdown_files.append(str(file_path))
                elif not override.skip_code and ext in code_exts:
                    code_files.append(str(file_path))

        return {
            "markdown": markdown_files,
            "code": code_files,
        }

    def _project_markdown_store_path(self, file_path: str) -> str:
        """Return the stored docs-collection path for a project markdown file."""
        if not self.obsidian_parser:
            return file_path
        return os.path.relpath(file_path, self.obsidian_parser.vault_path)

    def _store_chunk_list(
        self,
        collection: str,
        file_chunk_list: list[Chunk],
        existing_hashes: dict[str, str] | None = None,
    ) -> IngestResult:
        """Embed and upsert one file's chunk list into the target collection."""
        if not file_chunk_list:
            return IngestResult(
                files_processed=0,
                files_skipped=1,
                chunks_created=0,
                errors=[],
                duration_seconds=0,
            )

        file_path = file_chunk_list[0].file_path
        file_hash = file_chunk_list[0].content_hash

        if existing_hashes and file_path in existing_hashes:
            if existing_hashes[file_path] == file_hash:
                return IngestResult(
                    files_processed=0,
                    files_skipped=1,
                    chunks_created=0,
                    errors=[],
                    duration_seconds=0,
                )

            self.store.delete_by_file_path(collection, file_path)
            self.keyword_index.delete_by_file_path(collection, file_path)

        contents = [chunk.content for chunk in file_chunk_list]
        ids = [chunk.chunk_id for chunk in file_chunk_list]
        metadatas = [chunk.to_metadata() for chunk in file_chunk_list]
        embeddings = self._embed_for_ingest(contents)

        self.store.add(collection, ids, contents, embeddings, metadatas)
        self.keyword_index.upsert_chunks(collection, ids, contents, metadatas)

        if existing_hashes is not None:
            existing_hashes[file_path] = file_hash

        return IngestResult(
            files_processed=1,
            files_skipped=0,
            chunks_created=len(file_chunk_list),
            errors=[],
            duration_seconds=0,
        )

    # === SEARCH ===

    def search(
        self,
        query: str,
        project: str = None,
        max_results: int = 6,
        min_score_threshold: float = 0.35,
        collections: list[str] = None,
        tags: list[str] = None,
        language: str = None,
        category: str = None,
        confirmed_only: bool = False,
        n_results: int = None,
        min_score: float = None,
    ) -> SearchResponse:
        """Hybrid search (vector + BM25) with weighted fusion."""
        start_time = time.time()

        # Backward-compatible aliases.
        if n_results is not None:
            max_results = n_results
        if min_score is not None:
            min_score_threshold = min_score

        if collections is None:
            collections = [
                self.config.facts_collection,
                self.config.runbooks_collection,
                self.config.project_overviews_collection,
                self.config.memory_cards_collection,
                self.config.docs_collection,
                self.config.code_collection,
                self.config.discoveries_collection,
                self.config.conversations_collection,
            ]

        candidate_count = max(max_results * 4, max_results)
        query_embedding = self.embedder.embed_query(query)
        merged: dict[str, dict] = {}

        vector_jobs = {}
        keyword_jobs = {}
        with ThreadPoolExecutor(max_workers=max(2, len(collections) * 2)) as executor:
            for col_name in collections:
                where = self._build_where_filter(
                    col_name, project, tags, language, category, confirmed_only
                )
                keyword_filter = self._build_keyword_filter(
                    col_name, project, tags, language, category, confirmed_only
                )

                vector_jobs[
                    executor.submit(
                        self.store.query,
                        collection=col_name,
                        query_embedding=query_embedding,
                        n_results=candidate_count,
                        where=where if where else None,
                    )
                ] = col_name
                keyword_jobs[
                    executor.submit(
                        self.keyword_index.search,
                        query=query,
                        collection=col_name,
                        limit=candidate_count,
                        filters=keyword_filter,
                    )
                ] = col_name

            for future, col_name in vector_jobs.items():
                try:
                    raw = future.result()
                except Exception as e:
                    logger.warning("Vector search failed for collection '%s': %s", col_name, e)
                    continue

                if not raw.get("ids") or not raw["ids"][0]:
                    continue

                for i in range(len(raw["ids"][0])):
                    chunk_id = raw["ids"][0][i]
                    distance = float(raw["distances"][0][i])
                    vector_score = max(0.0, min(1.0, 1.0 - (distance / 2.0)))
                    key = f"{col_name}:{chunk_id}"

                    entry = merged.setdefault(
                        key,
                        {
                            "collection": col_name,
                            "content": "",
                            "metadata": {},
                            "vector_score": 0.0,
                            "keyword_score": 0.0,
                        },
                    )
                    entry["vector_score"] = max(entry["vector_score"], vector_score)
                    entry["content"] = raw["documents"][0][i] or entry["content"]
                    entry["metadata"] = raw["metadatas"][0][i] or entry["metadata"]

            for future, col_name in keyword_jobs.items():
                try:
                    keyword_results = future.result()
                except Exception as e:
                    logger.warning("Keyword search failed for collection '%s': %s", col_name, e)
                    continue

                for rank, item in enumerate(keyword_results):
                    keyword_score = 1.0 / (1.0 + rank)
                    item_collection = item.get("collection") or col_name
                    key = f"{item_collection}:{item['chunk_id']}"

                    entry = merged.setdefault(
                        key,
                        {
                            "collection": item_collection,
                            "content": "",
                            "metadata": {},
                            "vector_score": 0.0,
                            "keyword_score": 0.0,
                        },
                    )
                    entry["keyword_score"] = max(entry["keyword_score"], keyword_score)
                    entry["content"] = item.get("content") or entry["content"]
                    entry["metadata"] = item.get("metadata") or entry["metadata"]

        all_results: list[SearchResult] = []
        for item in merged.values():
            metadata = item["metadata"] or {}
            status = metadata.get("status", "active")
            if item["collection"] == self.config.memory_cards_collection:
                if status in {"superseded", "deprecated", "cancelled"}:
                    continue
            elif status != "active":
                continue

            trust_level = str(metadata.get("trust_level", "T4") or "T4")
            trust_boost = {
                "T1": 1.00,
                "T2": 0.95,
                "T3": 0.85,
                "T4": 0.70,
            }.get(trust_level, 0.70)

            if item["collection"] == self.config.memory_cards_collection:
                trust_boost *= {
                    "active_verified": 1.00,
                    "verified": 1.00,
                    "current": 1.00,
                    "active": 0.95,
                    "resolved": 0.90,
                    "failed": 0.88,
                    "active_unverified": 0.84,
                    "open_unverified": 0.78,
                    "historical": 0.58,
                    "superseded_candidate": 0.45,
                }.get(str(status), 0.70)
                if not bool(metadata.get("current_truth", False)):
                    trust_boost *= 0.95

            final_score = ((item["vector_score"] * 0.7) + (item["keyword_score"] * 0.3)) * trust_boost
            if final_score < min_score_threshold:
                continue
            all_results.append(
                SearchResult(
                    content=item["content"],
                    score=round(final_score, 4),
                    metadata=metadata,
                    collection=item["collection"],
                )
            )

        all_results.sort(key=lambda r: r.score, reverse=True)
        top_results = all_results[:max_results]

        search_time = (time.time() - start_time) * 1000  # ms

        logger.info(
            "Hybrid search completed: query='%s...', results=%s, time=%.2fms",
            query[:50],
            len(top_results),
            search_time,
        )

        return SearchResponse(
            query=query,
            results=top_results,
            total_results=len(all_results),
            search_time_ms=round(search_time, 2)
        )

    def _build_keyword_filter(
        self,
        collection: str,
        project: str = None,
        tags: list[str] = None,
        language: str = None,
        category: str = None,
        confirmed_only: bool = False,
    ) -> dict:
        """Build keyword index filters with the same semantics as vector search."""
        filters = {"status": "active"}

        if collection in [self.config.facts_collection, self.config.runbooks_collection, self.config.project_overviews_collection]:
            if project:
                filters["project"] = project
            if tags:
                filters["tags"] = tags

        elif collection == self.config.memory_cards_collection:
            if project:
                filters["project"] = project
            if category:
                filters["category"] = category
            if tags:
                filters["tags"] = tags

        elif collection == self.config.docs_collection:
            if project:
                filters["project"] = project
            if tags:
                filters["tags"] = tags

        elif collection == self.config.code_collection:
            if project:
                filters["project_name"] = project
            if language:
                filters["language"] = language

        elif collection == self.config.discoveries_collection:
            if project:
                filters["project"] = project
            if category:
                filters["category"] = category
            if confirmed_only:
                filters["confirmed"] = True

        elif collection == self.config.conversations_collection:
            if project:
                filters["project"] = project
            if category:
                filters["category"] = category

        return filters

    def _build_where_filter(self, collection, project, tags, language, category, confirmed_only):
        """Build ChromaDB where filter based on parameters and collection type."""
        filters = []
        if collection != self.config.memory_cards_collection:
            filters.append({"status": "active"})

        if collection in [self.config.facts_collection, self.config.runbooks_collection, self.config.project_overviews_collection]:
            if project:
                filters.append({"project": project})
            if tags:
                filters.append({"tags": {"$contains": tags[0]}})

        elif collection == self.config.memory_cards_collection:
            if project:
                filters.append({"project": project})
            if category:
                filters.append({"category": category})
            if tags:
                filters.append({"tags": {"$contains": tags[0]}})

        elif collection == self.config.docs_collection:
            if project:
                filters.append({"frontmatter_project": project})
            if tags:
                # ChromaDB can only do simple contains for single tag
                # For multiple tags, we'd need to do post-filtering
                filters.append({"frontmatter_tags": {"$contains": tags[0]}})

        elif collection == self.config.code_collection:
            if project:
                filters.append({"project_name": project})
            if language:
                filters.append({"language": language})

        elif collection == self.config.discoveries_collection:
            if project:
                filters.append({"project": project})
            if category:
                filters.append({"category": category})
            if confirmed_only:
                filters.append({"confirmed": True})

        elif collection == self.config.conversations_collection:
            if project:
                filters.append({"project": project})
            if category:
                filters.append({"category": category})

        if not filters:
            return None
        elif len(filters) == 1:
            return filters[0]
        else:
            return {"$and": filters}

    def _score_with_boost(self, result: SearchResult) -> float:
        """Apply boosting to search scores.
        Boost confirmed critical discoveries.

        Args:
            result: Search result to score

        Returns:
            Boosted score (capped at 1.0)
        """
        score = result.score
        if result.collection == self.config.discoveries_collection:
            meta = result.metadata
            if meta.get("confirmed", False):
                score *= 1.1  # 10% boost for confirmed
            if meta.get("severity") == "critical":
                score *= 1.05  # 5% boost for critical
        return min(score, 1.0)  # Cap at 1.0

    def search_snippets(
        self,
        query: str,
        project: str = None,
        max_results: int = 6,
        min_score_threshold: float = 0.35,
        collections: list[str] = None,
        tags: list[str] = None,
        language: str = None,
        category: str = None,
        confirmed_only: bool = False,
    ) -> list[SearchSnippet]:
        """Search and return lean snippets for tool-facing responses."""
        if collections is None:
            collections = [
                self.config.facts_collection,
                self.config.runbooks_collection,
                self.config.project_overviews_collection,
                self.config.memory_cards_collection,
                self.config.docs_collection,
                self.config.code_collection,
                self.config.discoveries_collection,
            ]

        response = self.search(
            query=query,
            project=project,
            max_results=max_results,
            min_score_threshold=min_score_threshold,
            collections=collections,
            tags=tags,
            language=language,
            category=category,
            confirmed_only=confirmed_only,
        )

        snippets: list[SearchSnippet] = []
        for result in response.results:
            meta = result.metadata or {}
            file_path = str(meta.get("file_path") or meta.get("source_file") or "")
            snippets.append(
                SearchSnippet(
                    text_preview=result.content[:700],
                    file_path=file_path,
                    start_line=int(meta.get("start_line", 0) or 0),
                    end_line=int(meta.get("end_line", 0) or 0),
                    score=round(result.score, 4),
                )
            )
        return snippets

    def get_knowledge_context(
        self, file_path: str, start_line: int, line_count: int
    ) -> dict:
        """Read a small line window directly from the local filesystem."""
        if file_path.startswith("memory://"):
            card_id = file_path.removeprefix("memory://")
            card = self.memory_registry.get_card(card_id)
            if not card:
                raise FileNotFoundError(f"Memory card not found: {file_path}")
            return {
                "file_path": file_path,
                "start_line": 1,
                "end_line": 1,
                "line_count": 1,
                "content": card.to_embedding_text(),
            }

        resolved_path = self._resolve_context_path(file_path)
        if not resolved_path:
            raise FileNotFoundError(f"File not found: {file_path}")

        start = max(1, int(start_line))
        count = max(1, int(line_count))

        with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        if total_lines == 0:
            return {
                "file_path": str(resolved_path),
                "start_line": start,
                "end_line": start,
                "line_count": 0,
                "content": "",
            }

        safe_start = min(start, total_lines)
        safe_end = min(total_lines, safe_start + count - 1)
        content = "".join(lines[safe_start - 1 : safe_end])

        return {
            "file_path": str(resolved_path),
            "start_line": safe_start,
            "end_line": safe_end,
            "line_count": (safe_end - safe_start + 1),
            "content": content,
        }

    def _resolve_context_path(self, file_path: str) -> Optional[Path]:
        """Resolve relative file paths from indexed metadata to real files."""
        if not file_path:
            return None

        raw = Path(os.path.expanduser(file_path))
        candidates: list[Path] = []

        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.append(Path.cwd() / raw)
            if self.config.obsidian_vault_path:
                candidates.append(Path(self.config.obsidian_vault_path) / raw)
            for project in self.config.project_paths:
                proj_path = project.get("path")
                if proj_path:
                    candidates.append(Path(proj_path) / raw)

        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved.exists() and resolved.is_file():
                return resolved
        return None

    # === INGESTION ===

    def ingest_obsidian_vault(self, full_reindex: bool = False) -> IngestResult:
        """Ingest entire Obsidian vault.

        If full_reindex=False: only process files whose hash has changed (incremental).
        If full_reindex=True: wipe docs collection and re-ingest everything.

        Steps:
        1. If full_reindex, clear the docs collection
        2. Get existing file hashes from store
        3. Parse vault with ObsidianParser
        4. Skip chunks whose source_file hash hasn't changed
        5. For changed files: delete old chunks, embed new chunks, store
        6. Return IngestResult with stats

        Args:
            full_reindex: If True, clear collection and re-index everything

        Returns:
            IngestResult with statistics about the ingestion
        """
        if not self.obsidian_parser:
            return IngestResult(
                files_processed=0,
                files_skipped=0,
                chunks_created=0,
                errors=["No Obsidian vault path configured"],
                duration_seconds=0
            )

        start = time.time()
        collection = self.config.docs_collection

        logger.info(f"Starting Obsidian vault ingestion (full_reindex={full_reindex})")

        if full_reindex:
            logger.info(f"Clearing collection '{collection}' for full reindex")
            self.store.clear_collection(collection)
            self.keyword_index.clear_collection(collection)
            existing_hashes = {}
        else:
            existing_hashes = self.store.get_file_hashes(collection)
            logger.info(f"Incremental ingestion: {len(existing_hashes)} files already indexed")

        # Stream vault files one-by-one to avoid building all chunks in memory.
        vault_files: list[str] = []
        for root, dirs, files in os.walk(self.obsidian_parser.vault_path):
            root_path = Path(root)
            dirs[:] = [
                d
                for d in dirs
                if not self.obsidian_parser._should_ignore(root_path / d)  # noqa: SLF001
            ]
            for file_name in files:
                file_path = root_path / file_name
                if file_path.suffix not in self.config.obsidian_extensions:
                    continue
                if self.obsidian_parser._should_ignore(file_path):  # noqa: SLF001
                    continue
                vault_files.append(str(file_path))

        logger.info("Vault scan: discovered %s markdown files", len(vault_files))

        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        errors = []

        for idx, abs_file_path in enumerate(vault_files, start=1):
            logger.info("Vault file %s/%s: parsing %s", idx, len(vault_files), abs_file_path)
            file_chunk_list = self.obsidian_parser.parse_file(abs_file_path)
            if not file_chunk_list:
                files_skipped += 1
                if idx % 25 == 0:
                    logger.info(
                        "Vault ingest progress: %s/%s files handled, %s chunks created",
                        idx,
                        len(vault_files),
                        chunks_created,
                    )
                continue

            file_path = file_chunk_list[0].file_path
            file_hash = file_chunk_list[0].content_hash

            # Skip unchanged files
            if file_path in existing_hashes and existing_hashes[file_path] == file_hash:
                files_skipped += 1
                if idx % 25 == 0:
                    logger.info(
                        "Vault ingest progress: %s/%s files handled, %s chunks created",
                        idx,
                        len(vault_files),
                        chunks_created,
                    )
                continue

            try:
                # Delete old chunks for this file
                if file_path in existing_hashes:
                    self.store.delete_by_file_path(collection, file_path)
                    self.keyword_index.delete_by_file_path(collection, file_path)

                # Embed and store new chunks
                contents = [c.content for c in file_chunk_list]
                logger.info(
                    "Vault file %s/%s: embedding %s chunks from %s",
                    idx,
                    len(vault_files),
                    len(file_chunk_list),
                    file_path,
                )
                embeddings = self._embed_for_ingest(contents)
                ids = [c.chunk_id for c in file_chunk_list]
                metadatas = [c.to_metadata() for c in file_chunk_list]

                self.store.add(collection, ids, contents, embeddings, metadatas)
                self.keyword_index.upsert_chunks(collection, ids, contents, metadatas)
                logger.info(
                    "Vault file %s/%s: stored %s chunks for %s",
                    idx,
                    len(vault_files),
                    len(file_chunk_list),
                    file_path,
                )

                files_processed += 1
                chunks_created += len(file_chunk_list)
                logger.debug(f"Indexed {file_path}: {len(file_chunk_list)} chunks")
                if idx % 25 == 0:
                    logger.info(
                        "Vault ingest progress: %s/%s files handled, %s chunks created",
                        idx,
                        len(vault_files),
                        chunks_created,
                    )
            except Exception as e:
                error_msg = f"Error processing {file_path}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        duration = time.time() - start
        result = IngestResult(
            files_processed=files_processed,
            files_skipped=files_skipped,
            chunks_created=chunks_created,
            errors=errors,
            duration_seconds=round(duration, 2)
        )
        logger.info(f"Vault ingestion complete: {result}")
        return result

    def ingest_project_batch(
        self,
        project_path: str,
        project_name: str,
        *,
        state: dict[str, Any] | None = None,
        full_reindex: bool = False,
        max_files: int | None = None,
        max_chunks: int | None = None,
        time_budget_seconds: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Ingest a bounded resumable slice of a mixed markdown/code project."""
        start = time.time()
        root = Path(project_path)

        if not root.is_dir():
            return {
                "status": "done",
                "phase": "done",
                "markdown_index": 0,
                "code_index": 0,
                "markdown_total": 0,
                "code_total": 0,
                "files_processed": 0,
                "files_skipped": 0,
                "chunks_created": 0,
                "errors": [f"Not a directory: {project_path}"],
                "duration_seconds": 0,
            }

        scanned = self._scan_project_files(project_path, project_name)
        markdown_files = scanned["markdown"]
        code_files = scanned["code"]
        logger.info(
            "Starting project batch ingest: %s (%s markdown, %s code, full_reindex=%s)",
            project_name,
            len(markdown_files),
            len(code_files),
            full_reindex,
        )

        markdown_total = len(markdown_files)
        code_total = len(code_files)
        state = state or {}
        phase = str(state.get("phase", "markdown"))
        markdown_index = int(state.get("markdown_index", 0) or 0)
        code_index = int(state.get("code_index", 0) or 0)

        if not markdown_files and phase == "markdown":
            phase = "code"
        if phase not in {"markdown", "code", "done"}:
            phase = "markdown" if markdown_files else "code"
        if phase == "done" and (markdown_index < markdown_total or code_index < code_total):
            phase = "markdown" if markdown_index < markdown_total else "code"

        errors: list[str] = []
        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        files_handled = 0

        if full_reindex:
            try:
                self.store.delete(self.config.code_collection, where={"project_name": project_name})
                self.keyword_index.delete_by_project(self.config.code_collection, project_name)
            except Exception as exc:
                errors.append(f"Failed to clear code chunks for {project_name}: {exc}")

            if self.obsidian_parser:
                for file_path in markdown_files:
                    stored_path = self._project_markdown_store_path(file_path)
                    try:
                        self.store.delete_by_file_path(self.config.docs_collection, stored_path)
                        self.keyword_index.delete_by_file_path(self.config.docs_collection, stored_path)
                    except Exception as exc:
                        errors.append(f"Failed to clear markdown chunks for {stored_path}: {exc}")

            existing_doc_hashes: dict[str, str] = {}
            existing_code_hashes: dict[str, str] = {}
        else:
            existing_doc_hashes = self.store.get_file_hashes(self.config.docs_collection)
            existing_code_hashes = self.store.get_file_hashes(self.config.code_collection)

        def budget_exhausted() -> bool:
            if files_handled == 0:
                return False
            if max_files is not None and files_handled >= max_files:
                return True
            if max_chunks is not None and chunks_created >= max_chunks:
                return True
            if time_budget_seconds is not None and (time.time() - start) >= time_budget_seconds:
                return True
            return False

        def snapshot(current_phase: str) -> dict[str, Any]:
            return {
                "phase": current_phase,
                "markdown_index": markdown_index,
                "code_index": code_index,
                "markdown_total": markdown_total,
                "code_total": code_total,
                "files_processed": files_processed,
                "files_skipped": files_skipped,
                "chunks_created": chunks_created,
                "errors": list(errors),
                "duration_seconds": round(time.time() - start, 2),
            }

        def emit_progress(current_phase: str) -> None:
            if progress_callback is not None:
                progress_callback(snapshot(current_phase))

        if phase == "markdown" and markdown_files and not self.obsidian_parser:
            errors.append("No Obsidian parser configured for project markdown ingestion")
            files_skipped += len(markdown_files) - markdown_index
            files_handled += max(0, len(markdown_files) - markdown_index)
            markdown_index = len(markdown_files)
            phase = "code"

        while phase == "markdown" and markdown_index < len(markdown_files):
            file_path = markdown_files[markdown_index]
            try:
                stored_path = self._project_markdown_store_path(file_path)
                file_hash = compute_file_hash(file_path)
                if existing_doc_hashes.get(stored_path) == file_hash:
                    files_skipped += 1
                else:
                    chunks = self.obsidian_parser.parse_file(file_path) if self.obsidian_parser else []
                    result = self._store_chunk_list(
                        self.config.docs_collection,
                        chunks,
                        existing_hashes=existing_doc_hashes,
                    )
                    files_processed += result.files_processed
                    files_skipped += result.files_skipped
                    chunks_created += result.chunks_created
                    errors.extend(result.errors)
                logger.info(
                    "Project markdown progress (%s): %s/%s files handled",
                    project_name,
                    markdown_index + 1,
                    len(markdown_files),
                )
            except Exception as exc:
                error_msg = f"Error processing markdown {file_path}: {exc}"
                errors.append(error_msg)
                logger.error(error_msg)

            markdown_index += 1
            files_handled += 1
            emit_progress("markdown" if markdown_index < len(markdown_files) else "code")
            if budget_exhausted():
                result = snapshot("markdown" if markdown_index < len(markdown_files) else "code")
                result["status"] = "partial"
                return result

        phase = "code"

        while phase == "code" and code_index < len(code_files):
            file_path = code_files[code_index]
            try:
                file_hash = compute_file_hash(file_path)
                if existing_code_hashes.get(file_path) == file_hash:
                    files_skipped += 1
                else:
                    chunks = self.code_parser.parse_file(file_path, project_name)
                    result = self._store_chunk_list(
                        self.config.code_collection,
                        chunks,
                        existing_hashes=existing_code_hashes,
                    )
                    files_processed += result.files_processed
                    files_skipped += result.files_skipped
                    chunks_created += result.chunks_created
                    errors.extend(result.errors)
                logger.info(
                    "Project code progress (%s): %s/%s files handled",
                    project_name,
                    code_index + 1,
                    len(code_files),
                )
            except Exception as exc:
                error_msg = f"Error processing code {file_path}: {exc}"
                errors.append(error_msg)
                logger.error(error_msg)

            code_index += 1
            files_handled += 1
            emit_progress("code")
            if budget_exhausted():
                result = snapshot("code")
                result["status"] = "partial"
                return result

        result = snapshot("done")
        result["status"] = "done"
        return result

    def ingest_project(self, project_path: str, project_name: str, full_reindex: bool = False) -> IngestResult:
        """Ingest a project directory, handling both markdown and code files."""
        result = self.ingest_project_batch(
            project_path,
            project_name,
            state={"phase": "markdown", "markdown_index": 0, "code_index": 0},
            full_reindex=full_reindex,
            max_files=None,
            max_chunks=None,
            time_budget_seconds=None,
        )
        ingest_result = IngestResult(
            files_processed=result["files_processed"],
            files_skipped=result["files_skipped"],
            chunks_created=result["chunks_created"],
            errors=result["errors"],
            duration_seconds=result["duration_seconds"],
        )
        logger.info("Project ingestion complete: %s", ingest_result)
        return ingest_result

    def _ingest_code_project(self, project_path: str, project_name: str, full_reindex: bool = False) -> IngestResult:
        """Ingest only code files from a project directory (internal).

        This is the original code-only ingestion logic, now used as the code
        phase of the unified ingest_project() method.

        Args:
            project_path: Absolute path to project directory
            project_name: Name of the project
            full_reindex: If True, clear this project's data and re-index

        Returns:
            IngestResult with statistics about the ingestion
        """
        start = time.time()
        collection = self.config.code_collection

        logger.info(f"Starting code ingestion: {project_name} (full_reindex={full_reindex})")

        if full_reindex:
            # Only clear chunks for this specific project
            logger.info(f"Clearing project '{project_name}' from collection '{collection}'")
            try:
                self.store.delete(collection, where={"project_name": project_name})
                self.keyword_index.delete_by_project(collection, project_name)
            except Exception as e:
                logger.warning(f"Failed to delete project chunks: {e}")
            existing_hashes = {}
        else:
            all_hashes = self.store.get_file_hashes(collection)
            existing_hashes = all_hashes  # Filter will happen during comparison
            logger.info(f"Incremental ingestion: {len(all_hashes)} total files already indexed")

        chunks = self.code_parser.parse_project(project_path, project_name)

        file_chunks = {}
        for chunk in chunks:
            file_chunks.setdefault(chunk.file_path, []).append(chunk)

        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        errors = []

        for file_path, file_chunk_list in file_chunks.items():
            file_hash = file_chunk_list[0].content_hash

            if file_path in existing_hashes and existing_hashes[file_path] == file_hash:
                files_skipped += 1
                continue

            try:
                if file_path in existing_hashes:
                    self.store.delete_by_file_path(collection, file_path)
                    self.keyword_index.delete_by_file_path(collection, file_path)

                contents = [c.content for c in file_chunk_list]
                embeddings = self._embed_for_ingest(contents)
                ids = [c.chunk_id for c in file_chunk_list]
                metadatas = [c.to_metadata() for c in file_chunk_list]

                self.store.add(collection, ids, contents, embeddings, metadatas)
                self.keyword_index.upsert_chunks(collection, ids, contents, metadatas)

                files_processed += 1
                chunks_created += len(file_chunk_list)
                logger.debug(f"Indexed {file_path}: {len(file_chunk_list)} chunks")
                if (files_processed + files_skipped) % 25 == 0:
                    logger.info(
                        "Code ingest progress (%s): %s/%s files handled, %s chunks created",
                        project_name,
                        (files_processed + files_skipped),
                        len(file_chunks),
                        chunks_created,
                    )
            except Exception as e:
                error_msg = f"Error processing {file_path}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        duration = time.time() - start
        result = IngestResult(
            files_processed=files_processed,
            files_skipped=files_skipped,
            chunks_created=chunks_created,
            errors=errors,
            duration_seconds=round(duration, 2)
        )
        logger.info(f"Code ingestion complete: {result}")
        return result

    def ingest_file(self, file_path: str, collection: str = "auto") -> IngestResult:
        """Ingest a single file. Auto-detect collection based on extension and path.

        Args:
            file_path: Absolute path to file
            collection: Collection name or "auto" to auto-detect

        Returns:
            IngestResult with statistics
        """
        start = time.time()
        path = Path(file_path)

        logger.info(f"Ingesting single file: {file_path} (collection={collection})")

        if collection == "auto":
            if path.suffix in self.config.obsidian_extensions:
                collection = self.config.docs_collection
            elif path.suffix in self.config.code_extensions:
                collection = self.config.code_collection
            else:
                error_msg = f"Unsupported file type: {path.suffix}"
                logger.error(error_msg)
                return IngestResult(
                    files_processed=0,
                    files_skipped=0,
                    chunks_created=0,
                    errors=[error_msg],
                    duration_seconds=0
                )

        try:
            if collection == self.config.docs_collection:
                if not self.obsidian_parser:
                    return IngestResult(
                        files_processed=0,
                        files_skipped=0,
                        chunks_created=0,
                        errors=["No Obsidian parser configured"],
                        duration_seconds=0
                    )
                chunks = self.obsidian_parser.parse_file(str(path))
            else:
                project_name = path.parent.name
                chunks = self.code_parser.parse_file(str(path), project_name)

            if not chunks:
                logger.warning(f"No chunks generated for {file_path}")
                return IngestResult(
                    files_processed=0,
                    files_skipped=1,
                    chunks_created=0,
                    errors=[],
                    duration_seconds=0
                )

            # Delete old and insert new
            self.store.delete_by_file_path(collection, chunks[0].file_path)
            self.keyword_index.delete_by_file_path(collection, chunks[0].file_path)

            contents = [c.content for c in chunks]
            embeddings = self._embed_for_ingest(contents)
            ids = [c.chunk_id for c in chunks]
            metadatas = [c.to_metadata() for c in chunks]

            self.store.add(collection, ids, contents, embeddings, metadatas)
            self.keyword_index.upsert_chunks(collection, ids, contents, metadatas)

            duration = time.time() - start
            logger.info(f"File ingestion complete: {len(chunks)} chunks in {duration:.2f}s")
            return IngestResult(
                files_processed=1,
                files_skipped=0,
                chunks_created=len(chunks),
                errors=[],
                duration_seconds=round(duration, 2)
            )
        except Exception as e:
            duration = time.time() - start
            error_msg = f"Failed to ingest {file_path}: {e}"
            logger.error(error_msg)
            return IngestResult(
                files_processed=0,
                files_skipped=0,
                chunks_created=0,
                errors=[error_msg],
                duration_seconds=round(duration, 2)
            )

    # === DISCOVERIES (proxies to DiscoveryManager) ===

    def store_discovery(
        self,
        content: str,
        context: str = "",
        project: str = "",
        category: str = "gotcha",
        severity: str = "important",
        source_agent: str = "unknown",
        source_session: str = "",
        related_files: list[str] = None
    ) -> Discovery:
        """Store a new discovery with automatic deduplication.

        Args:
            content: Main discovery content
            context: Additional context
            project: Project name
            category: Discovery category (bugfix|gotcha|performance|etc)
            severity: Severity level (critical|important|nice-to-know)
            source_agent: Agent that created the discovery
            source_session: Session ID
            related_files: List of related file paths

        Returns:
            Discovery object (newly created or merged with existing)
        """
        discovery = Discovery(
            content=content,
            context=context,
            project=project,
            category=category,
            severity=severity,
            source_agent=source_agent,
            source_session=source_session,
            related_files=related_files or []
        )
        return self.discovery_manager.create(discovery)

    def get_discoveries(
        self,
        project: str = None,
        unconfirmed_only: bool = False,
        category: str = None
    ) -> list[Discovery]:
        """Get discoveries with optional filters.

        Args:
            project: Filter by project
            unconfirmed_only: Only return unconfirmed discoveries
            category: Filter by category

        Returns:
            List of Discovery objects
        """
        return self.discovery_manager.list(project, unconfirmed_only, category)

    def confirm_discovery(self, discovery_id: str) -> Discovery:
        """Confirm a discovery and optionally promote to Obsidian.

        Args:
            discovery_id: Discovery ID to confirm

        Returns:
            Updated Discovery object
        """
        discovery = self.discovery_manager.confirm(discovery_id)
        if self.config.auto_promote_confirmed and self.discovery_promoter and discovery:
            try:
                self.discovery_promoter.promote(discovery)
                discovery.promoted_to_obsidian = True
                logger.info(f"Auto-promoted discovery {discovery_id[:8]}... to Obsidian")
            except Exception as e:
                logger.warning(f"Auto-promote failed for {discovery_id}: {e}")
        return discovery

    def reject_discovery(self, discovery_id: str) -> bool:
        """Reject and delete a discovery.

        Args:
            discovery_id: Discovery ID to reject

        Returns:
            True if deleted successfully
        """
        return self.discovery_manager.reject(discovery_id)

    def promote_discoveries_to_obsidian(self) -> int:
        """Promote all confirmed, un-promoted discoveries to Obsidian vault.

        Returns:
            Number of discoveries promoted
        """
        if not self.discovery_promoter:
            logger.warning("No Obsidian vault configured, cannot promote discoveries")
            return 0

        discoveries = self.discovery_manager.list(unconfirmed_only=False)
        to_promote = [d for d in discoveries if d.confirmed and not d.promoted_to_obsidian]

        if not to_promote:
            logger.info("No discoveries to promote")
            return 0

        paths = self.discovery_promoter.promote_all_confirmed(to_promote)
        logger.info(f"Promoted {len(paths)} discoveries to Obsidian")
        return len(paths)

    # === MANAGEMENT ===

    def list_projects(self) -> list[ProjectInfo]:
        """List all indexed/configured projects with health-oriented stats."""
        projects = []

        # Obsidian vault
        if self.config.obsidian_vault_path:
            doc_count = self.store.count(self.config.docs_collection)
            hashes = self.store.get_file_hashes(self.config.docs_collection)
            status = "indexed" if doc_count > 0 else "registered"
            projects.append(ProjectInfo(
                name=Path(self.config.obsidian_vault_path).name,
                path=self.config.obsidian_vault_path,
                type="obsidian",
                total_chunks=doc_count,
                last_indexed="",
                file_count=len(hashes),
                status=status,
                error_count=0,
            ))

        # Code projects
        for proj in self.config.project_paths:
            proj_name = proj.get("name", Path(proj["path"]).name)
            try:
                result = self.store.get(
                    self.config.code_collection,
                    where={"project_name": proj_name}
                )
                chunk_count = len(result.get("ids", []))
                file_hashes = {}
                last_indexed = ""
                for meta in result.get("metadatas", []):
                    if not meta:
                        continue
                    file_path = meta.get("file_path") or meta.get("source_file")
                    if file_path:
                        file_hashes[str(file_path)] = True
                    updated_at = str(meta.get("updated_at", "") or "")
                    if updated_at and updated_at > last_indexed:
                        last_indexed = updated_at
                status = "indexed" if chunk_count > 0 else "registered"
                error_count = 0
            except Exception as e:
                logger.warning(f"Failed to get stats for project {proj_name}: {e}")
                chunk_count = 0
                file_hashes = {}
                last_indexed = ""
                status = "error"
                error_count = 1

            projects.append(ProjectInfo(
                name=proj_name,
                path=proj["path"],
                type="code",
                total_chunks=chunk_count,
                last_indexed=last_indexed,
                file_count=len(file_hashes),
                status=status,
                error_count=error_count,
            ))

        logger.info(f"Listed {len(projects)} projects")
        return projects

    def ingest_registered_project(self, project_name: str, full_reindex: bool = False) -> IngestResult:
        """Ingest a configured project by name."""
        for proj in self.config.project_paths:
            candidate_name = proj.get("name", Path(proj["path"]).name)
            if candidate_name == project_name:
                return self.ingest_project(proj["path"], candidate_name, full_reindex=full_reindex)

        return IngestResult(
            files_processed=0,
            files_skipped=0,
            chunks_created=0,
            errors=[f"Configured project not found: {project_name}"],
            duration_seconds=0,
        )

    def get_stats(self) -> dict:
        """Get system-wide stats."""
        stats = {
            "collections": {},
            "total_chunks": 0,
        }

        for col in [
            self.config.docs_collection,
            self.config.code_collection,
            self.config.discoveries_collection,
            self.config.conversations_collection,
            self.config.facts_collection,
            self.config.runbooks_collection,
            self.config.project_overviews_collection,
            self.config.memory_cards_collection,
        ]:
            try:
                count = self.store.count(col)
                stats["collections"][col] = count
                stats["total_chunks"] += count
            except Exception as e:
                logger.warning(f"Failed to count collection {col}: {e}")
                stats["collections"][col] = 0

        stats["embedding_provider"] = self.embedder._resolved_provider()
        stats["embedding_model"] = self.embedder.model_name
        stats["embedding_dimension"] = self.embedder.dimension
        stats["data_dir"] = self.config.data_dir
        stats["memory_registry_path"] = self.config.memory_registry_path
        stats["memory_registry_cards"] = self.memory_registry.count()
        stats["obsidian_vault_configured"] = bool(self.config.obsidian_vault_path)
        stats["code_projects_configured"] = len(self.config.project_paths)

        logger.debug(f"System stats: {stats}")
        return stats

    def get_project_audit(self) -> dict:
        """Return a health/audit summary for configured projects."""
        projects = self.list_projects()
        indexed = [p for p in projects if p.type == "code" and p.total_chunks > 0]
        registered = [p for p in projects if p.type == "code" and p.total_chunks == 0 and p.status == "registered"]
        errored = [p for p in projects if p.status == "error"]

        next_unindexed = registered[0].name if registered else ""

        return {
            "summary": {
                "total_projects": len(projects),
                "code_projects": len([p for p in projects if p.type == "code"]),
                "indexed_code_projects": len(indexed),
                "registered_unindexed_code_projects": len(registered),
                "errored_projects": len(errored),
                "next_unindexed_project": next_unindexed,
            },
            "projects": [p.model_dump() for p in projects],
        }

    def clear_collection(self, collection: str) -> bool:
        """Wipe a collection completely."""
        logger.warning(f"Clearing collection '{collection}' - all data will be deleted!")
        try:
            self.store.clear_collection(collection)
            self.keyword_index.clear_collection(collection)
            logger.info(f"Successfully cleared collection '{collection}'")
            return True
        except Exception as e:
            logger.error(f"Failed to clear collection '{collection}': {e}")
            return False

    # === STRUCTURED MEMORY CARDS ===

    def store_memory_card(self, card: MemoryCard) -> MemoryCard:
        """Store an extracted atomic memory card in SQLite, ChromaDB, and FTS."""
        stored = self.memory_registry.upsert_card(card)
        collection = self.config.memory_cards_collection
        chroma_id = f"memory_{stored.card_id}"
        content = stored.to_embedding_text()
        metadata = stored.to_metadata()
        embedding = self._embed_for_ingest([content])[0]

        existing = self.store.get(collection, ids=[chroma_id])
        if existing.get("ids"):
            self.store.update(
                collection=collection,
                ids=[chroma_id],
                documents=[content],
                embeddings=[embedding],
                metadatas=[metadata],
            )
        else:
            self.store.add(
                collection=collection,
                ids=[chroma_id],
                documents=[content],
                embeddings=[embedding],
                metadatas=[metadata],
            )
        self.keyword_index.upsert_chunks(collection, [chroma_id], [content], [metadata])
        return stored

    def list_memory_cards(
        self,
        project: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        current_truth: bool | None = None,
        limit: int = 100,
    ) -> list[MemoryCard]:
        """List structured memory cards from the SQLite registry."""
        return self.memory_registry.list_cards(
            project=project,
            memory_type=memory_type,
            status=status,
            current_truth=current_truth,
            limit=limit,
        )

    def search_memory_cards(
        self,
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        max_results: int = 8,
        min_score_threshold: float = 0.25,
    ) -> SearchResponse:
        """Search extracted memory cards only."""
        return self.search(
            query=query,
            project=project,
            category=memory_type,
            max_results=max_results,
            min_score_threshold=min_score_threshold,
            collections=[self.config.memory_cards_collection],
        )

    def update_memory_card_status(
        self,
        card_id: str,
        status: str,
        current_truth: bool | None = None,
    ) -> MemoryCard | None:
        """Update memory card lifecycle state and sync Chroma metadata."""
        card = self.memory_registry.update_status(card_id, status, current_truth)
        if not card:
            return None
        return self.store_memory_card(card)

    def get_memory_audit(self) -> dict:
        """Return structured memory card counts by status/type/project."""
        audit = self.memory_registry.audit()
        audit["collection"] = self.config.memory_cards_collection
        audit["registry_path"] = self.config.memory_registry_path
        return audit

    def import_memory_cards(self, cards: list[MemoryCard]) -> list[MemoryCard]:
        """Store a batch of normalized memory cards."""
        stored: list[MemoryCard] = []
        for card in cards:
            stored.append(self.store_memory_card(card))
        return stored

    def store_semantic_record(self, record: SemanticRecord) -> SemanticRecord:
        """Store a curated semantic record in the appropriate semantic collection."""
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collection = collection_map.get(record.record_type)
        if not collection:
            raise RuntimeError(f"Unsupported semantic record type: {record.record_type}")

        embedding = self.embedder.embed_query(record.content)
        chroma_id = f"semantic_{record.record_type}_{record.record_id}"
        self.store.add(
            collection=collection,
            ids=[chroma_id],
            documents=[record.content],
            embeddings=[embedding],
            metadatas=[record.to_metadata()]
        )
        return record

    def promote_discovery_to_semantic(self, discovery_id: str, record_type: str, title: str = "") -> SemanticRecord:
        """Promote a confirmed discovery into a semantic record."""
        discovery = self.get_discovery(discovery_id)
        if not discovery:
            raise RuntimeError(f"Discovery not found: {discovery_id}")
        if not discovery.confirmed:
            raise RuntimeError("Only confirmed discoveries can be promoted to semantic memory")

        semantic_title = title or discovery.content.splitlines()[0][:120] or discovery.category
        record = SemanticRecord(
            title=semantic_title,
            content=discovery.content,
            project=discovery.project,
            record_type=record_type,
            tags=[discovery.category, discovery.severity],
            source_agent=discovery.source_agent,
            source_session=discovery.source_session,
            source_discovery_id=discovery.discovery_id,
            trust_level="T2",
            status="active",
            reviewed_at=discovery.confirmed_at or discovery.reviewed_at,
            confidence=max(discovery.confidence, 0.9),
        )
        stored = self.store_semantic_record(record)

        discovery.status = "active"
        discovery.trust_level = "T2"
        discovery.promoted_semantic_record_id = stored.record_id
        discovery.promoted_semantic_record_type = stored.record_type
        discovery.updated_at = datetime.now(timezone.utc).isoformat()
        self.discovery_manager.update(
            discovery.discovery_id,
            {
                "trust_level": discovery.trust_level,
                "status": discovery.status,
                "promoted_semantic_record_id": discovery.promoted_semantic_record_id,
                "promoted_semantic_record_type": discovery.promoted_semantic_record_type,
                "updated_at": discovery.updated_at,
            },
        )

        return stored

    def list_semantic_records(
        self,
        record_type: str | None = None,
        project: str | None = None,
        status: str = "active",
        limit: int = 100,
    ) -> list[SemanticRecord]:
        """List semantic records across semantic collections."""
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collections = [collection_map[record_type]] if record_type in collection_map else list(collection_map.values())

        results: list[SemanticRecord] = []
        for collection in collections:
            where = {"status": status}
            if project:
                where = {"$and": [{"status": status}, {"project": project}]}
            data = self.store.get(collection, where=where, limit=limit)
            for i, _ in enumerate(data.get("ids", [])):
                content = data["documents"][i]
                metadata = data["metadatas"][i]
                results.append(SemanticRecord.from_metadata(metadata, content))
        return results

    def update_semantic_record_status(self, record_id: str, record_type: str, status: str, superseded_by: str = "") -> bool:
        """Archive or supersede a semantic record by record id/type."""
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collection = collection_map.get(record_type)
        if not collection:
            raise RuntimeError(f"Unsupported semantic record type: {record_type}")

        chroma_id = f"semantic_{record_type}_{record_id}"
        data = self.store.get(collection, ids=[chroma_id])
        if not data.get("ids"):
            return False

        content = data["documents"][0]
        metadata = data["metadatas"][0]
        record = SemanticRecord.from_metadata(metadata, content)
        record.status = status
        record.superseded_by = superseded_by
        record.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.update(collection, ids=[chroma_id], metadatas=[record.to_metadata()])
        return True

    def mark_semantic_reviewed(self, record_id: str, record_type: str) -> bool:
        """Touch reviewed_at timestamp on a semantic record without changing status."""
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collection = collection_map.get(record_type)
        if not collection:
            raise RuntimeError(f"Unsupported semantic record type: {record_type}")

        chroma_id = f"semantic_{record_type}_{record_id}"
        data = self.store.get(collection, ids=[chroma_id])
        if not data.get("ids"):
            return False

        metadata = data["metadatas"][0]
        now = datetime.now(timezone.utc).isoformat()
        metadata["reviewed_at"] = now
        metadata["updated_at"] = now
        self.store.update(collection, ids=[chroma_id], metadatas=[metadata])
        return True

    def get_stale_records(self, stale_days: int = 30, project: str | None = None) -> list[SemanticRecord]:
        """Return active semantic records that have never been reviewed or were last reviewed
        more than stale_days ago."""
        active = self.list_semantic_records(status="active", project=project, limit=1000)
        now = datetime.now(timezone.utc)
        stale = []
        for r in active:
            if not r.reviewed_at:
                stale.append(r)
            else:
                try:
                    reviewed = datetime.fromisoformat(r.reviewed_at.replace("Z", "+00:00"))
                    if (now - reviewed).days >= stale_days:
                        stale.append(r)
                except (ValueError, TypeError):
                    stale.append(r)
        return stale

    def replace_semantic_record(
        self,
        old_record_id: str,
        record_type: str,
        new_title: str,
        new_content: str,
        new_project: str = "",
        new_tags: list[str] | None = None,
        new_confidence: float = 0.9,
    ) -> tuple[SemanticRecord, bool]:
        """Create a new semantic record and supersede the old one atomically.

        Returns (new_record, old_superseded_ok).
        """
        # Fetch old record for context
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collection = collection_map.get(record_type)
        if not collection:
            raise RuntimeError(f"Unsupported semantic record type: {record_type}")

        chroma_id = f"semantic_{record_type}_{old_record_id}"
        data = self.store.get(collection, ids=[chroma_id])
        old_project = new_project
        if data.get("ids"):
            old_meta = data["metadatas"][0]
            old_project = new_project or old_meta.get("project", "")

        new_record = SemanticRecord(
            title=new_title,
            content=new_content,
            project=old_project,
            record_type=record_type,
            tags=new_tags or [],
            trust_level="T2",
            status="active",
            reviewed_at=datetime.now(timezone.utc).isoformat(),
            confidence=new_confidence,
        )
        stored = self.store_semantic_record(new_record)
        superseded_ok = self.update_semantic_record_status(
            old_record_id, record_type, "superseded", stored.record_id
        )
        return stored, superseded_ok

    def search_semantic_records(
        self,
        query: str,
        record_type: str | None = None,
        project: str | None = None,
        max_results: int = 6,
        min_score_threshold: float = 0.35,
    ) -> SearchResponse:
        """Search semantic collections only."""
        collection_map = {
            "fact": self.config.facts_collection,
            "runbook": self.config.runbooks_collection,
            "project_overview": self.config.project_overviews_collection,
        }
        collections = [collection_map[record_type]] if record_type in collection_map else [
            self.config.facts_collection,
            self.config.runbooks_collection,
            self.config.project_overviews_collection,
        ]
        return self.search(
            query=query,
            project=project,
            max_results=max_results,
            min_score_threshold=min_score_threshold,
            collections=collections,
        )

    def get_semantic_audit(self) -> dict:
        """Return a lifecycle-oriented audit summary for semantic memory."""
        active = self.list_semantic_records(status="active", limit=1000)
        archived = self.list_semantic_records(status="archived", limit=1000)
        superseded = self.list_semantic_records(status="superseded", limit=1000)

        by_project: dict[str, int] = {}
        by_type: dict[str, int] = {}
        linkback_count = 0
        stale_without_review = 0
        for record in active + archived + superseded:
            by_project[record.project or "-"] = by_project.get(record.project or "-", 0) + 1
            by_type[record.record_type] = by_type.get(record.record_type, 0) + 1
            if record.source_discovery_id:
                linkback_count += 1
            if not record.reviewed_at:
                stale_without_review += 1

        discoveries = self.get_discoveries()
        promoted_discoveries = [d for d in discoveries if d.promoted_semantic_record_id]
        confirmed_unpromoted = [d for d in discoveries if d.confirmed and not d.promoted_semantic_record_id]
        superseded_without_replacement = [r for r in superseded if not r.superseded_by]

        indexed_projects = [p for p in self.list_projects() if p.type == "code" and p.total_chunks > 0]
        semantic_projects = {r.project for r in active if r.project}
        coverage_gap_projects = [p.name for p in indexed_projects if p.name not in semantic_projects]

        return {
            "summary": {
                "active_records": len(active),
                "archived_records": len(archived),
                "superseded_records": len(superseded),
                "records_with_discovery_linkback": linkback_count,
                "discoveries_with_semantic_linkback": len(promoted_discoveries),
                "records_missing_reviewed_at": stale_without_review,
                "confirmed_discoveries_not_promoted": len(confirmed_unpromoted),
                "coverage_gap_projects": len(coverage_gap_projects),
                "superseded_without_replacement": len(superseded_without_replacement),
            },
            "by_project": by_project,
            "by_type": by_type,
            "coverage_gap_projects": coverage_gap_projects,
            "promotion_candidates": [
                {
                    "discovery_id": d.discovery_id,
                    "project": d.project,
                    "category": d.category,
                    "severity": d.severity,
                    "content_preview": d.content[:160],
                }
                for d in confirmed_unpromoted[:50]
            ],
            "stale_candidates": [
                {
                    "record_id": r.record_id,
                    "record_type": r.record_type,
                    "project": r.project,
                    "title": r.title,
                    "created_at": r.created_at[:10] if r.created_at else "",
                    "reviewed_at": r.reviewed_at[:10] if r.reviewed_at else "never",
                    "age_days": (datetime.now(timezone.utc) - datetime.fromisoformat(
                        r.created_at.replace("Z", "+00:00")
                    )).days if r.created_at else 0,
                }
                for r in active if not r.reviewed_at
            ][:50],
        }

    def suggest_promotions(self, project: str | None = None, limit: int = 20) -> list[dict]:
        """Suggest confirmed discoveries that should likely be promoted."""
        discoveries = self.get_discoveries(project=project)
        candidates = [d for d in discoveries if d.confirmed and not d.promoted_semantic_record_id]
        suggestions = []
        for d in candidates[:limit]:
            suggested_type = "fact"
            if d.category in ["workaround", "config"]:
                suggested_type = "runbook"
            elif d.category in ["pattern", "dependency"]:
                suggested_type = "project_overview"
            title = d.content.splitlines()[0][:120]
            suggestions.append({
                "discovery_id": d.discovery_id,
                "project": d.project,
                "category": d.category,
                "severity": d.severity,
                "suggested_record_type": suggested_type,
                "title": title,
                "content_preview": d.content[:160],
                "promote_command": (
                    f'knowledgeforge semantic promote-discovery "{d.discovery_id}" '
                    f'{suggested_type} --title "{title}"'
                ),
            })
        return suggestions

    def generate_project_overview(self, project: str) -> SemanticRecord:
        """Generate and store a first-pass project overview from indexed knowledge."""
        docs = self.search(query=f"{project} architecture overview", project=project, collections=[self.config.docs_collection], max_results=3)
        code = self.search(query=f"{project} main entry point architecture", project=project, collections=[self.config.code_collection], max_results=3)
        discoveries = self.get_discoveries(project=project)[:5]

        lines = [f"Project overview for {project}.", "", "Documentation signals:"]
        for r in docs.results:
            source = r.metadata.get("file_path") or r.metadata.get("source_file", "?")
            lines.append(f"- [{source}] {r.content[:180]}")
        lines.append("\nCode signals:")
        for r in code.results:
            source = r.metadata.get("file_path") or r.metadata.get("source_file", "?")
            symbol = r.metadata.get("symbol_name", "")
            lines.append(f"- [{source}] {symbol}: {r.content[:140]}")
        lines.append("\nRecent discoveries:")
        for d in discoveries:
            lines.append(f"- [{d.category}/{d.severity}] {d.content[:140]}")

        record = SemanticRecord(
            title=f"{project} overview",
            content="\n".join(lines),
            project=project,
            record_type="project_overview",
            tags=["overview", "bootstrap"],
            trust_level="T2",
            status="active",
            confidence=0.8,
        )
        return self.store_semantic_record(record)

    def bootstrap_project_semantic_coverage(self, project: str) -> dict:
        """Bootstrap semantic coverage for a project with an overview and promotion suggestions."""
        overview = self.generate_project_overview(project)
        suggestions = self.suggest_promotions(project=project, limit=10)
        return {
            "project": project,
            "overview_record_id": overview.record_id,
            "overview_title": overview.title,
            "suggested_promotions": suggestions,
        }

    # === CONVERSATIONS ===

    def ingest_conversations(
        self,
        source_dirs: list[str] = None,
        enrichment_dir: str = None,
        full_reindex: bool = False
    ) -> IngestResult:
        """Index conversations into ChromaDB 'conversations' collection.

        Scans source directories for JSONL files, parses exchanges,
        generates embeddings, and stores in ChromaDB.

        Args:
            source_dirs: Dirs to scan for JSONL files (defaults to config)
            enrichment_dir: Path to enriched JSON files (Kimi metadata)
            full_reindex: Wipe collection before re-indexing

        Returns:
            IngestResult with statistics
        """
        from knowledgeforge.ingestion.conversations import (
            scan_conversation_dirs, parse_jsonl_file,
            load_enrichment_data, chunk_exchange
        )

        start = time.time()
        collection = self.config.conversations_collection

        if source_dirs is None:
            source_dirs = self.config.conversation_sources

        logger.info(f"Starting conversation ingestion (full_reindex={full_reindex})")

        if full_reindex:
            logger.info(f"Clearing collection '{collection}' for full reindex")
            self.store.clear_collection(collection)
            self.keyword_index.clear_collection(collection)
        else:
            existing_count = self.store.count(collection)
            logger.info(
                "Incremental ingestion: %s existing conversation chunks",
                existing_count if existing_count >= 0 else "unknown",
            )

        # Load enrichment data
        enrich_dir = enrichment_dir or self.config.conversation_enrichment_dir
        enrichment_map = {}
        if enrich_dir:
            enrichment_map = load_enrichment_data(enrich_dir)

        # Scan for JSONL files
        jsonl_files = scan_conversation_dirs(source_dirs)

        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        errors = []

        for fpath in jsonl_files:
            try:
                exchanges = parse_jsonl_file(
                    fpath,
                    enrichment_map=enrichment_map,
                    max_tool_result_chars=self.config.conversation_max_tool_result_chars,
                )

                if not exchanges:
                    files_skipped += 1
                    continue

                # Chunk, embed, and store each exchange
                all_chunks = []
                for ex in exchanges:
                    all_chunks.extend(chunk_exchange(ex))

                if not full_reindex:
                    candidate_ids = [cid for cid, _, _ in all_chunks]
                    existing_ids = self.store.existing_ids(collection, candidate_ids)
                    all_chunks = [
                        (cid, content, meta)
                        for cid, content, meta in all_chunks
                        if cid not in existing_ids
                    ]

                if not all_chunks:
                    files_skipped += 1
                    continue

                # Batch embed and store
                ids = [c[0] for c in all_chunks]
                contents = [c[1] for c in all_chunks]
                metadatas = [c[2] for c in all_chunks]
                embeddings = self._embed_for_ingest(contents)

                self.store.add(collection, ids, contents, embeddings, metadatas)
                self.keyword_index.upsert_chunks(collection, ids, contents, metadatas)

                files_processed += 1
                chunks_created += len(all_chunks)
                logger.debug(f"Indexed {fpath}: {len(all_chunks)} chunks from {len(exchanges)} exchanges")

            except Exception as e:
                error_msg = f"Error processing {fpath}: {e}"
                errors.append(error_msg)
                logger.error(error_msg)

        duration = time.time() - start
        result = IngestResult(
            files_processed=files_processed,
            files_skipped=files_skipped,
            chunks_created=chunks_created,
            errors=errors,
            duration_seconds=round(duration, 2)
        )
        logger.info(f"Conversation ingestion complete: {result}")
        return result

    def search_conversations(
        self,
        query: str,
        project: str = None,
        source_agent: str = None,
        after: str = None,
        before: str = None,
        n_results: int = 10,
        min_score: float = 0.0
    ) -> SearchResponse:
        """Semantic search over conversations collection.

        Args:
            query: Search query text
            project: Filter by project name
            source_agent: Filter by agent type ("claude", "codex", "gemini")
            after: Only include results after this date (YYYY-MM-DD)
            before: Only include results before this date (YYYY-MM-DD)
            n_results: Maximum results to return
            min_score: Minimum similarity score threshold

        Returns:
            SearchResponse with results sorted by relevance
        """
        start_time = time.time()
        collection = self.config.conversations_collection

        query_embedding = self.embedder.embed_query(query)

        # Build where filter
        filters = []
        if project:
            filters.append({"project": project})
        if source_agent:
            filters.append({"source_agent": source_agent})
        if after:
            filters.append({"timestamp": {"$gte": after}})
        if before:
            filters.append({"timestamp": {"$lte": before}})

        where = None
        if len(filters) == 1:
            where = filters[0]
        elif len(filters) > 1:
            where = {"$and": filters}

        all_results = []
        try:
            raw = self.store.query(
                collection=collection,
                query_embedding=query_embedding,
                n_results=n_results,
                where=where,
            )

            if raw["ids"] and raw["ids"][0]:
                for i in range(len(raw["ids"][0])):
                    distance = raw["distances"][0][i]
                    score = 1.0 - (distance / 2.0)
                    if score >= min_score:
                        all_results.append(SearchResult(
                            content=raw["documents"][0][i],
                            score=round(score, 4),
                            metadata=raw["metadatas"][0][i],
                            collection=collection,
                        ))
        except Exception as e:
            logger.warning(f"Error searching conversations: {e}")

        all_results.sort(key=lambda r: r.score, reverse=True)

        search_time = (time.time() - start_time) * 1000
        logger.info(f"Conversation search: query='{query[:50]}...', results={len(all_results)}, time={search_time:.2f}ms")

        return SearchResponse(
            query=query,
            results=all_results[:n_results],
            total_results=len(all_results),
            search_time_ms=round(search_time, 2),
        )

    def get_conversation(self, session_id: str, start_line: int = None, end_line: int = None) -> str:
        """Read a raw conversation from JSONL archive, formatted as markdown.

        Args:
            session_id: Session UUID to look up
            start_line: Optional starting line (1-indexed)
            end_line: Optional ending line (1-indexed)

        Returns:
            Formatted markdown string of the conversation
        """
        import json as _json

        # Find the JSONL file by session_id
        jsonl_path = self._find_session_file(session_id)
        if not jsonl_path:
            return f"Session {session_id} not found."

        lines = []
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line_num, raw_line in enumerate(f, start=1):
                if start_line and line_num < start_line:
                    continue
                if end_line and line_num > end_line:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = _json.loads(raw_line)
                except _json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")
                if msg_type == "user":
                    msg = data.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                        content = "\n".join(text_parts)
                    ts = data.get("timestamp", "")
                    lines.append(f"### User ({ts})\n{content}\n")

                elif msg_type == "assistant":
                    msg = data.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))
                                elif block.get("type") == "tool_use":
                                    text_parts.append(f"*Tool: {block.get('name', '?')}*")
                        content = "\n".join(text_parts)
                    lines.append(f"### Assistant\n{content}\n")

                elif msg_type == "tool_result":
                    result = str(data.get("content", ""))[:500]
                    lines.append(f"*Tool result:* {result}\n")

        if not lines:
            return f"No content found for session {session_id}."

        return f"# Conversation: {session_id}\nSource: {jsonl_path}\n\n" + "\n".join(lines)

    def list_conversation_sessions(
        self,
        project: str = None,
        source_agent: str = None,
        after: str = None,
        before: str = None,
        limit: int = 200,
    ) -> dict:
        """List indexed conversation sessions grouped from exchange records."""
        collection = self.config.conversations_collection

        filters = []
        if project:
            filters.append({"project": project})
        if source_agent:
            filters.append({"source_agent": source_agent})
        if after:
            filters.append({"timestamp": {"$gte": after}})
        if before:
            filters.append({"timestamp": {"$lte": before}})

        where = None
        if len(filters) == 1:
            where = filters[0]
        elif len(filters) > 1:
            where = {"$and": filters}

        try:
            result = self.store.get(collection, where=where)
        except Exception as e:
            logger.warning(f"Failed to list conversation sessions: {e}")
            return {"total_sessions": 0, "sessions": []}

        sessions = {}
        documents = result.get("documents", [])
        metadatas = result.get("metadatas", [])

        for document, meta in zip(documents, metadatas):
            if not meta:
                continue

            session_id = meta.get("session_id")
            if not session_id:
                continue

            timestamp = str(meta.get("timestamp", ""))
            summary_hint = ""
            if isinstance(document, str):
                for line in document.splitlines():
                    if line.startswith("Summary:"):
                        summary_hint = line.replace("Summary:", "", 1).strip()
                        break

            session = sessions.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "project": meta.get("project", ""),
                    "source_agent": meta.get("source_agent", "unknown"),
                    "exchange_count": 0,
                    "first_timestamp": timestamp,
                    "last_timestamp": timestamp,
                    "archive_path": meta.get("archive_path", ""),
                    "tool_names": set(),
                    "summary_hint": "",
                    "category": meta.get("category", ""),
                    "intent": meta.get("intent", ""),
                },
            )

            session["exchange_count"] += 1

            if timestamp and (not session["first_timestamp"] or timestamp < session["first_timestamp"]):
                session["first_timestamp"] = timestamp
            if timestamp and (not session["last_timestamp"] or timestamp > session["last_timestamp"]):
                session["last_timestamp"] = timestamp

            if not session["summary_hint"] and summary_hint:
                session["summary_hint"] = summary_hint
            if not session["archive_path"] and meta.get("archive_path"):
                session["archive_path"] = meta.get("archive_path", "")
            if not session["category"] and meta.get("category"):
                session["category"] = meta.get("category", "")
            if not session["intent"] and meta.get("intent"):
                session["intent"] = meta.get("intent", "")

            raw_tool_names = str(meta.get("tool_names", "") or "")
            for tool_name in raw_tool_names.split(","):
                cleaned = tool_name.strip()
                if cleaned:
                    session["tool_names"].add(cleaned)

        ordered_sessions = sorted(
            sessions.values(),
            key=lambda item: item.get("last_timestamp", ""),
            reverse=True,
        )

        serialized = []
        for session in ordered_sessions[: max(1, limit)]:
            serialized.append(
                {
                    **session,
                    "tool_names": sorted(session["tool_names"]),
                }
            )

        return {
            "total_sessions": len(ordered_sessions),
            "sessions": serialized,
        }

    def _find_session_file(self, session_id: str) -> Optional[str]:
        """Find a JSONL file by session ID across all conversation sources."""
        import os as _os

        # Check conversation sources
        for source_dir in self.config.conversation_sources:
            if not _os.path.isdir(source_dir):
                continue
            for root, dirs, files in _os.walk(source_dir):
                for fname in files:
                    if session_id in fname and fname.endswith(".jsonl"):
                        return _os.path.join(root, fname)

        # Check archive dir
        archive = self.config.conversation_archive_dir
        if archive and _os.path.isdir(archive):
            for root, dirs, files in _os.walk(archive):
                for fname in files:
                    if session_id in fname and fname.endswith(".jsonl"):
                        return _os.path.join(root, fname)

        return None

    def sync_conversations(self) -> dict:
        """Index new conversation files.

        Returns:
            Stats dict: {indexed, skipped, errors}
        """
        result = self.ingest_conversations()
        return {
            "indexed": result.files_processed,
            "skipped": result.files_skipped,
            "chunks_created": result.chunks_created,
            "errors": result.errors,
            "duration_seconds": result.duration_seconds,
        }

    def get_conversation_stats(self) -> dict:
        """Return conversation-specific stats."""
        collection = self.config.conversations_collection
        try:
            count = self.store.count(collection)
        except Exception:
            count = 0

        stats = {
            "total_exchanges": count,
            "collection": collection,
            "source_dirs": self.config.conversation_sources,
        }

        # Get per-agent and per-project counts
        if count > 0:
            try:
                result = self.store.get(collection)
                agents = {}
                projects = {}
                for meta in result.get("metadatas", []):
                    if meta:
                        agent = meta.get("source_agent", "unknown")
                        proj = meta.get("project", "unknown")
                        agents[agent] = agents.get(agent, 0) + 1
                        projects[proj] = projects.get(proj, 0) + 1
                stats["by_agent"] = agents
                stats["by_project"] = projects
            except Exception as e:
                logger.warning(f"Failed to get conversation breakdown: {e}")

        return stats
