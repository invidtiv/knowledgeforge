"""Embedding wrapper for local and API-backed embedding models.

This module provides a lazy-loading embedding interface that transparently
handles model-specific prefixing (e.g., nomic models requiring task prefixes)
and can use OpenAI/OpenRouter embeddings without loading a local CPU model.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class Embedder:
    """Generate embeddings through the configured provider.

    OpenAI/OpenRouter providers avoid loading a sentence-transformers model.
    The local provider keeps the previous sentence-transformers behavior.
    """

    def __init__(
        self,
        model_name: str = "nomic-ai/nomic-embed-text-v1.5",
        device: str = "cpu",
        provider: str = "auto",
        openai_api_key: str = "",
        openrouter_api_key: str = "",
    ):
        """Initialize embedder with model configuration.

        Args:
            model_name: Provider model identifier
            device: Device to run on ("cpu", "cuda", or "auto" for auto-detection)
            provider: "auto", "openai", "openrouter", or "local"
            openai_api_key: Optional OpenAI API key; falls back to OPENAI_API_KEY
            openrouter_api_key: Optional OpenRouter API key; falls back to OPENROUTER_API_KEY
        """
        self.provider = provider.lower().strip() if provider else "auto"
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self.openrouter_api_key = openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.model_name = self._resolve_model_name(model_name)
        self.device = device
        self._model = None  # Lazy loaded
        self._dimension = self._known_api_dimension()

    def _resolved_provider(self) -> str:
        """Return the concrete provider selected from config and env."""
        if self.provider == "auto":
            if self.openai_api_key:
                return "openai"
            if self.openrouter_api_key:
                return "openrouter"
            return "local"
        return self.provider

    def _resolve_model_name(self, model_name: str) -> str:
        """Choose the default model for API providers when using auto mode."""
        api_default_models = {"text-embedding-3-small", "openai/text-embedding-3-small"}
        if (
            (self.provider == "openai" or (self.provider == "auto" and self.openai_api_key))
            and model_name == "nomic-ai/nomic-embed-text-v1.5"
        ):
            return "text-embedding-3-small"
        if (
            (self.provider == "openrouter" or (self.provider == "auto" and self.openrouter_api_key))
            and model_name == "nomic-ai/nomic-embed-text-v1.5"
        ):
            return "openai/text-embedding-3-small"
        if (
            self.provider == "auto"
            and not self.openai_api_key
            and not self.openrouter_api_key
            and model_name in api_default_models
        ):
            return "nomic-ai/nomic-embed-text-v1.5"
        return model_name

    def _known_api_dimension(self) -> int | None:
        """Return dimensions for common API embedding models without a network call."""
        model = self.model_name.removeprefix("openai/").lower()
        if model in {"text-embedding-3-small", "text-embedding-ada-002"}:
            return 1536
        if model == "text-embedding-3-large":
            return 3072
        return None

    def _load_model(self):
        """Load the model on first use."""
        if self._resolved_provider() != "local":
            return

        if self._model is not None:
            return

        # Auto-detect CUDA if device is "auto"
        if self.device == "auto":
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        logger.info(f"Loading embedding model: {self.model_name} on {self.device}")
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(
            self.model_name,
            trust_remote_code=True,
            device=self.device
        )
        logger.info("Embedding model loaded successfully")

    def _embed_openai(self, texts: list[str]) -> list[list[float]]:
        """Embed texts with the OpenAI embeddings API."""
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI embeddings")

        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model_name, "input": texts},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI embeddings failed: {response.status_code} {response.text}")

        data = response.json().get("data", [])
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        if len(vectors) != len(texts):
            raise RuntimeError("OpenAI embeddings response did not include one vector per input")
        return vectors

    def _embed_openrouter(self, texts: list[str]) -> list[list[float]]:
        """Embed texts with OpenRouter's OpenAI-compatible embeddings endpoint."""
        if not self.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter embeddings")

        response = httpx.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model_name, "input": texts},
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenRouter embeddings failed: {response.status_code} {response.text}")

        data = response.json().get("data", [])
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        if len(vectors) != len(texts):
            raise RuntimeError("OpenRouter embeddings response did not include one vector per input")
        return vectors

    def _embed_api_or_local(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of already-prefixed texts with the selected provider."""
        provider = self._resolved_provider()
        if provider == "openai":
            return self._embed_openai(texts)
        if provider == "openrouter":
            return self._embed_openrouter(texts)
        if provider != "local":
            raise RuntimeError(f"Unsupported embedding provider: {self.provider}")

        self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    def _is_nomic(self) -> bool:
        """Check if model is nomic and needs prefixing."""
        return self._resolved_provider() == "local" and "nomic" in self.model_name.lower()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document texts (adds 'search_document: ' prefix for nomic).

        Args:
            texts: List of document texts to embed

        Returns:
            List of embedding vectors (one per text)
        """
        if self._is_nomic():
            texts = [f"search_document: {t}" for t in texts]
        return self._embed_api_or_local(texts)

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query (adds 'search_query: ' prefix for nomic).

        Args:
            query: Query text to embed

        Returns:
            Single embedding vector
        """
        text = f"search_query: {query}" if self._is_nomic() else query
        return self._embed_api_or_local([text])[0]

    def embed_batch(
        self,
        texts: list[str],
        batch_size: int = 32,
        is_query: bool = False
    ) -> list[list[float]]:
        """Embed a batch of texts with configurable batch size.

        Args:
            texts: List of texts to embed
            batch_size: Number of texts per batch
            is_query: If True, use query prefix; if False, use document prefix

        Returns:
            List of embedding vectors (one per text)
        """
        if self._is_nomic():
            prefix = "search_query: " if is_query else "search_document: "
            texts = [f"{prefix}{t}" for t in texts]

        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            all_embeddings.extend(self._embed_api_or_local(batch))

        return all_embeddings

    @property
    def dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Dimension of embedding vectors
        """
        if self._dimension is not None:
            return self._dimension

        self._load_model()
        return self._model.get_sentence_embedding_dimension()
