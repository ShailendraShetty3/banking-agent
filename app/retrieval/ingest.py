"""Ingest the policy corpus into Postgres. Idempotent: unchanged documents are skipped.

    python -m app.retrieval.ingest
"""
import logging

from app.config import ROOT, get_settings
from app.retrieval.chunking import load_corpus
from app.retrieval.embeddings import OllamaEmbedder
from app.retrieval.store import PolicyStore

log = logging.getLogger("ingest")


def _embedding_text(title: str, heading: str | None, text: str) -> str:
    # Title + heading give short chunks enough context to embed meaningfully.
    return "\n".join(p for p in (title, heading, text) if p)


def ingest(store: PolicyStore, corpus_dir) -> dict:
    docs = load_corpus(corpus_dir)
    existing = store.document_shas()
    stats = {"ingested": [], "unchanged": [], "removed": [], "non_policy": [], "injection_flagged": []}
    for doc in docs:
        if doc.status == "non_policy":
            stats["non_policy"].append(doc.doc_id)
        stats["injection_flagged"] += [c.chunk_id for c in doc.chunks if c.injection_flagged]
        if existing.get(doc.doc_id) == doc.content_sha:
            stats["unchanged"].append(doc.doc_id)
            continue
        vectors = store.embedder.embed_documents(
            [_embedding_text(doc.title, c.heading, c.safe_text) for c in doc.chunks])
        store.upsert_document(doc, vectors)
        stats["ingested"].append(doc.doc_id)
    stats["removed"] = store.delete_missing({d.doc_id for d in docs})
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = get_settings()
    store = PolicyStore(s.database_url, OllamaEmbedder(s.ollama_url, s.embed_model, s.embed_dim))
    try:
        store.apply_schema(ROOT / "scripts" / "schema.sql")
        for key, value in ingest(store, s.corpus_dir).items():
            log.info("%-18s %s", key, value)
    finally:
        store.close()


if __name__ == "__main__":
    main()
