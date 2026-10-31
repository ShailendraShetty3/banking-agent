-- Policy store: documents (governance metadata) and chunks (retrieval units).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    doc_id         text PRIMARY KEY,
    title          text NOT NULL,
    version        text NOT NULL,
    effective      text,
    owner          text,
    status         text NOT NULL CHECK (status IN ('policy', 'non_policy')),
    overridden_by  text,             -- this document defers to that one on conflict
    content_sha    text NOT NULL,    -- skip re-embedding unchanged documents
    ingested_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_chunks (
    chunk_id           text PRIMARY KEY,
    doc_id             text NOT NULL REFERENCES policy_documents(doc_id) ON DELETE CASCADE,
    heading            text,
    text               text NOT NULL,   -- original, kept for audit
    safe_text          text NOT NULL,   -- instruction-like lines removed; the only text shown to the LLM
    injection_flagged  boolean NOT NULL DEFAULT false,
    embedding          vector(768) NOT NULL,
    tsv                tsvector GENERATED ALWAYS AS (
                           to_tsvector('english', coalesce(heading, '') || ' ' || safe_text)
                       ) STORED
);

CREATE INDEX IF NOT EXISTS policy_chunks_tsv_idx ON policy_chunks USING gin (tsv);
-- HNSW is overkill for 25 chunks (a sequential scan is faster) but is what we would
-- run at 100x, so the query plan we test is the one we would ship.
CREATE INDEX IF NOT EXISTS policy_chunks_embedding_idx
    ON policy_chunks USING hnsw (embedding vector_cosine_ops);
