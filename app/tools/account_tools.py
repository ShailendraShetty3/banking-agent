"""The two supplied tools, implemented to the fixed contracts in data/tools.md.

    get_account_state(customer_id) -> dict
    raise_service_request(customer_id, category, summary) -> dict

Tools may read the data files; the agent may not (it only ever calls these).
"""
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class CustomerNotFound(Exception):
    pass


class UpstreamTimeout(Exception):
    pass


class InvalidCategory(Exception):
    pass


HOURS_PER_WORKING_DAY = 24  # SLA clock assumption; see README limitations


def parse_routing_queues(policy_path: Path) -> dict[str, int]:
    """Read queue names and SLAs from POL-SVC-006 so the tool cannot drift from policy.

    Lines look like: "- Transaction dispute -> DISPUTES, SLA 2 working days"
    plus the catch-all: "routed to GENERAL and must be reviewed by a human within 1 working day".
    """
    text = policy_path.read_text(encoding="utf-8")
    queues: dict[str, int] = {}
    for queue, n, unit in re.findall(r"->\s*([A-Z_]+),\s*SLA\s*(\d+)\s*(hours?|working days?)", text):
        queues[queue] = int(n) if unit.startswith("hour") else int(n) * HOURS_PER_WORKING_DAY
    general = re.search(r"routed to ([A-Z_]+).*?within (\d+) working day", text, re.S)
    if general:
        queues[general.group(1)] = int(general.group(2)) * HOURS_PER_WORKING_DAY
    if not queues:
        raise RuntimeError(f"No queues could be parsed from {policy_path}")
    return queues


class AccountTools:
    def __init__(self, accounts_path: Path, routing_policy_path: Path, *,
                 latency_s: float = 0.4, timeout_rate: float = 1 / 12,
                 rng: random.Random | None = None, request_log: Path | None = None):
        self._accounts: dict[str, dict] = json.loads(accounts_path.read_text(encoding="utf-8"))
        self.queues = parse_routing_queues(routing_policy_path)
        self.latency_s = latency_s
        self.timeout_rate = timeout_rate
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._request_log = request_log
        self.raised: list[dict] = []

    def get_account_state(self, customer_id: str) -> dict:
        time.sleep(self.latency_s)
        with self._lock:
            fail = self._rng.random() < self.timeout_rate
        if fail:
            raise UpstreamTimeout(f"account service did not respond for {customer_id}")
        if customer_id not in self._accounts:
            raise CustomerNotFound(customer_id)
        return dict(self._accounts[customer_id])  # copy: callers must not mutate the source

    def raise_service_request(self, customer_id: str, category: str, summary: str) -> dict:
        if category not in self.queues:
            raise InvalidCategory(f"{category!r} is not a queue in POL-SVC-006: {sorted(self.queues)}")
        if customer_id not in self._accounts:
            raise CustomerNotFound(customer_id)
        result = {
            "request_id": f"SR-{uuid.uuid4().hex[:8].upper()}",
            "queue": category,
            "sla_hours": self.queues[category],
        }
        entry = {**result, "customer_id": customer_id, "summary": summary,
                 "created_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.raised.append(entry)
            if self._request_log is not None:
                self._request_log.parent.mkdir(parents=True, exist_ok=True)
                with self._request_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
        return result
