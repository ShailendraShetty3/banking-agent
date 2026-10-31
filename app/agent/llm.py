"""Ollama chat client with structured output and a fallback model.

Structured output uses Ollama's `format` parameter with a JSON Schema, which
constrains decoding to schema-valid JSON. Schema-valid is not the same as correct:
citations and figures are still checked afterwards by guards.validate_claims.
"""
import json
import time
from dataclasses import dataclass

import httpx

from app.observability import Trace


class LLMUnavailable(Exception):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str


class OllamaLLM:
    def __init__(self, base_url: str, primary: str, fallback: str | None, timeout_s: float):
        self.base_url = base_url.rstrip("/")
        self.models = [m for m in (primary, fallback) if m]
        self.timeout_s = timeout_s

    def generate_json(self, *, system: str, user: str, schema: dict,
                      trace: Trace, prompt_version: str) -> LLMResult:
        """Try the primary model, then the fallback. Raise LLMUnavailable if both fail."""
        for model in self.models:
            t0 = time.perf_counter()
            try:
                r = httpx.post(f"{self.base_url}/api/chat", timeout=self.timeout_s, json={
                    "model": model,
                    "stream": False,
                    "format": schema,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 512},
                })
                r.raise_for_status()
                body = r.json()
                data = json.loads(body["message"]["content"])
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
                err = f"{type(exc).__name__}: {exc}"
                trace.llm_call(model=model, prompt_version=prompt_version, prompt_tokens=0,
                               completion_tokens=0, latency_ms=(time.perf_counter() - t0) * 1000,
                               ok=False, error=err)
                trace.error(f"llm:{model}", err)
                continue
            trace.llm_call(model=model, prompt_version=prompt_version,
                           prompt_tokens=body.get("prompt_eval_count", 0),
                           completion_tokens=body.get("eval_count", 0),
                           latency_ms=(time.perf_counter() - t0) * 1000, ok=True)
            return LLMResult(data=data, model=model)
        raise LLMUnavailable(f"all models failed: {self.models}")
