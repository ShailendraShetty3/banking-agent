"""HTTP API.

    uvicorn app.api:app --port 8000

POST /v1/ask is a sync endpoint on purpose: FastAPI runs it in a worker thread pool,
and every dependency (psycopg pool, httpx, tool latency) is blocking, so this gives
real concurrency without async plumbing.
"""
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.agent.prompts import PROMPT_ID
from app.bootstrap import build_agent
from app.config import get_settings
from app.schemas import AgentResponse, AskRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent = build_agent(get_settings())
    yield
    app.state.agent.store.close()


app = FastAPI(title="Grounded card-servicing agent", version="0.1.0", lifespan=lifespan)


@app.post("/v1/ask", response_model=AgentResponse)
def ask(req: AskRequest) -> AgentResponse:
    # In production customer_id comes from the authenticated session, never the request body.
    return app.state.agent.answer(req.customer_id, req.question)


@app.get("/health")
def health() -> dict:
    agent = app.state.agent
    s = agent.settings
    try:
        with agent.store.pool.connection() as conn:
            conn.execute("SELECT 1")
        db = "ok"
    except Exception as exc:  # report, don't crash the health check
        db = f"error: {exc}"
    try:
        models = [m["name"] for m in httpx.get(f"{s.ollama_url}/api/tags", timeout=5).json()["models"]]
    except Exception as exc:
        models = [f"error: {exc}"]
    return {
        "database": db,
        "ollama_models": models,
        "primary_model": s.llm_primary,
        "fallback_model": s.llm_fallback,
        "prompt_id": PROMPT_ID,
        "policy_chunks": len(agent.chunks),
        "stale_policy_documents": sorted(agent.stale_docs),
    }
