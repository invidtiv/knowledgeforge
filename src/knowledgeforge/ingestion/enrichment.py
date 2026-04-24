"""Optional LLM metadata enrichment for ingested content.

Uses an LLM (via litellm or direct API) to extract structured metadata
from content at ingestion time: type, topics, people, action items.
This is inspired by OB1's auto-extraction approach.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Extract structured metadata from the following text. Return JSON with: "
    "type (observation|task|idea|reference|person_note), "
    "topics (list of strings), "
    "people (list of names), "
    "action_items (list of strings), "
    "dates_mentioned (list of dates). "
    "Only include fields that are actually present."
)


class MetadataEnricher:
    """Optional LLM-based metadata enrichment for ingested content."""

    def __init__(
        self,
        api_key: str = None,
        model: str = "gpt-4o-mini",
        api_base: str = None,
    ):
        """Initialize enricher.

        Args:
            api_key: OpenAI-compatible API key. If None, enrichment is disabled.
            model: Model name to use for extraction.
            api_base: Optional API base URL (defaults to OpenAI).
        """
        self.api_key = api_key
        self.model = model
        self.api_base = api_base or "https://api.openai.com/v1"
        self._enabled = bool(api_key)

        if not self._enabled:
            logger.debug("MetadataEnricher: no api_key provided, enrichment disabled")

    @property
    def is_available(self) -> bool:
        """Whether enrichment is enabled (api_key was provided)."""
        return self._enabled

    def enrich(self, content: str) -> dict:
        """Extract structured metadata from content via LLM.

        Returns empty dict if not configured or on any error — never fails ingest.

        Args:
            content: Text content to enrich.

        Returns:
            Dict with extracted metadata fields, or empty dict.
        """
        if not self._enabled or not content.strip():
            return {}

        # Try litellm first if available
        try:
            import litellm
            return self._enrich_via_litellm(content, litellm)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("litellm enrichment failed: %s", exc)
            return {}

        # Fall back to raw requests
        return self._enrich_via_requests(content)

    def _enrich_via_litellm(self, content: str, litellm) -> dict:
        """Enrich using litellm library."""
        response = litellm.completion(
            model=self.model,
            api_key=self.api_key,
            api_base=self.api_base if self.api_base != "https://api.openai.com/v1" else None,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content[:4000]},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content or "{}"
        return json.loads(raw)

    def _enrich_via_requests(self, content: str) -> dict:
        """Enrich using raw HTTP requests to OpenAI-compatible API."""
        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": content[:4000]},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            }
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"] or "{}"
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Metadata enrichment failed: %s", exc)
            return {}

    def enrich_batch(self, contents: list[str]) -> list[dict]:
        """Enrich a batch of content strings sequentially.

        Args:
            contents: List of text strings to enrich.

        Returns:
            List of metadata dicts in the same order as input.
        """
        return [self.enrich(c) for c in contents]
