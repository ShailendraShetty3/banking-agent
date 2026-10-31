"""Fakes so the agent can be tested without Postgres or Ollama."""
import random
from datetime import date

import pytest

from app.agent.llm import LLMResult, LLMUnavailable
from app.agent.orchestrator import Agent
from app.config import Settings
from app.retrieval.chunking import load_corpus
from app.retrieval.store import RetrievalResult, RetrievedChunk
from app.tools import AccountTools

AS_OF = date(2026, 10, 31)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(as_of_date=AS_OF, tool_latency_s=0, tool_timeout_rate=0,
                    trace_log=tmp_path / "traces.jsonl", service_request_log=tmp_path / "sr.jsonl")


@pytest.fixture
def corpus(settings):
    return load_corpus(settings.corpus_dir)


class FakeStore:
    """chunk_index from the real corpus; search returns whatever the test scripts."""

    def __init__(self, docs):
        self.index = {}
        self.by_id = {}
        for d in docs:
            for c in d.chunks:
                row = dict(chunk_id=c.chunk_id, doc_id=d.doc_id, heading=c.heading, safe_text=c.safe_text,
                           injection_flagged=c.injection_flagged, title=d.title, version=d.version,
                           status=d.status, overridden_by=d.overridden_by)
                self.index[c.chunk_id] = row
        self.next_hits: list[tuple[str, float | None, list[str]]] = []

    def chunk_index(self):
        return self.index

    def search(self, question, top_k):
        chunks = []
        for i, (cid, vsim, salient) in enumerate(self.next_hits[:top_k]):
            r = self.index[cid]
            chunks.append(RetrievedChunk(
                chunk_id=cid, doc_id=r["doc_id"], title=r["title"], version=r["version"], status=r["status"],
                overridden_by=r["overridden_by"], heading=r["heading"], safe_text=r["safe_text"],
                vector_sim=vsim, lexical_rank=i + 1 if salient else None, rrf=1 / (60 + i + 1),
                salient_hits=salient))
        return RetrievalResult(chunks, [], [], True)


class FakeLLM:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    def generate_json(self, *, system, user, schema, trace, prompt_version):
        self.calls.append(user)
        if not self.outputs:
            raise LLMUnavailable("no scripted output")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(data=out, model="fake")


@pytest.fixture
def make_agent(settings, corpus):
    def _make(*, timeout_rate=0.0, llm_outputs=None, hits=None, seed=0):
        tools = AccountTools(settings.accounts_path, settings.corpus_dir / "POL-SVC-006_service_request_routing.md",
                             latency_s=0, timeout_rate=timeout_rate, rng=random.Random(seed))
        store = FakeStore(corpus)
        store.next_hits = hits or []
        agent = Agent(settings, store, tools, FakeLLM(llm_outputs))
        return agent
    return _make
