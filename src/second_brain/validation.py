import re

from .models import Entity, Extraction, canonical_id, instant


def validate_entity_references(draft: Extraction, entities: list[Entity], scope: str):
    """Only explicitly offered, exact identities may be reused; never fuzzy-merge names."""
    allowed = {(e.id, e.name.casefold(), e.kind) for e in entities if e.scope == scope}
    used = {c.subject_ref for c in draft.claims} | {
        c.object_ref for c in draft.claims if c.object_ref
    }
    for entity in draft.entities:
        if entity.ref not in used or not entity.existing_id:
            continue
        if (canonical_id(entity.existing_id), entity.name.casefold(), entity.kind) not in allowed:
            raise ValueError(
                f"Ambiguous or cross-scope entity identity for ref {entity.ref!r}; "
                "reuse only an offered existing_id with its exact name and kind, "
                "otherwise set existing_id=null to create a new entity"
            )


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
