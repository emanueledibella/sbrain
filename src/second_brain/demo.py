"""Fictional memories in a separate scope, reproducible without chat inference."""

from .engine import MemoryEngine


def run_demo(engine: MemoryEngine) -> dict:
    text = (
        "# Fictional example\n\n"
        "The trip destination is Palermo.\n"
        "Marco is not joining the trip.\n"
        "We might visit a museum.\n"
    )
    remembered = engine.remember(
        text,
        title="DEMO: trip (fictional information)",
        scope="demo",
        observed_at="2026-10-01T10:00:00+02:00",
        request_id="demo-source-en-v3",
        extraction={
            "entities": [{"ref": "trip", "name": "Demo trip", "kind": "event"}],
            "claims": [
                {
                    "subject_ref": "trip",
                    "predicate": "destination",
                    "value": "Palermo",
                    "context": "destination",
                    "statement": "The trip destination is Palermo.",
                    "quote": "The trip destination is Palermo.",
                },
                {
                    "subject_ref": "trip",
                    "predicate": "has_participant",
                    "value": "Marco: no",
                    "context": "Marco",
                    "statement": "Marco is not joining the trip.",
                    "quote": "Marco is not joining the trip.",
                    "polarity": "negative",
                },
                {
                    "subject_ref": "trip",
                    "predicate": "activity",
                    "value": "museum",
                    "epistemic": "proposed",
                    "statement": "We might visit a museum.",
                    "quote": "We might visit a museum.",
                },
            ],
        },
    )
    if not remembered["claim_ids"]:
        return remembered
    claims = [
        c for c in engine.history(scope="demo")["claims"] if c["id"] in remembered["claim_ids"]
    ]
    first = next(c for c in claims if c["predicate"] == "destination" and c["value"] == "Palermo")
    participant = next(
        c for c in claims if c["predicate"] == "has_participant" and c["value"] == "Marco: no"
    )
    updated = engine.change(
        first["id"],
        kind="update",
        value="Catania",
        statement="From October 4, the trip destination is Catania.",
        effective_at="2026-10-04T10:00:00+02:00",
        reason="DEMO: change of plans",
        request_id="demo-update-en-v3",
    )
    before = engine.explain(updated["replacement_id"])["recorded_at"]
    corrected = engine.change(
        participant["id"],
        kind="correction",
        value="Marco: yes",
        statement="Marco is joining the trip.",
        reason="DEMO: correcting the initial mistaken information",
        request_id="demo-correction-en-v3",
    )
    after = engine.explain(corrected["replacement_id"])["recorded_at"]
    results = {}
    for name, valid, known in (
        ("before_change", "2026-10-02T12:00:00+02:00", after),
        ("after_change_before_correction", "2026-10-06T12:00:00+02:00", before),
        ("after_correction", "2026-10-06T12:00:00+02:00", after),
    ):
        result = engine.recall(
            "trip Marco destination", scope="demo", valid_at=valid, known_at=known
        )
        results[name] = [
            {"value": c["value"], "source": c["evidence"][0]["source_id"]}
            for c in result["memories"]
        ]
    return {
        "scope": "demo",
        "fictional": True,
        "examples": results,
        "warnings": sorted(
            set(remembered["warnings"] + updated["warnings"] + corrected["warnings"])
        ),
    }
