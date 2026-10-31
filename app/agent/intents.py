"""Rule-based intent routing.

Deliberately not an LLM classifier: the intents routed here either change account
state (raising requests), carry regulatory weight (limit decisions, internal scores)
or are attacks (instruction override). Those must be decided the same way every time
and be testable without a model. Anything that matches nothing falls through to
POLICY_QA, the grounded LLM path, which can still refuse.

Known weakness (see README): paraphrases that avoid these keywords fall through to
POLICY_QA instead of the deterministic path.
"""
import re
from enum import StrEnum


class Intent(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    OTHER_CUSTOMER = "other_customer"
    INTERNAL_SCORE = "internal_score"
    CARD_BLOCK = "card_block"
    DISPUTE = "dispute"
    LIMIT_INCREASE = "limit_increase"
    LIMIT_INFO = "limit_info"
    STATEMENT_REQUEST = "statement_request"
    COMPLAINT = "complaint"
    STANDING_INSTRUCTION = "standing_instruction"
    POLICY_QA = "policy_qa"


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


_OVERRIDE = _rx(
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules?|polic(y|ies)|prompts?|guidelines?)\b",
    r"\b(desktop|agent|internal|staff) notes?\b",
    r"\bdo (what|as) (it|they|the \w+) says?\b",
    r"\bsystem (note|prompt|message)\b",
    r"\byou are now\b|\bpretend (to be|you)\b|\b(developer|admin|god) mode\b",
)
_INTERNAL_SCORE = _rx(r"\b(cibil|credit score|bureau score|internal score|behaviou?ral score|my score|scorecard)\b")
_CARD_BLOCK = _rx(
    r"\b(lost|stolen|block|freeze|missing)\b.{0,25}\bcard\b",
    r"\bcard\b.{0,25}\b(lost|stolen|blocked|missing)\b",
    r"\breplace(ment)?\b.{0,20}\bcard\b",
)
_DISPUTE = _rx(
    r"\bnever (been|made|authori[sz]ed|bought)\b",
    r"\bdid ?n[o']?t (make|authori[sz]e|buy|do)\b",
    r"\bnot me\b|\bfraud|\bunauthori[sz]ed\b|\bsomeone (used|else)\b",
    r"\bdispute\b|\bdon'?t recogni[sz]e\b|\bcharged twice\b|\bdouble charged\b",
)
_LIMIT = re.compile(r"\blimit\b", re.I)
_INCREASE = _rx(r"\b(increase|raise|higher|more|bigger|enhance|upgrade|extend|lakh)\b")
_BARE_INCREASE = _rx(r"\b(small|smaller|bigger|larger|an?|my) increase\b")
_QUESTION_FORM = _rx(r"^\s*(why|what|how|when|which|is|does|explain)\b")
_STATEMENT = _rx(r"\b(statement|certificate)s?\b")
_REQUEST_VERB = _rx(r"\b(send|copy|need|want|get|request|download|email|issue)\b")
_FEE_TALK = _rx(r"\b(fee|charge|cost|price)s?\b")
_COMPLAINT = _rx(r"\bcomplain(t|ts|ing)?\b|\bescalate\b")
_STANDING = _rx(r"\bstanding instructions?\b|\bauto-?debit\b|\bautopay\b|\bmandate\b")
_CUSTOMER_ID = re.compile(r"\bCUST-\d+\b", re.I)


def classify(question: str, customer_id: str) -> Intent:
    q = question.strip()
    if _OVERRIDE.search(q):
        return Intent.INSTRUCTION_OVERRIDE
    if any(cid.upper() != customer_id.upper() for cid in _CUSTOMER_ID.findall(q)):
        return Intent.OTHER_CUSTOMER
    if _INTERNAL_SCORE.search(q):
        return Intent.INTERNAL_SCORE
    if _CARD_BLOCK.search(q):
        return Intent.CARD_BLOCK
    if _DISPUTE.search(q):
        return Intent.DISPUTE
    if (_LIMIT.search(q) and _INCREASE.search(q)) or _BARE_INCREASE.search(q):
        return Intent.LIMIT_INFO if _QUESTION_FORM.search(q) else Intent.LIMIT_INCREASE
    if _STATEMENT.search(q) and _REQUEST_VERB.search(q) and not _FEE_TALK.search(q):
        return Intent.STATEMENT_REQUEST
    if _COMPLAINT.search(q):
        return Intent.COMPLAINT
    if _STANDING.search(q):
        return Intent.STANDING_INSTRUCTION
    return Intent.POLICY_QA
