import json
from datetime import date

import pytest

from app.agent.policy_rules import (
    PINNED_VERSIONS,
    add_months,
    annual_fee_fact,
    assess_limit_increase,
    fmt_inr,
    stale_documents,
)
from tests.conftest import AS_OF


@pytest.fixture(scope="module")
def accounts(request):
    from app.config import Settings
    return json.loads(Settings().accounts_path.read_text())


def codes(a):
    return [f.reason_code for f in a.mandatory_declines]


def test_open_dispute_is_mandatory_decline(accounts):
    a = assess_limit_increase(accounts["CUST-2003"], AS_OF)
    assert a.declined and codes(a) == ["AA04"]


def test_vulnerable_declined_without_disclosing_flag(accounts):
    a = assess_limit_increase(accounts["CUST-2006"], AS_OF)
    assert a.declined
    assert "vulnerable" not in a.mandatory_declines[0].text.lower()


def test_six_month_bar_beats_three_month_document(accounts):
    a = assess_limit_increase(accounts["CUST-2001"], AS_OF)  # increased 2026-06-19, 4.4 months ago
    assert codes(a) == ["AA05"]
    assert any("POL-CRD-011" in note for note in a.audit)


def test_six_month_bar_lifts_after_six_months(accounts):
    a = assess_limit_increase(accounts["CUST-2001"], date(2026, 12, 19))
    assert not a.declined


def test_rekyc_overdue_high_risk(accounts):
    a = assess_limit_increase(accounts["CUST-2008"], AS_OF)
    assert codes(a) == ["AA06"]
    assert "not frozen" in a.mandatory_declines[0].text


def test_thin_file_cap_and_referral(accounts):
    a = assess_limit_increase(accounts["CUST-2005"], AS_OF)
    assert not a.declined and a.thin_file is not None
    assert "1,50,000" in a.thin_file.text  # 1.25 x 1,20,000


def test_low_score_is_referred_not_disclosed(accounts):
    a = assess_limit_increase(accounts["CUST-2004"], AS_OF)  # score 590, below 620
    assert not a.declined and a.referral is not None
    assert "590" not in a.referral.text


def test_fee_fact_uses_product_threshold(accounts):
    fact = annual_fee_fact(accounts["CUST-2000"])
    assert "1,62,000" in fact and "4,00,000" in fact and "threshold met: no" in fact


def test_stale_document_detection():
    assert stale_documents(dict(PINNED_VERSIONS)) == set()
    changed = dict(PINNED_VERSIONS, **{"POL-CRD-001": "4.3"})
    assert stale_documents(changed) == {"POL-CRD-001"}


@pytest.mark.parametrize("n,s", [(500, "500"), (150000, "1,50,000"), (400000, "4,00,000"), (12500000, "1,25,00,000")])
def test_fmt_inr(n, s):
    assert fmt_inr(n) == s


def test_add_months_clamps_day():
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
