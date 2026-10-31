"""Central configuration. Every value can be overridden from the environment or .env."""
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Storage
    database_url: str = "postgresql://postgres:postgres@localhost:5432/banking_agent"
    data_dir: Path = ROOT / "data"

    # Models (all local, via Ollama — the synthetic data never leaves the machine)
    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    llm_primary: str = "llama3"
    llm_fallback: str = "phi3"
    llm_timeout_s: float = 240.0

    # Tools
    tool_latency_s: float = 0.4
    tool_timeout_rate: float = 1 / 12
    tool_max_attempts: int = 3  # 1 call + 2 retries on UpstreamTimeout

    # Retrieval
    retrieval_top_k: int = 5
    # A chunk is evidence if it matches a salient query keyword, OR (vector-only) its
    # cosine similarity is at least this. Out-of-corpus questions peaked at 0.618 on
    # this corpus, answerable ones at 0.63-0.76 with keyword hits; see README.
    min_vector_similarity: float = 0.68

    # Business date used for date rules (6-month bar, KYC overdue). Pin it for
    # reproducible evaluation; leave empty to use today's date.
    as_of_date: date | None = None

    trace_log: Path = ROOT / "logs" / "traces.jsonl"
    service_request_log: Path = ROOT / "logs" / "service_requests.jsonl"

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "policy_corpus"

    @property
    def accounts_path(self) -> Path:
        return self.data_dir / "accounts.json"

    def business_date(self) -> date:
        return self.as_of_date or date.today()


@lru_cache
def get_settings() -> Settings:
    return Settings()
