"""ChromaDB wrapper managing three collections for KnowledgeForge.

This module provides a clean interface to ChromaDB with:
- Embedded persistent storage (no server process needed)
- Three collections: documents, codebase, discoveries
- Lazy collection initialization
- Pre-computed embeddings (managed separately by Embedder class)
- Graceful error handling
- Incremental indexing support via file hash tracking
"""

import logging
import os
import threading
from typing import Optional

import chromadb

logger = logging.getLogger(__name__)


class VectorStore:
    """ChromaDB wrapper managing three collections for KnowledgeForge.

    Collections:
    - documents: Obsidian vault content (markdown chunks)
    - codebase: Code repository content (functions, classes, files)
    - discoveries: AI agent insights, bug fixes, learnings

    All collections use pre-computed embeddings from the Embedder class.
    Metadata is kept ChromaDB-compliant (str, int, float, bool only).
    """

    def __init__(self, persist_dir: str):
        """Initialize ChromaDB with persistent storage.

        Args:
            persist_dir: Directory for ChromaDB persistent storage
                        (will be created if it doesn't exist)
        """
        self.persist_dir = persist_dir
        self._client = None
        self._collections: dict = {}
        self._lock = threading.RLock()
        logger.debug(f"VectorStore initialized with persist_dir={persist_dir}")

    @property
    def client(self) -> chromadb.ClientAPI:
        """Lazy-initialize ChromaDB client.

        Creates persistent storage directory if needed.
        Only initializes on first access to avoid startup overhead.

        Returns:
            ChromaDB client instance
        """
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        os.makedirs(self.persist_dir, exist_ok=True)
                        self._client = chromadb.PersistentClient(path=self.persist_dir)
                        logger.info(f"ChromaDB initialized at {self.persist_dir}")
                    except Exception as e:
                        logger.error(f"Failed to initialize ChromaDB: {e}")
                        raise RuntimeError(f"ChromaDB initialization failed: {e}") from e
        return self._client

    def _get_collection(self, name: str) -> chromadb.Collection:
        """Get or create a collection by name.

        Collections are created lazily on first access with cosine similarity.

        Args:
            name: Collection name (documents, codebase, or discoveries)

        Returns:
            ChromaDB Collection instance
        """
        if name not in self._collections:
            with self._lock:
                if name not in self._collections:
                    try:
                        self._collections[name] = self.client.get_or_create_collection(
                            name=name,
                            metadata={"hnsw:space": "cosine"}  # Cosine similarity for semantic search
                        )
                        logger.info(f"Collection '{name}' ready (count: {self._collections[name].count()})")
                    except Exception as e:
                        logger.error(f"Failed to get/create collection '{name}': {e}")
                        raise RuntimeError(f"Collection access failed for '{name}': {e}") from e
        return self._collections[name]

    def add(
        self,
        collection: str,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict]
    ) -> None:
        """Add documents with pre-computed embeddings to a collection.

        IMPORTANT: ChromaDB metadata only supports str, int, float, bool.
        Use Chunk.to_metadata() or Discovery.to_metadata() to clean metadata
        before passing here.

        Automatically batches large inserts (5000 items per batch) to avoid
        ChromaDB memory limits.

        Args:
            collection: Collection name (documents, codebase, or discoveries)
            ids: List of unique document IDs
            documents: List of document text content
            embeddings: List of pre-computed embedding vectors
            metadatas: List of metadata dicts (ChromaDB-compliant)

        Raises:
            RuntimeError: If ChromaDB operation fails
        """
        try:
            col = self._get_collection(collection)

            # ChromaDB has batch size limits, chunk into batches of 5000
            batch_size = 5000
            total_added = 0

            for i in range(0, len(ids), batch_size):
                end = min(i + batch_size, len(ids))
                col.add(
                    ids=ids[i:end],
                    documents=documents[i:end],
                    embeddings=embeddings[i:end],
                    metadatas=metadatas[i:end]
                )
                total_added += (end - i)
                logger.debug(f"Added batch {i//batch_size + 1}: {end - i} items to '{collection}'")

            logger.info(f"Successfully added {total_added} items to collection '{collection}'")

        except Exception as e:
            logger.error(f"Failed to add to collection '{collection}': {e}")
            raise RuntimeError(f"Add operation failed for '{collection}': {e}") from e

    def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None
    ) -> dict:
        """Query a collection with a pre-computed embedding.

        Args:
            collection: Collection name to query
            query_embedding: Pre-computed query embedding vector
            n_results: Maximum number of results to return (default: 10)
            where: Metadata filter (e.g., {"language": "python"})
            where_document: Document content filter (e.g., {"$contains": "async"})

        Returns:
            ChromaDB QueryResult dict with keys:
            - ids: list[list[str]] - Document IDs
            - documents: list[list[str]] - Document content
            - metadatas: list[list[dict]] - Document metadata
            - distances: list[list[float]] - Similarity distances (lower=better)

        Note:
            If collection is empty, returns empty result structure.
            n_results is capped at collection size to avoid errors.
        """
        try:
            col = self._get_collection(collection)
            count = col.count()

            # Handle empty collection
            if count == 0:
                logger.warning(f"Collection '{collection}' is empty, returning empty results")
                return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

            # Build query kwargs
            kwargs = {
                "query_embeddings": [query_embedding],
                "n_results": min(n_results, count),  # Don't request more than available
                "include": ["documents", "metadatas", "distances"]
            }

            if where:
                kwargs["where"] = where
            if where_document:
                kwargs["where_document"] = where_document

            results = col.query(**kwargs)
            logger.debug(f"Query returned {len(results['ids'][0])} results from '{collection}'")
            return results

        except Exception as e:
            logger.error(f"Failed to query collection '{collection}': {e}")
            raise RuntimeError(f"Query operation failed for '{collection}': {e}") from e

    def update(
        self,
        collection: str,
        ids: list[str],
        documents: Optional[list[str]] = None,
        embeddings: Optional[list[list[float]]] = None,
        metadatas: Optional[list[dict]] = None
    ) -> None:
        """Update existing documents in a collection.

        Only provided fields are updated; others remain unchanged.

        Args:
            collection: Collection name
            ids: List of document IDs to update
            documents: Optional new document content
            embeddings: Optional new embeddings
            metadatas: Optional new metadata

        Raises:
            RuntimeError: If update operation fails
        """
        try:
            col = self._get_collection(collection)
            kwargs = {"ids": ids}

            if documents is not None:
                kwargs["documents"] = documents
            if embeddings is not None:
                kwargs["embeddings"] = embeddings
            if metadatas is not None:
                kwargs["metadatas"] = metadatas

            col.update(**kwargs)
            logger.info(f"Updated {len(ids)} items in collection '{collection}'")

        except Exception as e:
            logger.error(f"Failed to update collection '{collection}': {e}")
            raise RuntimeError(f"Update operation failed for '{collection}': {e}") from e

    def delete(
        self,
        collection: str,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None
    ) -> None:
        """Delete documents from a collection by IDs or filter.

        Args:
            collection: Collection name
            ids: Optional list of document IDs to delete
            where: Optional metadata filter for bulk deletion

        Note:
            At least one of `ids` or `where` must be provided.

        Raises:
            RuntimeError: If deletion fails
        """
        try:
            col = self._get_collection(collection)
            kwargs = {}

            if ids:
                kwargs["ids"] = ids
            if where:
                kwargs["where"] = where

            if not kwargs:
                logger.warning("Delete called with no ids or where filter, ignoring")
                return

            col.delete(**kwargs)
            logger.info(f"Deleted items from collection '{collection}' (ids={bool(ids)}, where={bool(where)})")

        except Exception as e:
            logger.error(f"Failed to delete from collection '{collection}': {e}")
            raise RuntimeError(f"Delete operation failed for '{collection}': {e}") from e

    def get(
        self,
        collection: str,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        limit: Optional[int] = None
    ) -> dict:
        """Get documents from a collection by IDs or filter.

        Args:
            collection: Collection name
            ids: Optional list of specific document IDs to retrieve
            where: Optional metadata filter
            limit: Optional maximum number of results

        Returns:
            Dict with keys:
            - ids: list[str] - Document IDs
            - documents: list[str] - Document content
            - metadatas: list[dict] - Document metadata

        Raises:
            RuntimeError: If get operation fails
        """
        try:
            col = self._get_collection(collection)
            kwargs = {"include": ["documents", "metadatas"]}

            if ids:
                kwargs["ids"] = ids
            if where:
                kwargs["where"] = where
            if limit:
                kwargs["limit"] = limit

            results = col.get(**kwargs)
            logger.debug(f"Retrieved {len(results.get('ids', []))} items from '{collection}'")
            return results

        except Exception as e:
            logger.error(f"Failed to get from collection '{collection}': {e}")
            raise RuntimeError(f"Get operation failed for '{collection}': {e}") from e

    def count(self, collection: str) -> int:
        """Get the number of documents in a collection.

        Args:
            collection: Collection name

        Returns:
            Number of documents in the collection
        """
        try:
            col = self._get_collection(collection)
            return col.count()
        except Exception as e:
            logger.error(f"Failed to count collection '{collection}': {e}")
            raise RuntimeError(f"Count operation failed for '{collection}': {e}") from e

    def delete_by_file_path(self, collection: str, file_path: str) -> None:
        """Delete all chunks associated with a specific file path.

        Tries both canonical (`file_path`) and legacy (`source_file`) metadata
        so existing indexes continue to work after schema migrations.
        """
        try:
            self.delete(collection, where={"file_path": file_path})
            self.delete(collection, where={"source_file": file_path})
            logger.info("Deleted all chunks for file_path='%s' from '%s'", file_path, collection)
        except Exception as e:
            logger.error("Failed to delete by file_path '%s': %s", file_path, e)
            raise RuntimeError(f"Delete by file_path failed: {e}") from e

    def delete_by_source_file(self, collection: str, source_file: str) -> None:
        """Backward-compatible alias for delete_by_file_path()."""
        self.delete_by_file_path(collection, source_file)

    def get_file_hashes(self, collection: str) -> dict[str, str]:
        """Get a mapping of file_path -> content_hash for all files in collection.

        Used for incremental indexing to skip unchanged files.

        Args:
            collection: Collection name (documents or codebase)

        Returns:
            Dict mapping file_path to content_hash

        Example:
            >>> store.get_file_hashes("documents")
            {"notes/meeting.md": "abc123...", "notes/todo.md": "def456..."}
        """
        try:
            col = self._get_collection(collection)
            total = col.count()

            if total == 0:
                return {}

            # Get all metadatas (ChromaDB doesn't support pagination well,
            # so we retrieve all at once)
            result = col.get(include=["metadatas"])
            hashes = {}

            for meta in result.get("metadatas", []):
                if not meta:
                    continue
                file_path = meta.get("file_path") or meta.get("source_file")
                content_hash = meta.get("content_hash") or meta.get("source_file_hash")
                if file_path and content_hash:
                    hashes[str(file_path)] = str(content_hash)

            logger.debug(f"Retrieved {len(hashes)} file hashes from '{collection}'")
            return hashes

        except Exception as e:
            logger.error(f"Failed to get file hashes from '{collection}': {e}")
            raise RuntimeError(f"Get file hashes failed for '{collection}': {e}") from e

    def clear_collection(self, collection: str) -> None:
        """Clear all items from a collection.

        Uses in-place batched deletes instead of dropping/recreating the
        collection to avoid invalidating collection handles held by other
        long-lived processes (API/MCP workers).

        Args:
            collection: Collection name to clear
        """
        try:
            col = self._get_collection(collection)
            count_before = col.count()
            if count_before == 0:
                logger.info(f"Collection '{collection}' already empty")
                return

            deleted = 0
            batch_size = 5000

            while True:
                batch = col.get(limit=batch_size, include=["metadatas"])
                ids = batch.get("ids", [])
                if not ids:
                    break
                col.delete(ids=ids)
                deleted += len(ids)

            logger.info(
                f"Collection '{collection}' cleared in-place "
                f"(deleted {deleted} / {count_before} items)"
            )

        except Exception as e:
            logger.warning(
                f"In-place clear failed for '{collection}' ({e}); "
                "falling back to delete+recreate"
            )
            try:
                self.client.delete_collection(collection)
                self._collections.pop(collection, None)
                self._get_collection(collection)
                logger.info(f"Collection '{collection}' cleared via fallback recreate")
            except Exception as inner:
                logger.error(f"Failed to clear collection '{collection}': {inner}")
                raise RuntimeError(f"Clear collection failed for '{collection}': {inner}") from inner

    def list_collections(self) -> list[str]:
        """List all collection names in the database.

        Returns:
            List of collection names
        """
        try:
            return [c.name for c in self.client.list_collections()]
        except Exception as e:
            logger.error(f"Failed to list collections: {e}")
            raise RuntimeError(f"List collections failed: {e}") from e
