"""Post-generation checks on LLM claims. A claim survives only if:

  1. it cites at least one source,
  2. every cited id was actually provided to the model for this request,
  3. every number in the claim appears in the text of the sources it cites,
  4. it does not leak an internal value (e.g. the behavioural score),
  5. it does not mention a card product other than the customer's.

Check 3 is what catches a model that cites the right document but changes the figure.
"""
import re
from dataclasses import dataclass

_ID_TOKENS = re.compile(r"\b(POL-[A-Z]+-\d+(#\d+)?|CUST-\d+|SR-[A-Z0-9]+|AA\d{2})\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
PRODUCTS = ("Classic", "Signature")


@dataclass
class ValidClaim:
    text: str
    source_ids: list[str]


def numbers_in(text: str) -> set[float]:
    text = _ID_TOKENS.sub(" ", text)
    out = set()
    for tok in _NUMBER.findall(text):
        tok = tok.rstrip(",").replace(",", "")
        try:
            out.add(float(tok))
        except ValueError:
            pass
    return out


def validate_claims(raw_claims: list[dict], sources: dict[str, str], *,
                    product: str | None, forbidden_values: set[str]) -> tuple[list[ValidClaim], list[str]]:
    valid, errors = [], []
    for i, claim in enumerate(raw_claims, start=1):
        text = (claim.get("text") or "").strip()
        cites = [c.strip().strip("[]") for c in claim.get("citations") or [] if isinstance(c, str)]
        problems = []
        if not text:
            problems.append("empty text")
        if not cites:
            problems.append("no citation")
        unknown = [c for c in cites if c not in sources]
        if unknown:
            problems.append(f"cites ids that were not provided: {unknown}")
        known = [c for c in cites if c in sources]
        if known:
            supported = set().union(*(numbers_in(sources[c]) for c in known))
            missing = sorted(n for n in numbers_in(text) - supported)
            if missing:
                problems.append(f"figures {missing} do not appear in the cited sources {known}")
        leaked = [v for v in forbidden_values if v and re.search(rf"\b{re.escape(v)}\b", text)]
        if leaked:
            problems.append("discloses an internal value")
        if product:
            others = [p for p in PRODUCTS if p != product and re.search(rf"\b{p}\b", text, re.I)]
            if others:
                problems.append(f"mentions another product {others}")
        if problems:
            errors.append(f"claim {i} ({text[:60]!r}): " + "; ".join(problems))
        else:
            valid.append(ValidClaim(text, known))
    return valid, errors
