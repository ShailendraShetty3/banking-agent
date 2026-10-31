from app.agent.guards import numbers_in, validate_claims

SOURCES = {
    "POL-FEE-007#1": "| Foreign currency markup | 3.5% |\n| Annual card fee, Signature | Waived on spend of 4,00,000 |",
    "POL-PRD-010#1": "Classic Card. Revolve rate 3.4% per month.",
    "account:card_year_spend": "card_year_spend: 162000",
}


def check(claims, product="Signature", forbidden=frozenset({"640"})):
    return validate_claims(claims, SOURCES, product=product, forbidden_values=set(forbidden))


def test_valid_claim_passes():
    valid, errors = check([{"text": "The foreign currency markup is 3.5%.", "citations": ["POL-FEE-007#1"]}])
    assert len(valid) == 1 and not errors


def test_indian_and_plain_number_formats_match():
    valid, errors = check([{"text": "Your card-year spend is 1,62,000, below 400000.",
                            "citations": ["account:card_year_spend", "POL-FEE-007#1"]}])
    assert valid and not errors


def test_uncited_claim_rejected():
    valid, errors = check([{"text": "The markup is 3.5%.", "citations": []}])
    assert not valid and "no citation" in errors[0]


def test_fabricated_citation_rejected():
    valid, errors = check([{"text": "Travel insurance is included.", "citations": ["POL-INS-001#1"]}])
    assert not valid and "not provided" in errors[0]


def test_right_document_wrong_figure_rejected():
    valid, errors = check([{"text": "The markup is 2.5%.", "citations": ["POL-FEE-007#1"]}])
    assert not valid and "2.5" in errors[0]


def test_internal_score_leak_rejected():
    valid, errors = check([{"text": "Your score of 640 is fine.", "citations": ["POL-FEE-007#1"]}])
    assert not valid and "internal" in errors[0]


def test_other_product_rejected():
    valid, errors = check([{"text": "The Classic revolve rate is 3.4% per month.", "citations": ["POL-PRD-010#1"]}],
                          product="Signature")
    assert not valid and "another product" in errors[0]


def test_document_ids_are_not_treated_as_figures():
    assert numbers_in("See POL-FEE-007#1 for the 3.5% markup") == {3.5}
