"""PostgreSQL + pgvector policy store with hybrid retrieval.

Hybrid search = keyword (Postgres full-text, ts_rank_cd) + vector (pgvector cosine),
fused with Reciprocal Rank Fusion: score = sum(1 / (60 + rank_i)). RRF uses ranks
rather than raw scores, so the two incomparable score scales never need calibrating.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.retrieval.chunking import PolicyDocument
from app.retrieval.embeddings import EmbeddingError, OllamaEmbedder

RRF_K = 60
CANDIDATES = 20  # per retriever, before fusion
# A query term is "salient" if it appears in at most this share of chunks. Matching
# only common terms ("card", "limit") is not evidence that the corpus answers a question.
SALIENT_MAX_DF = 0.2
# Stemmed customer-language words that are rare in policy text but say nothing about
# the topic of a question ("can you CHECK", "if I USE my card").
CONVERSATIONAL = {"check", "use", "would", "current", "year", "want", "need", "like",
                  "get", "make", "pleas", "help", "tell", "know"}


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    title: str
    version: str
    status: str
    overridden_by: str | None
    heading: str | None
    safe_text: str
    vector_sim: float | None
    lexical_rank: int | None
    rrf: float
    salient_hits: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]
    query_lexemes: list[str]
    salient_lexemes: list[str]
    vector_available: bool


def _vec_literal(v) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def _tsquery(question: str) -> str:
    """OR together the question's words; Postgres stems them and drops stopwords."""
    words = re.findall(r"[a-z0-9]+", question.lower())
    return " | ".join(dict.fromkeys(words)) or "zzznomatch"


_SEARCH_SQL = """
WITH vec AS (
    SELECT chunk_id,
           1 - (embedding <=> %(qvec)s::vector) AS vsim,
           row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS vrank
    FROM policy_chunks
    WHERE %(use_vec)s
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(n)s
), lex AS (
    SELECT chunk_id,
           row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS lrank
    FROM policy_chunks, to_tsquery('english', %(tsq)s) AS q
    WHERE tsv @@ q
    ORDER BY ts_rank_cd(tsv, q) DESC
    LIMIT %(n)s
)
SELECT c.chunk_id, c.doc_id, d.title, d.version, d.status, d.overridden_by,
       c.heading, c.safe_text, tsvector_to_array(c.tsv) AS lexemes,
       vec.vsim, lex.lrank,
       coalesce(1.0 / (%(k)s + vec.vrank), 0) + coalesce(1.0 / (%(k)s + lex.lrank), 0) AS rrf
FROM policy_chunks c
JOIN policy_documents d USING (doc_id)
LEFT JOIN vec USING (chunk_id)
LEFT JOIN lex USING (chunk_id)
WHERE vec.chunk_id IS NOT NULL OR lex.chunk_id IS NOT NULL
ORDER BY rrf DESC
LIMIT %(top)s
"""


class PolicyStore:
    def __init__(self, dsn: str, embedder: OllamaEmbedder):
        self.embedder = embedder
        self.pool = ConnectionPool(dsn, min_size=1, max_size=10, open=True,
                                   kwargs={"autocommit": True, "row_factory": dict_row})
        self._doc_freq: dict[str, int] | None = None
        self._n_chunks = 0

    def close(self) -> None:
        self.pool.close()

    # ---------- ingestion ----------
    def apply_schema(self, schema_path: Path) -> None:
        with self.pool.connection() as conn:
            conn.execute(schema_path.read_text(encoding="utf-8"))

    def document_shas(self) -> dict[str, str]:
        with self.pool.connection() as conn:
            return {r["doc_id"]: r["content_sha"]
                    for r in conn.execute("SELECT doc_id, content_sha FROM policy_documents")}

    def upsert_document(self, doc: PolicyDocument, vectors: list[list[float]]) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM policy_documents WHERE doc_id = %s", (doc.doc_id,))
            conn.execute(
                """INSERT INTO policy_documents
                   (doc_id, title, version, effective, owner, status, overridden_by, content_sha)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (doc.doc_id, doc.title, doc.version, doc.effective, doc.owner,
                 doc.status, doc.overridden_by, doc.content_sha))
            for chunk, vec in zip(doc.chunks, vectors, strict=True):
                conn.execute(
                    """INSERT INTO policy_chunks
                       (chunk_id, doc_id, heading, text, safe_text, injection_flagged, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::vector)""",
                    (chunk.chunk_id, doc.doc_id, chunk.heading, chunk.text, chunk.safe_text,
                     chunk.injection_flagged, _vec_literal(vec)))
        self._doc_freq = None

    def delete_missing(self, keep_doc_ids: set[str]) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("DELETE FROM policy_documents WHERE NOT (doc_id = ANY(%s)) RETURNING doc_id",
                                (list(keep_doc_ids),)).fetchall()
        self._doc_freq = None
        return [r["doc_id"] for r in rows]

    # ---------- lookup ----------
    def chunk_index(self) -> dict[str, dict]:
        """chunk_id -> metadata; used to validate and render citations."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT c.chunk_id, c.doc_id, c.heading, c.safe_text, c.injection_flagged,
                          d.title, d.version, d.status, d.overridden_by
                   FROM policy_chunks c JOIN policy_documents d USING (doc_id)""").fetchall()
        return {r["chunk_id"]: r for r in rows}

    def _load_doc_freq(self) -> None:
        with self.pool.connection() as conn:
            self._n_chunks = conn.execute("SELECT count(*) AS n FROM policy_chunks").fetchone()["n"]
            self._doc_freq = {r["word"]: r["ndoc"] for r in
                              conn.execute("SELECT word, ndoc FROM ts_stat('SELECT tsv FROM policy_chunks')")}

    def query_lexemes(self, question: str) -> list[str]:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT tsvector_to_array(to_tsvector('english', %s)) AS lx",
                               (question,)).fetchone()
        return list(row["lx"] or [])

    # ---------- hybrid search ----------
    def search(self, question: str, top_k: int) -> RetrievalResult:
        if self._doc_freq is None:
            self._load_doc_freq()
        lexemes = self.query_lexemes(question)
        salient = [lx for lx in lexemes if lx not in CONVERSATIONAL
                   and 0 < self._doc_freq.get(lx, 0) <= max(1, SALIENT_MAX_DF * self._n_chunks)]

        vector_available = True
        try:
            qvec = _vec_literal(self.embedder.embed_query(question))
        except EmbeddingError:
            # Degrade to keyword-only rather than fail the request.
            vector_available, qvec = False, _vec_literal([0.0] * self.embedder.dim)

        with self.pool.connection() as conn:
            rows = conn.execute(_SEARCH_SQL, {
                "qvec": qvec, "use_vec": vector_available, "tsq": _tsquery(question),
                "n": CANDIDATES, "k": RRF_K, "top": top_k,
            }).fetchall()

        chunks = [RetrievedChunk(
            chunk_id=r["chunk_id"], doc_id=r["doc_id"], title=r["title"], version=r["version"],
            status=r["status"], overridden_by=r["overridden_by"], heading=r["heading"],
            safe_text=r["safe_text"], vector_sim=r["vsim"], lexical_rank=r["lrank"], rrf=float(r["rrf"]),
            salient_hits=[lx for lx in salient if lx in set(r["lexemes"])],
        ) for r in rows]
        return RetrievalResult(chunks, lexemes, salient, vector_available)
