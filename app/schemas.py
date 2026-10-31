"""Request/response contracts for the API and the agent.

The customer-facing `message` is assembled ONLY from `notices` (fixed system text,
no facts) and `claims` (each of which must carry at least one citation). That is
how "a citation for every factual claim" is enforced structurally rather than by
asking the model nicely.
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ResponseType = Literal[
    "answer",                   # grounded answer to the question
    "decline_with_reason",      # mandatory policy decline, plain-language reason
    "raise_request",            # a service request was raised on the customer's behalf
    "refuse",                   # out of scope / not in corpus / not allowed
    "temporarily_unavailable",  # an upstream dependency failed; nothing was decided
]

CitationKind = Literal["policy", "account", "derived", "tool"]


class AskRequest(BaseModel):
    customer_id: str = Field(pattern=r"^[A-Z]+-\d+$", examples=["CUST-2000"])
    question: str = Field(min_length=1, max_length=2000)


class Citation(BaseModel):
    source_id: str  # e.g. "POL-FEE-007#1", "account:card_year_spend", "tool:raise_service_request"
    kind: CitationKind
    title: str | None = None
    version: str | None = None


class Claim(BaseModel):
    text: str
    citations: list[Citation] = Field(min_length=1)


class ServiceRequestRef(BaseModel):
    request_id: str
    queue: str
    sla_hours: int


class AgentResponse(BaseModel):
    trace_id: str
    customer_id: str
    response_type: ResponseType
    message: str
    claims: list[Claim]
    notices: list[str] = []
    reason_codes: list[str] = []
    service_request: ServiceRequestRef | None = None
    degraded: bool = False
    intent: str
    # For staff/audit, never shown to the customer: conflicts resolved, evidence filtered, etc.
    audit_notes: list[str] = []
    prompt_version: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "AgentResponse":
        if self.response_type in ("answer", "decline_with_reason") and not self.claims:
            raise ValueError(f"{self.response_type} must contain at least one cited claim")
        if self.response_type == "raise_request" and self.service_request is None:
            raise ValueError("raise_request must reference the service request that was raised")
        expected = " ".join([*self.notices, *(c.text for c in self.claims)])
        if self.message != expected:
            raise ValueError("message must be composed only of notices and cited claims")
        return self

    def cited_docs(self) -> set[str]:
        return {
            c.source_id.split("#")[0]
            for claim in self.claims
            for c in claim.citations
            if c.kind == "policy"
        }
