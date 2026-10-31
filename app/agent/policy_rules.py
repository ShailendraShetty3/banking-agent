"""Deterministic policy rules: code decides, the corpus justifies, the LLM never decides.

Every threshold below is transcribed from a specific document version. At startup the
agent compares PINNED_VERSIONS with the versions actually ingested; if a document has
changed, rules depending on it are treated as stale and the case is routed to a human
instead of being decided on out-of-date rules (see Agent._stale).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date

PINNED_VERSIONS = {
    "POL-CRD-001": "4.2",
    "POL-CRD-002": "2.1",
    "POL-CRD-003": "1.4",
    "POL-CON-004": "3.0",
    "POL-SVC-005": "2.6",
    "POL-SVC-006": "1.9",
    "POL-FEE-007": "5.1",
    "POL-KYC-008": "3.3",
    "POL-GOV-009": "2.0",
}

# POL-CRD-001 s.3 — 6 months. (POL-CRD-011 says 3 but defers to POL-CRD-001.)
LIMIT_INCREASE_COOLDOWN_MONTHS = 6
# POL-CRD-001 s.2
MAX_MULTIPLE_OF_LIMIT = 2.0
MAX_MULTIPLE_OF_MONTHLY_INCOME = 4.0
# POL-CRD-003 s.5
THIN_FILE_MIN_MONTHS = 6
THIN_FILE_CAP_MULTIPLE = 1.25
# POL-KYC-008
REKYC_YEARS_BY_RISK_BAND = {"high": 2, "medium": 8, "low": 10}
REKYC_GRACE_DAYS = 90
# POL-FEE-007
ANNUAL_FEE_WAIVER_SPEND = {"Classic": 150_000, "Signature": 400_000}

LIMIT_RULE_DOCS = {"POL-CRD-001", "POL-CRD-002", "POL-CRD-003", "POL-CON-004",
                   "POL-SVC-005", "POL-KYC-008", "POL-GOV-009", "POL-SVC-006"}


def stale_documents(ingested_versions: dict[str, str]) -> set[str]:
    return {doc for doc, v in PINNED_VERSIONS.items() if ingested_versions.get(doc) != v}


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def fmt_inr(amount: float) -> str:
    """Indian digit grouping, matching the corpus style: 150000 -> 1,50,000."""
    s = str(int(round(amount)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])


@dataclass
class Finding:
    text: str                 # customer-facing, plain language, no internal values
    sources: list[str]        # citation ids
    reason_code: str | None = None


@dataclass
class LimitAssessment:
    mandatory_declines: list[Finding] = field(default_factory=list)
    thin_file: Finding | None = None
    referral: Finding | None = None
    audit: list[str] = field(default_factory=list)

    @property
    def declined(self) -> bool:
        return bool(self.mandatory_declines)


def assess_limit_increase(acct: dict, as_of: date) -> LimitAssessment:
    """Apply POL-CRD-001 s.3 mandatory declines, then POL-CRD-003 thin-file treatment.

    The agent can only ever DECLINE (where policy removes discretion) or REFER.
    It never approves: debt-burden ratio and income (POL-CRD-001 s.2-3, POL-CRD-002)
    are not available from the account tool, so no increase can be safely granted.
    """
    a = LimitAssessment()

    if acct.get("vulnerable_flag"):
        # Deliberately neutral wording: the flag itself is not disclosed, and no sales content follows.
        a.mandatory_declines.append(Finding(
            "We're not able to offer a credit limit increase on your account at present.",
            ["POL-CON-004#2", "POL-CRD-001#3", "account:vulnerable_flag"]))
        a.audit.append("Declined under POL-CON-004: vulnerable flag set; all sales content suppressed.")

    if acct.get("open_dispute"):
        a.mandatory_declines.append(Finding(
            "There is an open dispute on your account, and a credit limit increase cannot be "
            "processed while a dispute is open.",
            ["POL-CRD-001#3", "POL-SVC-005#2", "account:open_dispute"], "AA04"))

    last = acct.get("last_limit_increase_date")
    if last:
        last_d = date.fromisoformat(last)
        next_ok = add_months(last_d, LIMIT_INCREASE_COOLDOWN_MONTHS)
        a.audit.append("POL-CRD-011 (3 months) conflicts with POL-CRD-001 s.3 (6 months); "
                       "applied POL-CRD-001, as POL-CRD-011 itself states POL-CRD-001 prevails.")
        if as_of < next_ok:
            a.mandatory_declines.append(Finding(
                f"Your limit was last increased on {last}, and a further increase cannot be "
                f"approved within 6 months of a previous increase.",
                ["POL-CRD-001#3", "account:last_limit_increase_date"], "AA05"))

    band = (acct.get("kyc_risk_band") or "").lower()
    kyc_done = acct.get("kyc_last_done")
    if band in REKYC_YEARS_BY_RISK_BAND and kyc_done:
        due = add_months(date.fromisoformat(kyc_done), 12 * REKYC_YEARS_BY_RISK_BAND[band])
        overdue_days = (as_of - due).days
        if overdue_days > REKYC_GRACE_DAYS:
            a.mandatory_declines.append(Finding(
                "Some of the information we hold about you needs to be refreshed (your periodic KYC "
                "update is overdue), so your limit cannot be increased until that is done. Your "
                "existing limit is unaffected and your account is not frozen for this reason.",
                ["POL-KYC-008#1", "account:kyc_last_done"], "AA06"))
            a.audit.append(f"Re-KYC overdue by {overdue_days} days (band={band}, due {due}).")

    if a.declined:
        return a

    months = acct.get("months_repayment_history", 0)
    limit = acct["current_limit"]
    if months < THIN_FILE_MIN_MONTHS:
        a.thin_file = Finding(
            f"Because your repayment history with us is {months} months, which is less than 6 months, "
            f"any increase is capped at 1.25 times your current limit of {fmt_inr(limit)} (so at most "
            f"{fmt_inr(limit * THIN_FILE_CAP_MULTIPLE)}), and it must be reviewed by a credit officer "
            f"rather than decided automatically.",
            ["POL-CRD-003#5", "account:months_repayment_history", "account:current_limit"])
        a.audit.append("Treated as thin-file on internal history alone; bureau trade lines are not "
                       "available from the account tool.")
    else:
        a.referral = Finding(
            f"A new limit can be at most the lower of 2 times your current limit of {fmt_inr(limit)} "
            f"and 4 times your assessed monthly net income, so a credit officer needs to verify "
            f"your income before any decision.",
            ["POL-CRD-001#2", "POL-CRD-002#1", "account:current_limit"])
    return a


def annual_fee_fact(acct: dict) -> str | None:
    product = acct.get("card_product")
    threshold = ANNUAL_FEE_WAIVER_SPEND.get(product)
    if threshold is None:
        return None
    spend = acct.get("card_year_spend", 0)
    met = "yes" if spend >= threshold else "no"
    return (f"Annual fee waiver check for this customer's {product} card: card-year spend "
            f"{fmt_inr(spend)}; waiver threshold for {product} {fmt_inr(threshold)}; threshold met: {met}.")
