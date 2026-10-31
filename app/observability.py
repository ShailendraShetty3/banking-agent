"""Minimal tracing: one JSON line per request with spans, token usage, latency and errors.

Deliberately not OpenTelemetry: at this size a JSONL file is easier to read and to
explain. The span/attribute shape maps 1:1 onto OTel if we ever export it.
"""
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agent")
_write_lock = threading.Lock()


class Trace:
    def __init__(self, customer_id: str, question: str):
        self.trace_id = uuid.uuid4().hex[:16]
        self.started = time.perf_counter()
        self.record: dict = {
            "trace_id": self.trace_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "customer_id": customer_id,
            "question": question,
            "spans": [],
            "llm_calls": [],
            "errors": [],
            "attrs": {},
        }

    @contextmanager
    def span(self, name: str, **attrs):
        span = {"name": name, "attrs": attrs}
        t0 = time.perf_counter()
        try:
            yield span["attrs"]
            span["status"] = "ok"
        except Exception as exc:
            span["status"] = "error"
            span["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.record["spans"].append(span)

    def set(self, **attrs) -> None:
        self.record["attrs"].update(attrs)

    def error(self, where: str, exc: BaseException | str) -> None:
        msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        self.record["errors"].append({"where": where, "error": msg})
        log.warning("trace=%s %s: %s", self.trace_id, where, msg)

    def llm_call(self, *, model: str, prompt_version: str, prompt_tokens: int,
                 completion_tokens: int, latency_ms: float, ok: bool, error: str | None = None) -> None:
        self.record["llm_calls"].append({
            "model": model, "prompt_version": prompt_version,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "latency_ms": round(latency_ms, 1), "ok": ok, "error": error,
        })

    @property
    def tokens(self) -> dict:
        calls = self.record["llm_calls"]
        return {
            "prompt": sum(c["prompt_tokens"] for c in calls),
            "completion": sum(c["completion_tokens"] for c in calls),
        }

    def finish(self, response, path: Path | None) -> dict:
        self.record["latency_ms"] = round((time.perf_counter() - self.started) * 1000, 1)
        self.record["tokens"] = self.tokens
        self.record["response"] = {
            "response_type": response.response_type,
            "intent": response.intent,
            "degraded": response.degraded,
            "cited": sorted({c.source_id for cl in response.claims for c in cl.citations}),
            "service_request": response.service_request.model_dump() if response.service_request else None,
        }
        log.info("trace=%s intent=%s type=%s latency_ms=%s tokens=%s errors=%d",
                 self.trace_id, response.intent, response.response_type,
                 self.record["latency_ms"], self.record["tokens"], len(self.record["errors"]))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _write_lock, path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.record, default=str) + "\n")
        return self.record
