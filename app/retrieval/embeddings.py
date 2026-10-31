"""nomic-embed-text via Ollama.

nomic-embed-text is trained with task prefixes: documents are embedded as
"search_document: ..." and queries as "search_query: ...". Skipping the prefixes
measurably hurts retrieval, so they are applied here and nowhere else.
"""
from functools import lru_cache

import httpx


class EmbeddingError(Exception):
    pass


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        # Query embeddings are cached: identical questions are common in service traffic.
        self.embed_query = lru_cache(maxsize=2048)(self._embed_query)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = httpx.post(f"{self.base_url}/api/embed",
                           json={"model": self.model, "input": texts}, timeout=self.timeout_s)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"{type(exc).__name__}: {exc}") from exc
        vectors = r.json()["embeddings"]
        if any(len(v) != self.dim for v in vectors):
            raise EmbeddingError(f"expected {self.dim}-dim vectors from {self.model}")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"search_document: {t}" for t in texts])

    def _embed_query(self, text: str) -> tuple[float, ...]:
        return tuple(self._embed([f"search_query: {text}"])[0])
