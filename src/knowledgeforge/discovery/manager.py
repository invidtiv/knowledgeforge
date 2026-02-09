"""Discovery lifecycle management module.

Handles the complete discovery lifecycle:
- create: Store new discoveries with deduplication
- get: Retrieve by ID
- list: Query with filters
- confirm: Mark as verified
- reject: Delete unwanted discoveries
- update: Modify existing discoveries
- search_similar: Find related discoveries
"""

import logging
from typing import Optional, List
from datetime import datetime, timezone

from knowledgeforge.core.store import VectorStore
from knowledgeforge.core.embedder import Embedder
from knowledgeforge.core.models import Discovery
from knowledgeforge.config import KnowledgeForgeConfig

logger = logging.getLogger(__name__)


class DiscoveryManager:
    """Manages the discovery lifecycle: create, search, confirm, reject, promote."""

    def __init__(self, store: VectorStore, embedder: Embedder, config: KnowledgeForgeConfig):
        """Initialize discovery manager.

        Args:
            store: VectorStore instance for ChromaDB operations
            embedder: Embedder instance for generating embeddings
            config: KnowledgeForge configuration
        """
        self.store = store
        self.embedder = embedder
        self.config = config
        self.collection = config.discoveries_collection  # "discoveries"
        logger.debug(f"DiscoveryManager initialized with collection '{self.collection}'")

    def create(self, discovery: Discovery) -> Discovery:
        """Embed and store a new discovery.

        Before storing, check for similar existing discoveries (cosine similarity > 0.9).
        If a very similar discovery exists, append context to existing instead of duplicating.

        Steps:
        1. Embed the discovery content
        2. Search for similar existing discoveries
        3. If similarity > 0.9 (i.e., distance < 0.1 in cosine):
           - Append new context to existing discovery
           - Return the existing discovery (updated)
        4. Otherwise store as new discovery

        Args:
            discovery: Discovery object to store

        Returns:
            Discovery object (either newly created or existing one that was updated)
        """
        logger.info(f"Creating discovery: {discovery.discovery_id[:8]}...")

        # Step 1: Embed the discovery content
        embedding = self.embedder.embed_query(discovery.content)

        # Step 2: Search for similar existing discoveries (only if collection not empty)
        count = self.store.count(self.collection)
        if count > 0:
            try:
                results = self.store.query(
                    collection=self.collection,
                    query_embedding=embedding,
                    n_results=1  # Just need the most similar one
                )

                # Check if we found a very similar discovery (distance < 0.1 = similarity > 0.9)
                if results["ids"][0] and results["distances"][0]:
                    distance = results["distances"][0][0]
                    if distance < 0.1:
                        # Found a duplicate! Merge with existing
                        existing_id = results["ids"][0][0]
                        existing_content = results["documents"][0][0]
                        existing_metadata = results["metadatas"][0][0]

                        logger.info(f"Found similar discovery (distance={distance:.4f}), merging instead of duplicating")

                        # Reconstruct the existing discovery
                        existing_discovery = Discovery.from_metadata(existing_metadata, existing_content)

                        # Append new context to existing (if different)
                        if discovery.context and discovery.context not in existing_discovery.context:
                            existing_discovery.context = f"{existing_discovery.context}\n\n---\n\n{discovery.context}".strip()

                        # Update timestamp
                        existing_discovery.updated_at = datetime.now(timezone.utc).isoformat()

                        # Update in vector store
                        self.store.update(
                            collection=self.collection,
                            ids=[existing_id],
                            documents=[existing_discovery.content],
                            metadatas=[existing_discovery.to_metadata()]
                        )

                        logger.info(f"Merged context into existing discovery: {existing_discovery.discovery_id[:8]}...")
                        return existing_discovery

            except Exception as e:
                logger.warning(f"Failed to check for duplicates: {e}. Creating new discovery anyway.")

        # Step 3: No duplicate found, store as new discovery
        discovery_id = discovery.discovery_id

        # ChromaDB uses its own ID system (we'll use content hash for consistency)
        chroma_id = f"discovery_{discovery_id}"

        self.store.add(
            collection=self.collection,
            ids=[chroma_id],
            documents=[discovery.content],
            embeddings=[embedding],
            metadatas=[discovery.to_metadata()]
        )

        logger.info(f"Created new discovery: {discovery_id[:8]}... (category={discovery.category}, severity={discovery.severity})")
        return discovery

    def get(self, discovery_id: str) -> Optional[Discovery]:
        """Retrieve a specific discovery by its ID.

        Use VectorStore.get() with where={"discovery_id": discovery_id}

        Args:
            discovery_id: The discovery_id to retrieve

        Returns:
            Discovery object if found, None otherwise
        """
        logger.debug(f"Getting discovery: {discovery_id[:8]}...")

        try:
            results = self.store.get(
                collection=self.collection,
                where={"discovery_id": discovery_id},
                limit=1
            )

            if not results["ids"]:
                logger.warning(f"Discovery not found: {discovery_id}")
                return None

            # Reconstruct Discovery from metadata + content
            content = results["documents"][0]
            metadata = results["metadatas"][0]

            discovery = Discovery.from_metadata(metadata, content)
            logger.debug(f"Retrieved discovery: {discovery_id[:8]}...")
            return discovery

        except Exception as e:
            logger.error(f"Failed to get discovery {discovery_id}: {e}")
            raise RuntimeError(f"Failed to retrieve discovery: {e}") from e

    def list(self, project: str = None, unconfirmed_only: bool = False,
             category: str = None) -> List[Discovery]:
        """List discoveries with optional filters.

        Build ChromaDB where filter from parameters.
        Multiple filters use {"$and": [...]} syntax.
        Retrieve all matching, reconstruct Discovery objects.

        Args:
            project: Filter by project name (optional)
            unconfirmed_only: Only return unconfirmed discoveries (optional)
            category: Filter by category (optional)

        Returns:
            List of Discovery objects matching the filters
        """
        logger.debug(f"Listing discoveries (project={project}, unconfirmed_only={unconfirmed_only}, category={category})")

        # Build ChromaDB where filter
        filters = []

        if project:
            filters.append({"project": project})

        if unconfirmed_only:
            filters.append({"confirmed": False})

        if category:
            filters.append({"category": category})

        # Combine filters with $and if multiple
        where = None
        if len(filters) == 1:
            where = filters[0]
        elif len(filters) > 1:
            where = {"$and": filters}

        try:
            # Get all matching discoveries
            results = self.store.get(
                collection=self.collection,
                where=where
            )

            # Reconstruct Discovery objects
            discoveries = []
            for i, doc_id in enumerate(results["ids"]):
                content = results["documents"][i]
                metadata = results["metadatas"][i]
                discovery = Discovery.from_metadata(metadata, content)
                discoveries.append(discovery)

            logger.info(f"Listed {len(discoveries)} discoveries")
            return discoveries

        except Exception as e:
            logger.error(f"Failed to list discoveries: {e}")
            raise RuntimeError(f"Failed to list discoveries: {e}") from e

    def confirm(self, discovery_id: str) -> Discovery:
        """Mark a discovery as confirmed, set confirmed_at timestamp.

        1. Get the discovery
        2. Update confirmed=True, confirmed_at=now
        3. Update in vector store
        4. Return updated discovery

        Args:
            discovery_id: The discovery_id to confirm

        Returns:
            Updated Discovery object

        Raises:
            RuntimeError: If discovery not found or update fails
        """
        logger.info(f"Confirming discovery: {discovery_id[:8]}...")

        # Step 1: Get the discovery
        discovery = self.get(discovery_id)
        if not discovery:
            raise RuntimeError(f"Discovery not found: {discovery_id}")

        # Step 2: Update confirmed fields
        discovery.confirmed = True
        discovery.confirmed_at = datetime.now(timezone.utc).isoformat()
        discovery.updated_at = datetime.now(timezone.utc).isoformat()

        # Step 3: Update in vector store
        # Need to find the ChromaDB ID
        chroma_id = f"discovery_{discovery_id}"

        self.store.update(
            collection=self.collection,
            ids=[chroma_id],
            metadatas=[discovery.to_metadata()]
        )

        logger.info(f"Confirmed discovery: {discovery_id[:8]}...")
        return discovery

    def reject(self, discovery_id: str) -> bool:
        """Delete a rejected discovery.

        1. Find the discovery's ChromaDB ID
        2. Delete from vector store
        3. Return True if deleted

        Args:
            discovery_id: The discovery_id to reject/delete

        Returns:
            True if deleted successfully, False if not found
        """
        logger.info(f"Rejecting discovery: {discovery_id[:8]}...")

        # Verify discovery exists first
        discovery = self.get(discovery_id)
        if not discovery:
            logger.warning(f"Cannot reject discovery that doesn't exist: {discovery_id}")
            return False

        # Delete using the ChromaDB ID
        chroma_id = f"discovery_{discovery_id}"

        try:
            self.store.delete(
                collection=self.collection,
                ids=[chroma_id]
            )
            logger.info(f"Rejected (deleted) discovery: {discovery_id[:8]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to reject discovery {discovery_id}: {e}")
            raise RuntimeError(f"Failed to reject discovery: {e}") from e

    def update(self, discovery_id: str, updates: dict) -> Discovery:
        """Update discovery fields.

        1. Get existing discovery
        2. Apply updates to the discovery object
        3. Re-embed if content changed
        4. Update in vector store
        5. Return updated discovery

        Args:
            discovery_id: The discovery_id to update
            updates: Dict of field names -> new values

        Returns:
            Updated Discovery object

        Raises:
            RuntimeError: If discovery not found or update fails
        """
        logger.info(f"Updating discovery: {discovery_id[:8]}... with {len(updates)} changes")

        # Step 1: Get existing discovery
        discovery = self.get(discovery_id)
        if not discovery:
            raise RuntimeError(f"Discovery not found: {discovery_id}")

        # Step 2: Apply updates to discovery object
        content_changed = False
        for field_name, new_value in updates.items():
            if hasattr(discovery, field_name):
                if field_name == "content" and new_value != discovery.content:
                    content_changed = True
                setattr(discovery, field_name, new_value)
                logger.debug(f"Updated field '{field_name}' for discovery {discovery_id[:8]}...")
            else:
                logger.warning(f"Ignoring unknown field '{field_name}' in update")

        # Update timestamp
        discovery.updated_at = datetime.now(timezone.utc).isoformat()

        # Step 3: Re-embed if content changed
        chroma_id = f"discovery_{discovery_id}"

        if content_changed:
            logger.debug("Content changed, re-embedding discovery")
            embedding = self.embedder.embed_query(discovery.content)
            self.store.update(
                collection=self.collection,
                ids=[chroma_id],
                documents=[discovery.content],
                embeddings=[embedding],
                metadatas=[discovery.to_metadata()]
            )
        else:
            # Just update metadata
            self.store.update(
                collection=self.collection,
                ids=[chroma_id],
                metadatas=[discovery.to_metadata()]
            )

        logger.info(f"Updated discovery: {discovery_id[:8]}...")
        return discovery

    def search_similar(self, query: str, n_results: int = 5) -> List[Discovery]:
        """Find discoveries similar to a query.

        Used for deduplication and enriching search results.

        Args:
            query: Query text to search for
            n_results: Maximum number of results to return

        Returns:
            List of similar Discovery objects
        """
        logger.debug(f"Searching for similar discoveries: '{query[:50]}...'")

        # Check if collection is empty
        count = self.store.count(self.collection)
        if count == 0:
            logger.info("Discovery collection is empty, returning no results")
            return []

        # Embed the query
        query_embedding = self.embedder.embed_query(query)

        # Search for similar discoveries
        results = self.store.query(
            collection=self.collection,
            query_embedding=query_embedding,
            n_results=min(n_results, count)
        )

        # Reconstruct Discovery objects
        discoveries = []
        for i, doc_id in enumerate(results["ids"][0]):
            content = results["documents"][0][i]
            metadata = results["metadatas"][0][i]
            distance = results["distances"][0][i]

            discovery = Discovery.from_metadata(metadata, content)
            discoveries.append(discovery)
            logger.debug(f"Found similar discovery: {discovery.discovery_id[:8]}... (distance={distance:.4f})")

        logger.info(f"Found {len(discoveries)} similar discoveries")
        return discoveries
