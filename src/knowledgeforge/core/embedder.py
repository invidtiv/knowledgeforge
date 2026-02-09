"""Embedding wrapper for sentence-transformers models.

This module provides a lazy-loading embedding interface that transparently
handles model-specific prefixing (e.g., nomic models requiring task prefixes).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Embedder:
    """Wraps sentence-transformers for embedding generation.

    Handles nomic model prefixing transparently.
    Lazy loads model on first use to avoid startup delays.
    """

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5", device: str = "cpu"):
        """Initialize embedder with model configuration.

        Args:
            model_name: HuggingFace model identifier
            device: Device to run on ("cpu", "cuda", or "auto" for auto-detection)
        """
        self.model_name = model_name
        self.device = device
        self._model = None  # Lazy loaded

    def _load_model(self):
        """Load the model on first use."""
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

    def _is_nomic(self) -> bool:
        """Check if model is nomic and needs prefixing."""
        return "nomic" in self.model_name.lower()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document texts (adds 'search_document: ' prefix for nomic).

        Args:
            texts: List of document texts to embed

        Returns:
            List of embedding vectors (one per text)
        """
        self._load_model()
        if self._is_nomic():
            texts = [f"search_document: {t}" for t in texts]
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query (adds 'search_query: ' prefix for nomic).

        Args:
            query: Query text to embed

        Returns:
            Single embedding vector
        """
        self._load_model()
        text = f"search_query: {query}" if self._is_nomic() else query
        embedding = self._model.encode([text], show_progress_bar=False)
        return embedding[0].tolist()

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
        self._load_model()
        if self._is_nomic():
            prefix = "search_query: " if is_query else "search_document: "
            texts = [f"{prefix}{t}" for t in texts]

        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_embeddings = self._model.encode(batch, show_progress_bar=False)
            all_embeddings.extend(batch_embeddings.tolist())

        return all_embeddings

    @property
    def dimension(self) -> int:
        """Get the embedding dimension.

        Returns:
            Dimension of embedding vectors
        """
        self._load_model()
        return self._model.get_sentence_embedding_dimension()
