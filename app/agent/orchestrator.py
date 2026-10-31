"""Agent orchestration: one request in, one schema-valid AgentResponse out.

Flow
  1. classify intent (rules)                    -> early refusals need no tools
  2. get_account_state (retry on timeout)       -> None if the upstream stays down
  3. dispatch:
       limit_*            deterministic policy_rules, decline or refer
       dispute/block/...  raise_service_request to the POL-SVC-006 queue
       internal_score     refuse the internal part, offer approved reason codes
       policy_qa          hybrid retrieval -> evidence gate -> LLM -> claim validation
  4. anything ungrounded -> refuse and route to GENERAL (human review)
"""
import re
import time

from app.agent.guards import validate_claims
from app.agent.intents import Intent, classify
from app.agent.llm import LLMUnavailable, OllamaLLM
from app.agent.policy_rules import (
    LIMIT_RULE_DOCS,
    annual_fee_fact,
    assess_limit_increase,
    stale_documents,
)
from app.agent.prompts import PROMPT_ID, SYSTEM_PROMPT, build_user_prompt, output_schema
from app.config import Settings
from app.observability import Trace
from app.retrieval.store import PolicyStore
from app.schemas import AgentResponse, Citation, Claim, ResponseType, ServiceRequestRef
from app.tools import AccountTools, CustomerNotFound, InvalidCategory, UpstreamTimeout

# The only account fields the LLM ever sees. behavioural_score, vulnerable_flag and
# kyc_risk_band are internal: they drive rules in code but never enter a prompt.
LLM_ACCOUNT_FIELDS = ["card_product", "current_limit", "statement_balance", "minimum_due",
                      "due_date", "card_year_spend", "open_dispute"]
PRODUCT_HEADINGS = ("Classic Card", "Signature Card")  # product-specific sections of POL-PRD-010

ROUTED_INTENTS = {
    Intent.CARD_BLOCK: ("CARDS_URGENT", "card block or replacement"),
    Intent.DISPUTE: ("DISPUTES", "transaction dispute"),
    Intent.STATEMENT_REQUEST: ("SELFSERVE", "statement or certificate"),
    Intent.COMPLAINT: ("COMPLAINTS", "complaint"),
    Intent.STANDING_INSTRUCTION: ("PAYMENTS", "standing instruction change"),
}

ACCOUNT_DOWN = ("I can't reach your account details right now, so I can't check anything "
                "specific to your account.")


class ResponseBuilder:
    """Collects notices and claims; resolves every citation id against real sources."""

    def __init__(self, agent: "Agent", trace: Trace, customer_id: str, intent: Intent):
        self.agent, self.trace, self.customer_id, self.intent = agent, trace, customer_id, intent
        self.account: dict | None = None
        self.claims: list[Claim] = []
        self.notices: list[str] = []
        self.reason_codes: list[str] = []
        self.audit: list[str] = []
        self.service_request: ServiceRequestRef | None = None
        self.degraded = False
        self.model: str | None = None
        self.prompt_version: str | None = None
        self.extra_sources: dict[str, str] = {}  # derived facts shown to the LLM
        # A derived fact is computed by code from policy; citing it also cites that policy.
        self.derived_basis: dict[str, list[str]] = {}

    def cite(self, source_id: str) -> Citation:
        if source_id.startswith("account:"):
            field = source_id.split(":", 1)[1]
            if self.account is None or field not in self.account:
                raise ValueError(f"citation to unavailable account field {source_id}")
            return Citation(source_id=source_id, kind="account")
        if source_id.startswith("tool:"):
            return Citation(source_id=source_id, kind="tool")
        if source_id.startswith("derived:"):
            if source_id not in self.extra_sources:
                raise ValueError(f"unknown derived fact {source_id}")
            return Citation(source_id=source_id, kind="derived")
        chunk = self.agent.chunks.get(source_id)
        if chunk is None or chunk["status"] != "policy":
            raise ValueError(f"{source_id} is not a citeable policy chunk")
        return Citation(source_id=source_id, kind="policy", title=chunk["title"], version=chunk["version"])

    def claim(self, text: str, *source_ids: str) -> None:
        ids = list(dict.fromkeys(source_ids))
        for sid in source_ids:
            ids += [b for b in self.derived_basis.get(sid, []) if b not in ids]
        self.claims.append(Claim(text=text, citations=[self.cite(s) for s in ids]))

    def notice(self, text: str) -> None:
        self.notices.append(text)

    def build(self, response_type: ResponseType) -> AgentResponse:
        return AgentResponse(
            trace_id=self.trace.trace_id, customer_id=self.customer_id,
            response_type=response_type,
            message=" ".join([*self.notices, *(c.text for c in self.claims)]),
            claims=self.claims, notices=self.notices, reason_codes=self.reason_codes,
            service_request=self.service_request, degraded=self.degraded,
            intent=str(self.intent), audit_notes=self.audit,
            prompt_version=self.prompt_version, model=self.model,
        )


