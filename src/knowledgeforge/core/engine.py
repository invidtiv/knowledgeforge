"""Central KnowledgeForge engine that orchestrates all components.

This is the main entry point for both MCP and REST interfaces.
It knows nothing about transport layers - it's a pure Python class that
coordinates the vector store, embedder, parsers, and discovery system.
"""

import time
import logging
from pathlib import Path
from typing import Optional

from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.core.embedder import Embedder
from knowledgeforge.core.store import VectorStore
from knowledgeforge.core.models import (
    Chunk, Discovery, SearchResult, SearchResponse, IngestResult, ProjectInfo
)
from knowledgeforge.ingestion.obsidian import ObsidianParser
from knowledgeforge.ingestion.code import CodeParser
from knowledgeforge.ingestion.chunker import compute_file_hash
from knowledgeforge.discovery.manager import DiscoveryManager
from knowledgeforge.discovery.promoter import DiscoveryPromoter

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
        self.embedder = Embedder(config.embedding_model, config.embedding_device)
        self.store = VectorStore(config.chroma_persist_dir)

        # Initialize parsers only if paths are configured
        self.obsidian_parser = None
        if config.obsidian_vault_path:
            self.obsidian_parser = ObsidianParser(config.obsidian_vault_path, config)

        self.code_parser = CodeParser(config)
        self.discovery_manager = DiscoveryManager(self.store, self.embedder, config)

        self.discovery_promoter = None
        if config.obsidian_vault_path:
            self.discovery_promoter = DiscoveryPromoter(
                config.obsidian_vault_path,
                config.obsidian_discoveries_folder
            )

        logger.info("KnowledgeForgeEngine initialized successfully")

    # === SEARCH ===

    def search(
        self,
        query: str,
        collections: list[str] = None,
        project: str = None,
        tags: list[str] = None,
        language: str = None,
        category: str = None,
        confirmed_only: bool = False,
        n_results: int = 10,
        min_score: float = 0.0
    ) -> SearchResponse:
        """Unified search across all collections.

        Steps:
        1. Embed query using embedder.embed_query()
        2. Determine which collections to search (default: all three)
        3. Build where-filters from parameters for each collection
        4. Query each collection
        5. Convert ChromaDB distances to similarity scores (score = 1 - distance/2 for cosine)
        6. Filter by min_score
        7. Merge results, sort by score descending
        8. Return top n_results

        ChromaDB cosine distance: 0 = identical, 2 = opposite
        Convert to score: score = 1 - (distance / 2)  -> range [0, 1]

        Args:
            query: Search query text
            collections: List of collection names to search (default: all)
            project: Filter by project name
            tags: Filter by tags (Obsidian only)
            language: Filter by programming language (code only)
            category: Filter by discovery category (discoveries only)
            confirmed_only: Only return confirmed discoveries
            n_results: Maximum number of results to return
            min_score: Minimum similarity score threshold (0.0 to 1.0)

        Returns:
            SearchResponse with results sorted by relevance
        """
        start_time = time.time()

        if collections is None:
            collections = [
                self.config.docs_collection,
                self.config.code_collection,
                self.config.discoveries_collection
            ]

        query_embedding = self.embedder.embed_query(query)
        all_results = []

        for col_name in collections:
            where = self._build_where_filter(col_name, project, tags, language, category, confirmed_only)

            try:
                raw = self.store.query(
                    collection=col_name,
                    query_embedding=query_embedding,
                    n_results=n_results,
                    where=where if where else None
                )

                # Process results
                if raw["ids"] and raw["ids"][0]:
                    for i in range(len(raw["ids"][0])):
                        distance = raw["distances"][0][i]
                        score = 1.0 - (distance / 2.0)  # Convert cosine distance to similarity

                        if score >= min_score:
                            all_results.append(SearchResult(
                                content=raw["documents"][0][i],
                                score=round(score, 4),
                                metadata=raw["metadatas"][0][i],
                                collection=col_name
                            ))
            except Exception as e:
                logger.warning(f"Error searching collection '{col_name}': {e}")

        # Sort by score descending, boost confirmed critical discoveries
        all_results.sort(key=lambda r: self._score_with_boost(r), reverse=True)

        # Take top n_results
        top_results = all_results[:n_results]

        search_time = (time.time() - start_time) * 1000  # ms

        logger.info(f"Search completed: query='{query[:50]}...', results={len(top_results)}, time={search_time:.2f}ms")

        return SearchResponse(
            query=query,
            results=top_results,
            total_results=len(all_results),
            search_time_ms=round(search_time, 2)
        )

    def _build_where_filter(self, collection, project, tags, language, category, confirmed_only):
        """Build ChromaDB where filter based on parameters and collection type.

        Args:
            collection: Collection name
            project: Project filter
            tags: Tags filter
            language: Language filter
            category: Category filter
            confirmed_only: Confirmed only filter

        Returns:
            ChromaDB where filter dict or None
        """
        filters = []

        if collection == self.config.docs_collection:
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
            existing_hashes = {}
        else:
            existing_hashes = self.store.get_file_hashes(collection)
            logger.info(f"Incremental ingestion: {len(existing_hashes)} files already indexed")

        # Parse vault
        chunks = self.obsidian_parser.parse_vault()

        # Group chunks by source file
        file_chunks = {}
        for chunk in chunks:
            file_chunks.setdefault(chunk.source_file, []).append(chunk)

        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        errors = []

        for source_file, file_chunk_list in file_chunks.items():
            file_hash = file_chunk_list[0].source_file_hash

            # Skip unchanged files
            if source_file in existing_hashes and existing_hashes[source_file] == file_hash:
                files_skipped += 1
                continue

            try:
                # Delete old chunks for this file
                if source_file in existing_hashes:
                    self.store.delete_by_source_file(collection, source_file)

                # Embed and store new chunks
                contents = [c.content for c in file_chunk_list]
                embeddings = self.embedder.embed_documents(contents)
                ids = [c.chunk_id for c in file_chunk_list]
                metadatas = [c.to_metadata() for c in file_chunk_list]

                self.store.add(collection, ids, contents, embeddings, metadatas)

                files_processed += 1
                chunks_created += len(file_chunk_list)
                logger.debug(f"Indexed {source_file}: {len(file_chunk_list)} chunks")
            except Exception as e:
                error_msg = f"Error processing {source_file}: {e}"
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

    def ingest_project(self, project_path: str, project_name: str, full_reindex: bool = False) -> IngestResult:
        """Ingest a code project directory. Same incremental logic as vault ingestion.

        Args:
            project_path: Absolute path to project directory
            project_name: Name of the project
            full_reindex: If True, clear this project's data and re-index

        Returns:
            IngestResult with statistics about the ingestion
        """
        start = time.time()
        collection = self.config.code_collection

        logger.info(f"Starting project ingestion: {project_name} (full_reindex={full_reindex})")

        if full_reindex:
            # Only clear chunks for this specific project
            logger.info(f"Clearing project '{project_name}' from collection '{collection}'")
            try:
                self.store.delete(collection, where={"project_name": project_name})
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
            file_chunks.setdefault(chunk.source_file, []).append(chunk)

        files_processed = 0
        files_skipped = 0
        chunks_created = 0
        errors = []

        for source_file, file_chunk_list in file_chunks.items():
            file_hash = file_chunk_list[0].source_file_hash

            if source_file in existing_hashes and existing_hashes[source_file] == file_hash:
                files_skipped += 1
                continue

            try:
                if source_file in existing_hashes:
                    self.store.delete_by_source_file(collection, source_file)

                contents = [c.content for c in file_chunk_list]
                embeddings = self.embedder.embed_documents(contents)
                ids = [c.chunk_id for c in file_chunk_list]
                metadatas = [c.to_metadata() for c in file_chunk_list]

                self.store.add(collection, ids, contents, embeddings, metadatas)

                files_processed += 1
                chunks_created += len(file_chunk_list)
                logger.debug(f"Indexed {source_file}: {len(file_chunk_list)} chunks")
            except Exception as e:
                error_msg = f"Error processing {source_file}: {e}"
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
        logger.info(f"Project ingestion complete: {result}")
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
            self.store.delete_by_source_file(collection, chunks[0].source_file)

            contents = [c.content for c in chunks]
            embeddings = self.embedder.embed_documents(contents)
            ids = [c.chunk_id for c in chunks]
            metadatas = [c.to_metadata() for c in chunks]

            self.store.add(collection, ids, contents, embeddings, metadatas)

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
        """List all indexed projects with stats.

        Returns:
            List of ProjectInfo objects
        """
        projects = []

        # Obsidian vault
        if self.config.obsidian_vault_path:
            doc_count = self.store.count(self.config.docs_collection)
            hashes = self.store.get_file_hashes(self.config.docs_collection)
            projects.append(ProjectInfo(
                name=Path(self.config.obsidian_vault_path).name,
                path=self.config.obsidian_vault_path,
                type="obsidian",
                total_chunks=doc_count,
                last_indexed="",  # Would need to track this separately
                file_count=len(hashes)
            ))

        # Code projects
        for proj in self.config.project_paths:
            proj_name = proj.get("name", Path(proj["path"]).name)
            # Get count for this project
            try:
                result = self.store.get(
                    self.config.code_collection,
                    where={"project_name": proj_name}
                )
                chunk_count = len(result.get("ids", []))
                file_hashes = {}
                for meta in result.get("metadatas", []):
                    if meta and "source_file" in meta:
                        file_hashes[meta["source_file"]] = True
            except Exception as e:
                logger.warning(f"Failed to get stats for project {proj_name}: {e}")
                chunk_count = 0
                file_hashes = {}

            projects.append(ProjectInfo(
                name=proj_name,
                path=proj["path"],
                type="code",
                total_chunks=chunk_count,
                last_indexed="",
                file_count=len(file_hashes)
            ))

        logger.info(f"Listed {len(projects)} projects")
        return projects

    def get_stats(self) -> dict:
        """Get system-wide stats.

        Returns:
            Dictionary with system statistics
        """
        stats = {
            "collections": {},
            "total_chunks": 0,
        }

        for col in [
            self.config.docs_collection,
            self.config.code_collection,
            self.config.discoveries_collection
        ]:
            try:
                count = self.store.count(col)
                stats["collections"][col] = count
                stats["total_chunks"] += count
            except Exception as e:
                logger.warning(f"Failed to count collection {col}: {e}")
                stats["collections"][col] = 0

        stats["embedding_model"] = self.config.embedding_model
        stats["data_dir"] = self.config.data_dir
        stats["obsidian_vault_configured"] = bool(self.config.obsidian_vault_path)
        stats["code_projects_configured"] = len(self.config.project_paths)

        logger.debug(f"System stats: {stats}")
        return stats

    def clear_collection(self, collection: str) -> bool:
        """Wipe a collection completely.

        WARNING: This permanently deletes all data!

        Args:
            collection: Collection name to clear

        Returns:
            True if cleared successfully
        """
        logger.warning(f"Clearing collection '{collection}' - all data will be deleted!")
        try:
            self.store.clear_collection(collection)
            logger.info(f"Successfully cleared collection '{collection}'")
            return True
        except Exception as e:
            logger.error(f"Failed to clear collection '{collection}': {e}")
            return False
