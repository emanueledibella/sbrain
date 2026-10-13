"""Small, reproducible semantic probes. This is an evaluation, not a proof of accuracy."""

import re

from .models import SourceRevision, instant, uid
from .store import digest

CASES = [
    {
        "id": "negation-en",
        "text": "Marco is not joining the trip.",
        "epistemic": "explicit",
        "value": r"\b(no|not|false|declined|won.t)\b",
    },
    {
        "id": "proposal-en",
        "text": "We might choose Palermo as the trip destination.",
        "epistemic": "proposed",
        "value": r"Palermo",
    },
    {
        "id": "decision-en",
        "text": "We decided today that the trip destination is Palermo.",
        "epistemic": "explicit",
        "value": r"Palermo",
    },
    {
        "id": "negation-it",
        "text": "Marco non parteciperà alla gita.",
        "epistemic": "explicit",
        "value": r"\b(no|non|false|negativo)\b",
    },
    {
        "id": "proposal-it",
        "text": "Forse sceglieremo Palermo come destinazione della gita.",
        "epistemic": "proposed",
        "value": r"Palermo",
    },
    {
        "id": "decision-it",
        "text": "Oggi abbiamo deciso che la destinazione della gita è Palermo.",
        "epistemic": "explicit",
        "value": r"Palermo",
    },
]


def assess(case, draft):
    if case["id"].startswith("negation"):
        matching = [claim for claim in draft.claims if claim.polarity == "negative"]
    else:
        matching = [claim for claim in draft.claims if re.search(case["value"], claim.value, re.I)]
    passed = bool(matching) and all(c.epistemic == case["epistemic"] for c in draft.claims)
    return {
        "case": case["id"],
        "passed": passed,
        "expected_epistemic": case["epistemic"],
        "claims": [c.model_dump(mode="json") for c in draft.claims],
    }


def evaluate_models(engine):
    if engine.models is None:
        raise ValueError("Evaluation requires a local extraction model")
    results = []
    # No evaluation data is adopted in the user's canonical ledger or graph.
    at = instant("2026-10-12T10:00:00Z")
    for case in CASES:
        source = SourceRevision(
            source_id=uid(),
            revision=1,
            title=case["id"],
            scope="evaluation",
            kind="note",
            sha256=digest(case),
            path="evaluation-only",
            observed_at=at,
            recorded_at=at,
        )
        try:
            draft = engine.models.extract(case["text"], source, [])
            results.append(assess(case, draft))
        except Exception as exc:
            results.append({"case": case["id"], "passed": False, "error": str(exc)[:300]})
    return {
        "model": engine.settings.chat_model,
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "results": results,
        "warnings": []
        if all(r["passed"] for r in results)
        else [
            "Semantic evaluation failed; inspect the reported claims before relying on this model"
        ],
    }
