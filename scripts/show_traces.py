"""Print a readable summary of recent traces from logs/traces.jsonl.

    python scripts/show_traces.py            # last 10
    python scripts/show_traces.py -n 3 --full
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--full", action="store_true", help="print the raw JSON record")
    args = ap.parse_args()
    lines = get_settings().trace_log.read_text(encoding="utf-8").splitlines()[-args.n:]
    for line in lines:
        t = json.loads(line)
        if args.full:
            print(json.dumps(t, indent=2))
            continue
        r = t["response"]
        print(f"trace {t['trace_id']}  {t['customer_id']}  {t['latency_ms']:.0f} ms  "
              f"intent={t['attrs'].get('intent')}  -> {r['response_type']}"
              f"{'  [degraded]' if r['degraded'] else ''}")
        print(f"  Q: {t['question']}")
        for s in t["spans"]:
            extra = {k: v for k, v in s["attrs"].items() if k != "hits"}
            print(f"  span {s['name']:<28} {s['duration_ms']:>9.1f} ms  {s['status']}  {extra or ''}")
        for c in t["llm_calls"]:
            print(f"  llm  {c['model']:<10} {c['prompt_version']}  tokens {c['prompt_tokens']}+"
                  f"{c['completion_tokens']}  {c['latency_ms']:.0f} ms  {'ok' if c['ok'] else c['error']}")
        for e in t["errors"]:
            print(f"  error {e['where']}: {e['error']}")
        print(f"  cited: {', '.join(r['cited']) or '-'}"
              + (f"  request: {r['service_request']['request_id']} -> {r['service_request']['queue']}"
                 if r["service_request"] else ""))
        print()


if __name__ == "__main__":
    main()
