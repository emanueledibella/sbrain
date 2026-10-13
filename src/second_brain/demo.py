"""Fictional memories in a separate scope, reproducible without chat inference."""

from .engine import MemoryEngine


def run_demo(engine: MemoryEngine) -> dict:
    text = (
        "# Esempio inventato\n\n"
        "La destinazione della gita è Palermo.\n"
        "Marco non partecipa alla gita.\n"
        "Forse visiteremo un museo.\n"
    )
    remembered = engine.remember(
        text,
        title="DEMO: gita (informazioni inventate)",
        scope="demo",
        observed_at="2026-10-01T10:00:00+02:00",
        request_id="demo-source-v1",
        extraction={
            "entities": [{"ref": "trip", "name": "Gita dimostrativa", "kind": "event"}],
            "claims": [
                {
                    "subject_ref": "trip",
                    "predicate": "destination",
                    "value": "Palermo",
                    "context": "destination",
                    "statement": "La destinazione della gita è Palermo.",
                    "quote": "La destinazione della gita è Palermo.",
                },
                {
                    "subject_ref": "trip",
                    "predicate": "participation",
                    "value": "Marco: no",
                    "context": "Marco",
                    "statement": "Marco non partecipa alla gita.",
                    "quote": "Marco non partecipa alla gita.",
                },
                {
                    "subject_ref": "trip",
                    "predicate": "activity",
                    "value": "museo",
                    "epistemic": "proposed",
                    "statement": "Forse visiteremo un museo.",
                    "quote": "Forse visiteremo un museo.",
                },
            ],
        },
    )
    if not remembered["claim_ids"]:
        return remembered
    claims = engine.history(scope="demo")["claims"]
    first = next(c for c in claims if c["predicate"] == "destination" and c["value"] == "Palermo")
    participant = next(
        c for c in claims if c["predicate"] == "participation" and c["value"] == "Marco: no"
    )
    updated = engine.change(
        first["id"],
        kind="update",
        value="Catania",
        statement="Dal 4 ottobre la destinazione della gita è Catania.",
        effective_at="2026-10-04T10:00:00+02:00",
        reason="DEMO: cambiamento di programma",
        request_id="demo-update-v1",
    )
    before = engine.explain(updated["replacement_id"])["recorded_at"]
    corrected = engine.change(
        participant["id"],
        kind="correction",
        value="Marco: sì",
        statement="Marco partecipa alla gita.",
        reason="DEMO: correggo l'informazione errata iniziale",
        request_id="demo-correction-v1",
    )
    after = engine.explain(corrected["replacement_id"])["recorded_at"]
    results = {}
    for name, valid, known in (
        ("prima_del_cambio", "2026-10-02T12:00:00+02:00", after),
        ("dopo_il_cambio_prima_della_correzione", "2026-10-06T12:00:00+02:00", before),
        ("dopo_la_correzione", "2026-10-06T12:00:00+02:00", after),
    ):
        result = engine.recall(
            "gita Marco destinazione", scope="demo", valid_at=valid, known_at=known
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
