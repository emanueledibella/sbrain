from .models import Extraction


def validate_extraction(draft: Extraction, text: str):
    refs = [e.ref for e in draft.entities]
    if len(set(refs)) != len(refs):
        raise ValueError("Entity refs must be unique")
    for claim in draft.claims:
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