class Agent:
    def __init__(self, settings: Settings, store: PolicyStore, tools: AccountTools, llm: OllamaLLM):
        self.settings, self.store, self.tools, self.llm = settings, store, tools, llm
        self.chunks = store.chunk_index()
        versions = {c["doc_id"]: c["version"] for c in self.chunks.values()}
        self.stale_docs = stale_documents(versions)

    # ------------------------------------------------------------------ entry point
    def answer(self, customer_id: str, question: str) -> AgentResponse:
        trace = Trace(customer_id, question)
        intent = classify(question, customer_id)
        trace.set(intent=str(intent), prompt_id=PROMPT_ID)
        rb = ResponseBuilder(self, trace, customer_id, intent)
        try:
            response = self._dispatch(rb, question)
        except Exception as exc:  # last resort: the customer never sees a stack trace
            trace.error("agent", exc)
            rb = ResponseBuilder(self, trace, customer_id, intent)
            rb.notice("Sorry, something went wrong on our side and I couldn't complete that. "
                      "Nothing has been changed on your account. Please try again shortly.")
            response = rb.build("temporarily_unavailable")
        trace.finish(response, self.settings.trace_log)
        return response

    def _dispatch(self, rb: ResponseBuilder, question: str) -> AgentResponse:
        intent = rb.intent
        if intent is Intent.INSTRUCTION_OVERRIDE:
            return self._refuse_override(rb)
        if intent is Intent.OTHER_CUSTOMER:
            rb.notice("I can only discuss the account of the customer I'm speaking with.")
            return rb.build("refuse")

        try:
            rb.account = self._load_account(rb)
        except CustomerNotFound:
            rb.notice("I couldn't find an account matching your details, so I can't help with "
                      "account questions here. Please contact us through your registered channel.")
            return rb.build("refuse")
        rb.degraded = rb.account is None

        if intent is Intent.INTERNAL_SCORE:
            return self._refuse_internal_score(rb)
        if intent in ROUTED_INTENTS:
            return self._route(rb, question)
        if intent in (Intent.LIMIT_INCREASE, Intent.LIMIT_INFO):
            return self._limit(rb, question)
        return self._policy_qa(rb, question)

    # ------------------------------------------------------------------ tools
    def _load_account(self, rb: ResponseBuilder) -> dict | None:
        """Retry UpstreamTimeout with short backoff; return None if it never answers."""
        for attempt in range(1, self.settings.tool_max_attempts + 1):
            try:
                with rb.trace.span("tool.get_account_state", attempt=attempt):
                    return self.tools.get_account_state(rb.customer_id)
            except UpstreamTimeout as exc:
                rb.trace.error("tool.get_account_state", exc)
                if attempt < self.settings.tool_max_attempts:
                    time.sleep(0.2 * attempt)
        rb.audit.append(f"get_account_state timed out {self.settings.tool_max_attempts} times; "
                        "answered without account data.")
        return None

    def _raise_request(self, rb: ResponseBuilder, queue: str, summary: str) -> bool:
        try:
            with rb.trace.span("tool.raise_service_request", queue=queue):
                result = self.tools.raise_service_request(rb.customer_id, queue, summary)
        except (InvalidCategory, CustomerNotFound) as exc:
            rb.trace.error("tool.raise_service_request", exc)
            return False
        rb.service_request = ServiceRequestRef(**result)
        return True

    def _sla_text(self, queue: str) -> str:
        """The SLA wording for a queue, read from POL-SVC-006 rather than restated in code."""
        for cid in ("POL-SVC-006#1", "POL-SVC-006#2"):
            m = re.search(rf"->\s*{queue},\s*SLA\s*([^\n]+)", self.chunks[cid]["safe_text"])
            if m:
                return m.group(1).strip().rstrip(".")
        return "1 working day (human review)" if queue == "GENERAL" else "the published service level"

    def _confirm_request(self, rb: ResponseBuilder, what: str) -> None:
        sr = rb.service_request
        policy = "POL-SVC-006#2" if sr.queue == "GENERAL" else "POL-SVC-006#1"
        rb.claim(f"I've raised {what} request {sr.request_id} with our {sr.queue} team; "
                 f"the target time is {self._sla_text(sr.queue)}.",
                 policy, "tool:raise_service_request")

    # ------------------------------------------------------------------ refusals
    def _refuse_override(self, rb: ResponseBuilder) -> AgentResponse:
        rb.notice("I can't act on instructions taken from notes or documents. I only follow the "
                  "bank's published policy, and I can't approve or action a credit limit change myself.")
        rb.notice("If you'd like a limit increase, ask me directly and I'll check it against our credit policy.")
        rb.audit.append("Instruction-override attempt detected in the customer message; refused without tool calls.")
        return rb.build("refuse")

    def _refuse_internal_score(self, rb: ResponseBuilder) -> AgentResponse:
        rb.notice("I can't share a CIBIL or other credit bureau score: it isn't part of the "
                  "information available to me.")
        rb.claim("Internal score values are never disclosed.", "POL-CRD-001#4", "POL-GOV-009#2")
        rb.claim("If a credit decision goes against you, you're given the reason in plain language "
                 "using one of our approved reasons: recent missed payments; existing obligations "
                 "relative to income; limited credit history with us; an open dispute on the account; "
                 "a recent change to your limit; or information we hold needs to be refreshed.",
                 "POL-GOV-009#1", "POL-GOV-009#3")
        return rb.build("refuse")

    def _refuse_and_route(self, rb: ResponseBuilder, question: str, why: str) -> AgentResponse:
        rb.audit.append(f"Refused and routed to GENERAL: {why}")
        rb.trace.set(refusal_reason=why)
        rb.notice("I don't have approved information to answer that, so I won't guess.")
        if self._raise_request(rb, "GENERAL", f"Unanswered customer question ({why}): {question[:300]}"):
            rb.claim(f"I've passed your question to our team as request {rb.service_request.request_id}; "
                     "a person will review it within 1 working day.",
                     "POL-SVC-006#2", "tool:raise_service_request")
        return rb.build("refuse")

    # ------------------------------------------------------------------ routed requests
    def _route(self, rb: ResponseBuilder, question: str) -> AgentResponse:
        queue, what = ROUTED_INTENTS[rb.intent]
        if rb.degraded:
            rb.notice("I couldn't load your account details just now, but I've still logged your request.")
        if not self._raise_request(rb, queue, f"{what}: {question[:300]}"):
            rb.notice("I couldn't log your request just now. Nothing has been changed; please try "
                      "again shortly or contact us by phone.")
            return rb.build("temporarily_unavailable")
        if rb.intent is Intent.DISPUTE:
            # Stated unprompted: the commonest cause of repeat dispute calls (POL-SVC-005).
            rb.claim("You can dispute a transaction within 60 days of the statement it appears on.",
                     "POL-SVC-005#1")
            rb.claim("Provisional credit is applied within 5 working days for card-present fraud claims "
                     "and within 10 working days for all other dispute categories.", "POL-SVC-005#1")
        self._confirm_request(rb, what)
        return rb.build("raise_request")

    # ------------------------------------------------------------------ credit limit
    def _limit(self, rb: ResponseBuilder, question: str) -> AgentResponse:
        if rb.account is None:
            rb.notice(ACCOUNT_DOWN + " I need them to look at a credit limit request, so I haven't "
                      "assessed or submitted anything. Please try again in a few minutes.")
            return rb.build("temporarily_unavailable")
        stale = LIMIT_RULE_DOCS & self.stale_docs
        if stale:
            return self._refuse_and_route(rb, question, f"limit rules pinned to changed documents {sorted(stale)}")

        with rb.trace.span("rules.limit_increase") as span:
            a = assess_limit_increase(rb.account, self.settings.business_date())
            span.update(declined=a.declined, thin_file=a.thin_file is not None)
        rb.audit += a.audit

        if a.declined:
            for f in a.mandatory_declines:
                rb.claim(f.text, *f.sources)
                if f.reason_code:
                    rb.reason_codes.append(f.reason_code)
            return rb.build("decline_with_reason")

        finding = a.thin_file or a.referral
        rb.claim(finding.text, *finding.sources)
        if rb.intent is Intent.LIMIT_INFO:
            rb.notice("If you'd like a credit officer to review an increase, just ask and I'll raise the request.")
            return rb.build("answer")
        if not self._raise_request(rb, "CREDIT_OPS", f"Limit increase request: {question[:300]}"):
            rb.notice("I couldn't submit the request just now; please try again shortly.")
            return rb.build("temporarily_unavailable")
        self._confirm_request(rb, "a credit limit review")
        return rb.build("raise_request")

    # ------------------------------------------------------------------ grounded QA
    def _policy_qa(self, rb: ResponseBuilder, question: str) -> AgentResponse:
        with rb.trace.span("retrieval.hybrid") as span:
            result = self.store.search(question, self.settings.retrieval_top_k)
            span.update(
                vector_available=result.vector_available, salient=result.salient_lexemes,
                hits=[{"id": c.chunk_id, "rrf": round(c.rrf, 4),
                       "vsim": round(c.vector_sim, 3) if c.vector_sim is not None else None,
                       "lex_rank": c.lexical_rank, "salient": c.salient_hits} for c in result.chunks])
        if not result.vector_available:
            rb.audit.append("Embedding service unavailable; retrieval ran keyword-only.")

        product = (rb.account or {}).get("card_product")
        evidence = []
        for c in result.chunks:
            if c.status != "policy":
                rb.audit.append(f"Excluded {c.chunk_id}: document is not governed policy.")
                continue
            if product and c.heading in PRODUCT_HEADINGS and c.heading != f"{product} Card":
                rb.audit.append(f"Excluded {c.chunk_id}: describes a different card product.")
                continue
            relevant = bool(c.salient_hits) or (c.vector_sim or 0) >= self.settings.min_vector_similarity
            if relevant:
                evidence.append(c)
        rb.trace.set(evidence=[c.chunk_id for c in evidence])
        if not evidence:
            return self._refuse_and_route(rb, question, "retrieval found no relevant policy")

        sources: dict[str, str] = {}
        for c in evidence:
            header = f"{c.title} (version {c.version})" + (f" - {c.heading}" if c.heading else "")
            if c.overridden_by:
                header += f" [where this conflicts with {c.overridden_by}, {c.overridden_by} prevails]"
                rb.audit.append(f"{c.chunk_id} is subordinate to {c.overridden_by} on conflict.")
            sources[c.chunk_id] = f"{header}\n{c.safe_text}"
        if rb.account is not None:
            for f in LLM_ACCOUNT_FIELDS:
                sources[f"account:{f}"] = f"{f}: {rb.account.get(f)}"
            if re.search(r"\b(annual|fee|waive[dr]?)\b", question, re.I):
                fact = annual_fee_fact(rb.account)
                if fact:
                    rb.extra_sources["derived:annual_fee_waiver"] = fact
                    rb.derived_basis["derived:annual_fee_waiver"] = ["POL-FEE-007#1"]
                    sources["derived:annual_fee_waiver"] = fact

        forbidden = {str(rb.account["behavioural_score"])} if rb.account else set()
        rb.prompt_version = PROMPT_ID
        errors: list[str] = []
        valid = []
        for attempt in (1, 2):
            try:
                with rb.trace.span("llm.generate", attempt=attempt):
                    out = self.llm.generate_json(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(question, product or "unknown", sources, errors or None),
                        schema=output_schema(list(sources)), trace=rb.trace, prompt_version=PROMPT_ID)
            except LLMUnavailable as exc:
                rb.trace.error("llm", exc)
                rb.notice("I can't answer that right now because our assistant service is unavailable.")
                if self._raise_request(rb, "GENERAL", f"Unanswered (assistant unavailable): {question[:300]}"):
                    self._confirm_request(rb, "a follow-up")
                return rb.build("temporarily_unavailable")
            rb.model = out.model
            if not out.data.get("grounded", False):
                return self._refuse_and_route(rb, question, "model reported the sources do not answer it")
            valid, errors = validate_claims(out.data.get("claims", []), sources,
                                            product=product, forbidden_values=forbidden)
            with rb.trace.span("guards.validate_claims", attempt=attempt) as span:
                span.update(valid=len(valid), rejected=errors)
            if not errors:
                break
        if errors:
            rb.audit.append(f"Dropped {len(errors)} claim(s) that failed citation checks: {errors}")
        if not valid:
            return self._refuse_and_route(rb, question, "no claim survived citation checks")
        if rb.account is None:
            rb.notice(ACCOUNT_DOWN + " Here is our general policy instead.")
        for v in valid:
            rb.claim(v.text, *v.source_ids)
        return rb.build("answer")
