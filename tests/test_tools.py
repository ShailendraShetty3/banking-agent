import random

import pytest

from app.tools import AccountTools, CustomerNotFound, InvalidCategory, UpstreamTimeout


@pytest.fixture
def tools(settings):
    return AccountTools(settings.accounts_path, settings.corpus_dir / "POL-SVC-006_service_request_routing.md",
                        latency_s=0, timeout_rate=0)


def test_queues_parsed_from_policy(tools):
    assert tools.queues == {"CARDS_URGENT": 4, "DISPUTES": 48, "SELFSERVE": 24, "CREDIT_OPS": 72,
                            "PAYMENTS": 48, "COMPLAINTS": 120, "GENERAL": 24}


def test_get_account_state_returns_copy(tools):
    a = tools.get_account_state("CUST-2000")
    a["current_limit"] = 1
    assert tools.get_account_state("CUST-2000")["current_limit"] == 120000


def test_unknown_customer(tools):
    with pytest.raises(CustomerNotFound):
        tools.get_account_state("CUST-9999")


def test_timeout_rate_is_roughly_one_in_twelve(settings):
    tools = AccountTools(settings.accounts_path, settings.corpus_dir / "POL-SVC-006_service_request_routing.md",
                         latency_s=0, rng=random.Random(42))
    failures = 0
    for _ in range(6000):
        try:
            tools.get_account_state("CUST-2000")
        except UpstreamTimeout:
            failures += 1
    assert 0.06 < failures / 6000 < 0.11


def test_raise_service_request_contract(tools):
    out = tools.raise_service_request("CUST-2002", "DISPUTES", "unrecognised transaction")
    assert set(out) == {"request_id", "queue", "sla_hours"}
    assert out["request_id"].startswith("SR-") and out["queue"] == "DISPUTES" and out["sla_hours"] == 48


@pytest.mark.parametrize("category", ["Disputes", "FRAUD", "transaction dispute", ""])
def test_invalid_category_is_not_guessed(tools, category):
    with pytest.raises(InvalidCategory):
        tools.raise_service_request("CUST-2002", category, "x")
