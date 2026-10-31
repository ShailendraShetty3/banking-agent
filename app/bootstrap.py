"""Wire concrete dependencies together. Shared by the API, the eval runner and the demo."""
from app.agent.llm import OllamaLLM
from app.agent.orchestrator import Agent
from app.config import Settings
from app.retrieval.embeddings import OllamaEmbedder
from app.retrieval.store import PolicyStore
from app.tools import AccountTools


def build_tools(s: Settings, **overrides) -> AccountTools:
    kwargs = dict(latency_s=s.tool_latency_s, timeout_rate=s.tool_timeout_rate,
                  request_log=s.service_request_log)
    kwargs.update(overrides)
    return AccountTools(s.accounts_path, s.corpus_dir / "POL-SVC-006_service_request_routing.md", **kwargs)


def build_agent(s: Settings, *, tools: AccountTools | None = None, llm: OllamaLLM | None = None) -> Agent:
    store = PolicyStore(s.database_url, OllamaEmbedder(s.ollama_url, s.embed_model, s.embed_dim))
    return Agent(
        settings=s,
        store=store,
        tools=tools or build_tools(s),
        llm=llm or OllamaLLM(s.ollama_url, s.llm_primary, s.llm_fallback, s.llm_timeout_s),
    )
