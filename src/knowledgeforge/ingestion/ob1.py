"""OB1 (Open Brain) thoughts parser for KnowledgeForge.

Fetches thoughts from OB1's Supabase REST API and converts them into
KnowledgeForge Chunks for indexing in the documents collection.
"""

import logging
import requests
from datetime import datetime, timezone
from typing import Optional

from knowledgeforge.core.models import Chunk
from knowledgeforge.ingestion.fingerprint import content_fingerprint

logger = logging.getLogger(__name__)

# Trust level mapping from OB1 metadata type
_TYPE_TRUST_MAP = {
    "reference": "T2",
}
_DEFAULT_TRUST = "T3"


class OB1Parser:
    """Fetches OB1 thoughts from Supabase and converts them to KF Chunks."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        access_key: str = "",
    ):
        """Initialize the OB1 parser.

        Args:
            supabase_url: Supabase project URL (e.g. https://xyz.supabase.co).
            supabase_key: Supabase service role key.
            access_key: Optional OB1 access key (passed as header if set).
        """
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self.access_key = access_key

    def _headers(self) -> dict:
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
        }
        if self.access_key:
            headers["x-access-key"] = self.access_key
        return headers

    def fetch_thoughts(
        self,
        limit: int = 100,
        since: str = None,
        type_filter: str = None,
    ) -> list[dict]:
        """Fetch thoughts from OB1's Supabase REST API.

        Args:
            limit: Maximum number of thoughts to fetch.
            since: ISO 8601 timestamp — only fetch thoughts created after this.
            type_filter: Filter by metadata type field (e.g. "reference").

        Returns:
            List of thought dicts, or empty list on error.
        """
        url = f"{self.supabase_url}/rest/v1/thoughts"
        params = {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        if since:
            params["created_at"] = f"gte.{since}"
        if type_filter:
            params["metadata->>type"] = f"eq.{type_filter}"

        try:
            resp = requests.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("OB1Parser: failed to fetch thoughts: %s", exc)
            return []

    def parse_thoughts(self, thoughts: list[dict]) -> list[Chunk]:
        """Convert OB1 thought dicts into KnowledgeForge Chunks.

        Args:
            thoughts: List of thought dicts from the Supabase API.

        Returns:
            List of Chunk objects.
        """
        chunks = []
        for thought in thoughts:
            try:
                thought_id = thought.get("id", "")
                content = thought.get("content", "") or ""
                if not content.strip():
                    continue

                metadata = thought.get("metadata") or {}
                if isinstance(metadata, str):
                    import json
                    try:
                        metadata = json.loads(metadata)
                    except Exception:
                        metadata = {}

                ob1_type = metadata.get("type", "")
                trust_level = _TYPE_TRUST_MAP.get(ob1_type, _DEFAULT_TRUST)

                topics = metadata.get("topics", [])
                if isinstance(topics, list) and topics:
                    project_name = str(topics[0])
                else:
                    project_name = "ob1"

                created_at = thought.get("created_at") or datetime.now(timezone.utc).isoformat()

                chunk = Chunk(
                    chunk_id=f"ob1_thought_{thought_id}_0",
                    content=content,
                    file_path=f"ob1://thoughts/{thought_id}",
                    content_hash=content_fingerprint(content),
                    chunk_index=0,
                    chunk_type="thought",
                    trust_level=trust_level,
                    project_name=project_name,
                    created_at=created_at,
                )
                chunks.append(chunk)
            except Exception as exc:
                logger.warning("OB1Parser: error converting thought %s: %s", thought.get("id"), exc)

        logger.debug("OB1Parser: converted %s thoughts to chunks", len(chunks))
        return chunks

    def sync_thoughts(
        self,
        limit: int = 100,
        since: str = None,
        type_filter: str = None,
    ) -> list[Chunk]:
        """Fetch and parse OB1 thoughts in one call.

        Args:
            limit: Maximum number of thoughts to fetch.
            since: ISO 8601 timestamp — only fetch thoughts created after this.
            type_filter: Filter by metadata type field.

        Returns:
            List of Chunk objects.
        """
        thoughts = self.fetch_thoughts(limit=limit, since=since, type_filter=type_filter)
        return self.parse_thoughts(thoughts)

    def get_thought_by_id(self, thought_id: str) -> Optional[dict]:
        """Fetch a single thought by its ID.

        Args:
            thought_id: The thought's UUID.

        Returns:
            Thought dict, or None if not found or on error.
        """
        url = f"{self.supabase_url}/rest/v1/thoughts"
        params = {
            "select": "*",
            "id": f"eq.{thought_id}",
            "limit": "1",
        }
        try:
            resp = requests.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json()
            return results[0] if results else None
        except Exception as exc:
            logger.warning("OB1Parser: failed to fetch thought %s: %s", thought_id, exc)
            return None
