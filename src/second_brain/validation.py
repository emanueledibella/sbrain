import re

from .models import Extraction, instant


def statement_signals(statement: str) -> tuple[bool, bool]:
    negative = bool(re.search(r"\b(not|never|non)\b", statement, re.I))
    tentative = bool(
        re.search(
            r"\b(might|could|maybe|perhaps|possibly|forse|potremmo|potrebbe)\b"
            r"|\bmay (choose|go|join|attend|be)\b",
            statement,
            re.I,
        )
    )
    return negative, tentative


def validate_extraction(draft: Extraction, text: str, observed_at=None, semantic_checks=False):
    refs = [e.ref for e in draft.entities]
    if len(set(refs)) != len(refs):
        raise ValueError("Entity refs must be unique")
    for claim in draft.claims:
        if semantic_checks:
            negative, tentative = statement_signals(claim.statement)
            quoted_negative, quoted_tentative = statement_signals(claim.quote)
            negative = negative or quoted_negative
            tentative = tentative or quoted_tentative
            if negative and claim.polarity != "negative":
                raise ValueError("Statement contains negation; preserve it with polarity=negative")
            if tentative and claim.epistemic == "explicit":
                raise ValueError("Statement is tentative; use epistemic=proposed or inferred")
        start_time = claim.valid_from or observed_at
        if claim.valid_to and start_time and instant(claim.valid_to) <= instant(start_time):
            raise ValueError("valid_to must be after valid_from")
        if claim.subject_ref not in refs or (claim.object_ref and claim.object_ref not in refs):
            raise ValueError("Claim references an unknown entity; populate the entities array")
        if claim.quote_start is None:
            start = text.find(claim.quote)
            if start < 0:
                raise ValueError("Evidence quote is absent from the source")
            if text.find(claim.quote, start + 1) >= 0:
                raise ValueError("Repeated quote needs quote_start to disambiguate the evidence")
        else:
            start = claim.quote_start
            if text[start : start + len(claim.quote)] != claim.quote:
                raise ValueError("Evidence offsets do not match the source")
