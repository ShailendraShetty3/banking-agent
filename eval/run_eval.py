"""Run the evaluation set against the real agent (Postgres + Ollama) and gate on pass rate.

    python -m eval.run_eval                       # all cases
    python -m eval.run_eval --cases EV-001,EV-010
    python -m eval.run_eval --min-pass-rate 0.8   # exit 1 below the gate (CI)

Faults are injected per case via the `fault` column:
    account_timeout  get_account_state always raises UpstreamTimeout
    llm_down         primary and fallback models both point at models that do not exist
"""
import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agent.llm import OllamaLLM
from app.agent.orchestrator import Agent
from app.bootstrap import build_agent, build_tools
from app.config import ROOT, get_settings

CASES = ROOT / "eval" / "eval_cases.csv"
OUT = ROOT / "eval" / "results"
NON_POLICY_DOCS = {"POL-OPS-012"}


def _norm(s: str) -> str:
    """Lowercase and drop digit-group commas so '4,00,000', '400,000' and '400000' compare equal."""
    return re.sub(r"(?<=\d),(?=\d)", "", s.lower())


def _terms(cell: str) -> list[str]:
    return [t.strip() for t in (cell or "").split("|") if t.strip()]


def check(case: dict, resp, account_scores: dict) -> list[str]:
    failures = []
    if resp.response_type != case["expected_behaviour"]:
        failures.append(f"behaviour: expected {case['expected_behaviour']}, got {resp.response_type}")
    if case["expected_behaviour"] in ("answer", "decline_with_reason", "raise_request"):
        missing = {d.strip() for d in case["grounding_docs"].split(",") if d.strip()} - resp.cited_docs()
        if missing:
            failures.append(f"grounding: did not cite {sorted(missing)}")
    if case["expected_queue"]:
        got = resp.service_request.queue if resp.service_request else None
        if got != case["expected_queue"]:
            failures.append(f"queue: expected {case['expected_queue']}, got {got}")
    msg = _norm(resp.message)
    for t in _terms(case["must_contain"]):
        if _norm(t) not in msg:
            failures.append(f"missing {t!r}")
    for t in _terms(case["must_not_contain"]):
        if re.search(rf"(?<![\w.]){re.escape(_norm(t))}(?![\w])", msg):
            failures.append(f"contains forbidden {t!r}")
    # Global invariants, checked on every case.
    if resp.cited_docs() & NON_POLICY_DOCS:
        failures.append("cited a non-policy document")
    score = account_scores.get(case["customer_id"])
    if score is not None and re.search(rf"\b{score}\b", resp.message):
        failures.append("disclosed internal behavioural score")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", help="comma-separated case ids")
    ap.add_argument("--min-pass-rate", type=float, default=None)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    s = get_settings()
    cases = list(csv.DictReader(CASES.open(encoding="utf-8")))
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["case_id"] in wanted]

    base = build_agent(s, tools=build_tools(s, rng=random.Random(args.seed)))
    variants = {
        "": base,
        "account_timeout": Agent(s, base.store, build_tools(s, timeout_rate=1.0), base.llm),
        "llm_down": Agent(s, base.store, base.tools,
                          OllamaLLM(s.ollama_url, "unavailable-primary", "unavailable-fallback", 10)),
    }
    scores = {cid: str(a["behavioural_score"]) for cid, a in
              json.loads(s.accounts_path.read_text(encoding="utf-8")).items()}

    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        t0 = time.perf_counter()
        resp = variants[case["fault"]].answer(case["customer_id"], case["question"])
        latency = round((time.perf_counter() - t0) * 1000)
        failures = check(case, resp, scores)
        results.append({"case": case, "passed": not failures, "failures": failures,
                        "latency_ms": latency, "response": resp.model_dump()})
        print(f"{case['case_id']} {'PASS' if not failures else 'FAIL'} {resp.response_type:<24} "
              f"{latency:>7} ms  {'; '.join(failures)}", flush=True)
    base.store.close()

    traces = {}
    if s.trace_log.exists():
        for line in s.trace_log.read_text(encoding="utf-8").splitlines():
            t = json.loads(line)
            traces[t["trace_id"]] = t
    for r in results:
        t = traces.get(r["response"]["trace_id"], {})
        r["tokens"] = t.get("tokens", {"prompt": 0, "completion": 0})
        r["llm_calls"] = len(t.get("llm_calls", []))

    passed = sum(r["passed"] for r in results)
    rate = passed / len(results) if results else 0.0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (OUT / "results.json").write_text(json.dumps(
        {"run_at": stamp, "pass_rate": rate, "passed": passed, "total": len(results), "results": results},
        indent=2, default=str), encoding="utf-8")
    write_summary(results, rate, stamp, s)
    print(f"\npass rate {passed}/{len(results)} = {rate:.1%}")
    if args.min_pass_rate is not None and rate < args.min_pass_rate:
        print(f"EVAL GATE FAILED: {rate:.1%} < {args.min_pass_rate:.0%}")
        return 1
    return 0


def write_summary(results, rate, stamp, s) -> None:
    exp_yes = [r for r in results if r["case"]["expect_pass"] == "yes"]
    exp_no = [r for r in results if r["case"]["expect_pass"] == "no"]
    llm = [r for r in results if r["llm_calls"]]
    lines = [
        "# Evaluation results", "",
        f"Run at {stamp} · primary model `{s.llm_primary}`, fallback `{s.llm_fallback}` · "
        f"business date {s.business_date()}", "",
        f"**Pass rate: {sum(r['passed'] for r in results)}/{len(results)} = {rate:.1%}**", "",
        f"- Cases I expected to pass: {sum(r['passed'] for r in exp_yes)}/{len(exp_yes)} passed",
        f"- Cases I expected to fail: {sum(r['passed'] for r in exp_no)}/{len(exp_no)} passed",
        f"- Cases that used the LLM: {len(llm)}; mean latency "
        f"{(sum(r['latency_ms'] for r in llm) / len(llm) / 1000 if llm else 0):.1f} s; mean tokens "
        f"{(sum(r['tokens']['prompt'] for r in llm) / len(llm) if llm else 0):.0f} prompt / "
        f"{(sum(r['tokens']['completion'] for r in llm) / len(llm) if llm else 0):.0f} completion",
        "", "| Case | Expected | Got | Pass | Predicted | Latency ms | Tokens (p/c) | Failures |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c, resp = r["case"], r["response"]
        lines.append(
            f"| {c['case_id']} | {c['expected_behaviour']} | {resp['response_type']} | "
            f"{'✅' if r['passed'] else '❌'} | {c['expect_pass']} | {r['latency_ms']} | "
            f"{r['tokens']['prompt']}/{r['tokens']['completion']} | {'; '.join(r['failures']) or ''} |")
    lines += ["", "## Responses", ""]
    for r in results:
        resp = r["response"]
        lines.append(f"**{r['case']['case_id']}** ({r['case']['customer_id']}) _{r['case']['question']}_  ")
        lines.append(f"→ `{resp['response_type']}` {resp['message']}  ")
        cites = sorted({c['source_id'] for cl in resp['claims'] for c in cl['citations']})
        lines.append(f"citations: {', '.join(cites) or '—'}"
                     + (f" · request: {resp['service_request']['request_id']} → {resp['service_request']['queue']}"
                        if resp['service_request'] else "") + "\n")
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
