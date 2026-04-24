"""OB1 (Open Brain) bridge for bidirectional knowledge sync.

Handles:
- Pushing KF discoveries/semantic records to OB1 as thoughts
- Pulling OB1 thoughts into KF as indexed documents
- Tracking sync state to avoid duplicates
- Content fingerprinting for dedup across systems
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from knowledgeforge.core.models import Discovery, SemanticRecord, Chunk

logger = logging.getLogger(__name__)


class OB1Bridge:
    """Bidirectional bridge between KnowledgeForge and OB1."""

    def __init__(self, supabase_url: str, supabase_key: str, access_key: str = ""):
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self.access_key = access_key
        self._sync_state: dict[str, str] = {}  # content_fingerprint -> sync_status
        self._last_sync_time: Optional[str] = None
        self._sync_counts: dict[str, int] = {"push": 0, "pull": 0}

    def _supabase_headers(self) -> dict:
        """Return auth headers for Supabase REST API."""
        return {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _content_fingerprint(self, content: str) -> str:
        """Return SHA-256 hex digest of normalized content."""
        normalized = content.strip()
        # Normalize line endings and collapse multiple blank lines to one
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def fetch_ob1_thoughts(
        self,
        limit: int = 50,
        since: str = None,
        type_filter: str = None,
    ) -> list[dict]:
        """Fetch thoughts from OB1's Supabase database.

        Args:
            limit: Maximum number of thoughts to fetch.
            since: ISO datetime string; only fetch thoughts created after this.
            type_filter: Filter by metadata->>'type' value.

        Returns:
            List of thought dicts, empty list on error.
        """
        url = f"{self.supabase_url}/rest/v1/thoughts"
        params = {
            "select": "*",
            "order": "created_at.desc",
            "limit": limit,
        }
        if since:
            params["created_at"] = f"gte.{since}"
        if type_filter:
            params["metadata->>type"] = f"eq.{type_filter}"

        try:
            response = requests.get(
                url,
                headers=self._supabase_headers(),
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("Failed to fetch OB1 thoughts: %s", exc)
            return []

    def push_discovery_to_ob1(self, discovery: Discovery) -> Optional[str]:
        """Push a KnowledgeForge discovery to OB1 as a thought.

        Args:
            discovery: The Discovery object to push.

        Returns:
            The created OB1 thought ID string, or None on error.
        """
        url = f"{self.supabase_url}/rest/v1/thoughts"
        fingerprint = self._content_fingerprint(discovery.content)
        body = {
            "content": discovery.content,
            "content_fingerprint": fingerprint,
            "metadata": {
                "type": "reference",
                "source": "knowledgeforge",
                "kf_discovery_id": discovery.discovery_id,
                "kf_category": discovery.category,
                "kf_trust_level": discovery.trust_level,
                "topics": ["knowledgeforge-sync"],
            },
        }

        try:
            response = requests.post(
                url,
                headers=self._supabase_headers(),
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            thought_id = data[0]["id"] if isinstance(data, list) and data else None
            if thought_id:
                self._sync_state[fingerprint] = "synced"
                self._sync_counts["push"] += 1
                self._last_sync_time = datetime.now(timezone.utc).isoformat()
            return thought_id
        except Exception as exc:
            logger.warning(
                "Failed to push discovery %s to OB1: %s",
                discovery.discovery_id[:8],
                exc,
            )
            return None

    def push_semantic_record_to_ob1(self, record: SemanticRecord) -> Optional[str]:
        """Push a KnowledgeForge semantic record to OB1 as a thought.

        Args:
            record: The SemanticRecord object to push.

        Returns:
            The created OB1 thought ID string, or None on error.
        """
        url = f"{self.supabase_url}/rest/v1/thoughts"
        fingerprint = self._content_fingerprint(record.content)
        body = {
            "content": record.content,
            "content_fingerprint": fingerprint,
            "metadata": {
                "type": "reference",
                "source": "knowledgeforge",
                "kf_record_id": record.record_id,
                "kf_record_type": record.record_type,
                "kf_title": record.title,
                "kf_trust_level": record.trust_level,
                "topics": ["knowledgeforge-sync", record.record_type],
            },
        }

        try:
            response = requests.post(
                url,
                headers=self._supabase_headers(),
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            thought_id = data[0]["id"] if isinstance(data, list) and data else None
            if thought_id:
                self._sync_state[fingerprint] = "synced"
                self._sync_counts["push"] += 1
                self._last_sync_time = datetime.now(timezone.utc).isoformat()
            return thought_id
        except Exception as exc:
            logger.warning(
                "Failed to push semantic record %s to OB1: %s",
                record.record_id[:8],
                exc,
            )
            return None

    def export_discoveries_to_ob1(
        self,
        discoveries: list[Discovery],
        skip_unconfirmed: bool = True,
    ) -> dict:
        """Bulk export discoveries to OB1.

        Args:
            discoveries: List of Discovery objects to export.
            skip_unconfirmed: When True, skip discoveries that are not confirmed.

        Returns:
            Dict with keys: synced, skipped, failed, errors.
        """
        result = {"synced": 0, "skipped": 0, "failed": 0, "errors": []}

        for discovery in discoveries:
            if skip_unconfirmed and not discovery.confirmed:
                result["skipped"] += 1
                continue

            fingerprint = self._content_fingerprint(discovery.content)
            if fingerprint in self._sync_state:
                result["skipped"] += 1
                continue

            thought_id = self.push_discovery_to_ob1(discovery)
            if thought_id:
                result["synced"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(
                    f"Failed to push discovery {discovery.discovery_id[:8]}"
                )

        return result

    def export_semantic_records_to_ob1(self, records: list[SemanticRecord]) -> dict:
        """Bulk export semantic records to OB1.

        Args:
            records: List of SemanticRecord objects to export.

        Returns:
            Dict with keys: synced, skipped, failed, errors.
        """
        result = {"synced": 0, "skipped": 0, "failed": 0, "errors": []}

        for record in records:
            fingerprint = self._content_fingerprint(record.content)
            if fingerprint in self._sync_state:
                result["skipped"] += 1
                continue

            thought_id = self.push_semantic_record_to_ob1(record)
            if thought_id:
                result["synced"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(
                    f"Failed to push semantic record {record.record_id[:8]}"
                )

        return result

    def sync_status(self) -> dict:
        """Return a summary of the current sync state.

        Returns:
            Dict with total_synced, by_direction, and last_sync_time.
        """
        return {
            "total_synced": len(self._sync_state),
            "by_direction": {
                "push": self._sync_counts["push"],
                "pull": self._sync_counts["pull"],
            },
            "last_sync_time": self._last_sync_time,
        }
