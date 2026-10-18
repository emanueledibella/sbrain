"""Conflicts follow source-supported facts, identities, cardinality and both time axes."""

import json

import pytest
from rdflib import OWL

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import Extraction
from second_brain.ollama import Ollama
from second_brain.vocabulary import SB

from .conftest import extraction
from .test_preference_contracts import TARGET, TEXT, proposal
from .test_semantics import relation


@pytest.fixture
def automatic(engine, monkeypatch):
    """Exercise real extraction checks and adoption with deterministic model output."""
    engine.settings.embedding_dimensions = 3
    engine.models = Ollama(engine.settings)
    monkeypatch.setattr(engine.models, "status", lambda: {"model_digests": {}})

    def ingest(text=TEXT, *, predicate="prefers", target=TARGET, **fields):
        data = proposal(text, predicate=predicate, target=target, **fields)
        return submit(text, data)

    def submit(text, data, **options):
        def post(endpoint, body):
            if endpoint == "/api/embed":
                return {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]}
            return {"message": {"content": json.dumps(data)}}

        monkeypatch.setattr(engine.models, "_post", post)
        return engine.remember(text, scope="test", **options)

    ingest.submit = submit
    return ingest


def memories(engine, **options):
    return engine.recall("Nora Bell", scope="test", **options)["memories"]


@pytest.mark.parametrize("first", ["prefers", "preference", "preferred_by"])
@pytest.mark.parametrize("second", ["prefers", "preference", "preferred_by"])
def test_opposite_preference_forms_report_the_same_conflict(engine, automatic, first, second):
    a = automatic(predicate=first, context="favorite-drinks")
    text = f"Nora Bell non preferisce {TARGET}."
    b = automatic(text, predicate=second, polarity="negative", context="beverages")
    assert a["claim_ids"] and b["claim_ids"]
    assert b["conflicts"] == [{"claim_id": b["claim_ids"][0], "other_id": a["claim_ids"][0]}]
    rows = memories(engine)
    assert {r["predicate"] for r in rows} == {first, second}
    assert {r["context"] for r in rows} == {"default"}
    assert all(len(r["conflicts"]) == 1 for r in rows)
    # The comparison bridge never adds an OWL equivalence or changes original predicates.
    ontology = engine.vocabulary.ontology()
    assert (SB.preference, OWL.equivalentProperty, SB.prefers) not in ontology
    assert engine.read_source(a["source_id"])["text"] == TEXT


@pytest.mark.parametrize("predicate", ["prefers", "preference", "preferred_by"])
@pytest.mark.parametrize(
    "polarities", [("positive", "positive"), ("positive", "negative"), ("negative", "negative")]
)
def test_different_preference_targets_do_not_conflict(engine, automatic, predicate, polarities):
    for target, polarity in zip((TARGET, "il caffè amaro"), polarities, strict=True):
        text = f"Nora Bell {'non ' if polarity == 'negative' else ''}preferisce {target}."
        result = automatic(text, predicate=predicate, target=target, polarity=polarity)
        assert result["claim_ids"] and not result["conflicts"]
    assert len(memories(engine)) == 2
    assert all(not r["conflicts"] for r in memories(engine))


def test_context_only_duplicates_are_removed_with_audit_and_retry_safety(engine, automatic):
    data = proposal(context="drinks")
    data["claims"].append({**data["claims"][0], "context": "favorites"})
    original = json.dumps(data, sort_keys=True)
    result = automatic.submit(TEXT, data, request_id="duplicate-context")
    assert len(result["claim_ids"]) == 1
    assert json.dumps(data, sort_keys=True) == original
    claim = engine.explain(result["claim_ids"][0])
    assert claim["context"] == "default"
    repairs = claim["provenance"]["quality_checks"]["normalizations"]
    assert {r["from"] for r in repairs if r["field"] == "context"} == {"drinks", "favorites"}
    assert any(r["field"] == "duplicate_source_fact" for r in repairs)
    head = engine.status()["ledger_head"]
    again = automatic.submit(TEXT, data, request_id="duplicate-context")
    assert engine.extract_source(result["source_id"])["claim_ids"] == result["claim_ids"]
    assert again["claim_ids"] == result["claim_ids"]
    assert engine.status()["ledger_head"] == head
    assert engine.status()["sources"] == engine.status()["claims"] == 1


@pytest.mark.parametrize("change", ["polarity", "epistemic", "valid_from", "subject_ref"])
def test_invalid_or_incompatible_duplicates_cannot_hide_behind_deduplication(
    engine, automatic, change
):
    data = proposal()
    duplicate = {**data["claims"][0], "context": "another-label"}
    if change == "subject_ref":
        data["entities"].append({**data["entities"][0], "ref": "other"})
        duplicate[change] = "other"
    else:
        duplicate[change] = {
            "polarity": "negative",
            "epistemic": "proposed",
            "valid_from": "2028-01-01T10:00:00Z",
        }[change]
    data["claims"].append(duplicate)
    result = automatic.submit(TEXT, data)
    assert result["status"] == "stored_pending_extraction" and not result["claim_ids"]
    assert engine.status()["claims"] == 0
    assert engine.read_source(result["source_id"])["text"] == TEXT


def test_deduplication_preserves_distinct_source_occurrences_and_sources(engine, automatic):
    text = TEXT + " " + TEXT
    data = proposal(text)
    data["claims"][0].update(quote=TEXT, quote_start=0)
    data["claims"].append({**data["claims"][0], "quote_start": len(TEXT) + 1})
    result = automatic.submit(text, data)
    assert len(result["claim_ids"]) == 2
    evidence = [engine.explain(c)["evidence"][0] for c in result["claim_ids"]]
    assert {e["start"] for e in evidence} == {0, len(TEXT) + 1}
    other = automatic()
    assert len(other["claim_ids"]) == 1 and engine.status()["sources"] == 2
    assert not other["conflicts"]


def test_manually_provided_contexts_are_preserved(engine):
    first = engine.remember(TEXT, scope="test", extraction=proposal(context="at-home"))
    claim = engine.explain(first["claim_ids"][0])
    text = f"Nora Bell non preferisce {TARGET}."
    data = proposal(text, polarity="negative", context="at-work")
    data["entities"][0]["existing_id"] = claim["subject_id"]
    data["entities"][1]["existing_id"] = claim["object_id"]
    second = engine.remember(text, scope="test", extraction=data)
    assert not second["conflicts"]
    assert {r["context"] for r in memories(engine)} == {"at-home", "at-work"}


def test_legacy_model_context_and_its_correction_do_not_hide_conflicts(
    engine, automatic, monkeypatch, clock
):
    # Simulate a source accepted by the former automatic gate, retaining its ledger format.
    with monkeypatch.context() as patch:
        patch.setattr(
            engine.models,
            "extract",
            lambda *args: Extraction.model_validate(proposal(context="model-chosen-label")),
        )
        result = automatic()
    original = engine.explain(result["claim_ids"][0])
    assert original["context"] == "model-chosen-label"
    clock.set("2026-10-13T10:00:00Z")
    changed = engine.change(
        original["id"],
        kind="correction",
        value=TARGET,
        statement=TEXT,
        reason="Confirmed preference",
    )
    text = f"Nora Bell non preferisce {TARGET}."
    denied = automatic(text, polarity="negative")
    assert denied["conflicts"] == [
        {"claim_id": denied["claim_ids"][0], "other_id": changed["replacement_id"]}
    ]
    head = engine.status()["ledger_head"]
    assert all(r["conflicts"] for r in memories(engine))
    assert engine.status()["ledger_head"] == head
    assert engine.explain(original["id"])["context"] == "model-chosen-label"


def test_desire_literal_does_not_acquire_entity_preference_semantics(engine, automatic):
    automatic()
    text = f"Nora Bell non vuole {TARGET}."
    denied = automatic(text, predicate="preference", polarity="negative")
    assert denied["claim_ids"] and not denied["conflicts"]
    assert all(not r["conflicts"] for r in memories(engine))


def test_typed_homonymous_targets_stay_distinct_until_an_approved_temporal_merge(engine, clock):
    first = relation(
        engine,
        predicate="prefers",
        left="Nora Bell",
        right=TARGET,
        right_kind="entity",
        scope="test",
    )
    claim = engine.explain(first["claim_ids"][0])
    second = relation(
        engine,
        predicate="prefers",
        left="Nora Bell",
        right=TARGET,
        right_kind="entity",
        left_id=claim["subject_id"],
        polarity="negative",
        scope="test",
    )
    other = engine.explain(second["claim_ids"][0])
    assert claim["object_id"] != other["object_id"] and not second["conflicts"]
    clock.set("2026-10-13T10:00:00Z")
    engine.identity_change(
        other["object_id"],
        kind="merge",
        target_id=claim["object_id"],
        effective_at="2026-10-12T10:00:00Z",
        reason="Owner confirmed same tea",
    )
    assert all(r["conflicts"] for r in memories(engine))
    assert all(not r["conflicts"] for r in memories(engine, known_at="2026-10-12T11:00:00Z"))


def test_ambiguous_literal_target_is_not_matched_to_one_of_two_homonyms(engine):
    first = relation(
        engine,
        predicate="prefers",
        left="Nora Bell",
        right=TARGET,
        right_kind="entity",
        scope="test",
        context="default",
    )
    claim = engine.explain(first["claim_ids"][0])
    relation(
        engine,
        predicate="prefers",
        left="Nora Bell",
        right=TARGET,
        right_kind="entity",
        left_id=claim["subject_id"],
        scope="test",
        context="default",
    )
    text = f"Nora Bell non preferisce {TARGET}."
    data = proposal(text, predicate="preference", polarity="negative")
    data["entities"][0]["existing_id"] = claim["subject_id"]
    result = engine.remember(text, scope="test", extraction=data)
    assert not result["conflicts"]


@pytest.mark.parametrize(
    "polarities,conflict",
    [
        (("positive", "positive"), True),
        (("positive", "negative"), False),
        (("negative", "negative"), False),
    ],
)
def test_single_valued_relations_distinguish_different_values_from_denials(
    engine, polarities, conflict
):
    a = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        right="Bari",
        right_kind="place",
        scope="test",
        polarity=polarities[0],
    )
    original = engine.explain(a["claim_ids"][0])
    b = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        left_id=original["subject_id"],
        right="Como",
        right_kind="place",
        scope="test",
        polarity=polarities[1],
    )
    assert bool(b["conflicts"]) == conflict


def test_inverse_claims_and_target_identity_override_descriptive_value(engine):
    a = relation(engine, left="Nora Bell", right="Meeting West", scope="test")
    original = engine.explain(a["claim_ids"][0])
    b = relation(
        engine,
        predicate="has_participant",
        left="Meeting West",
        left_kind="event",
        left_id=original["object_id"],
        right="Nora Bell",
        right_kind="person",
        right_id=original["subject_id"],
        polarity="negative",
        scope="test",
    )
    assert b["conflicts"]


def test_nonoverlapping_claims_and_uncertainty_are_not_conflicts(engine):
    a = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        right="Bari",
        right_kind="place",
        scope="test",
        valid_from="2026-10-10T10:00:00Z",
        valid_to="2026-10-11T10:00:00Z",
    )
    original = engine.explain(a["claim_ids"][0])
    b = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        left_id=original["subject_id"],
        right="Como",
        right_kind="place",
        scope="test",
        valid_from="2026-10-11T10:00:00Z",
    )
    assert not b["conflicts"]
    c = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        left_id=original["subject_id"],
        right="Pisa",
        right_kind="place",
        scope="test",
        epistemic="proposed",
    )
    assert not c["conflicts"]
    assert all(
        not r["conflicts"]
        for r in engine.recall("Trip", scope="test", include_uncertain=True)["memories"]
    )


def test_cross_form_conflicts_are_not_used_as_positive_owl_premises(engine, automatic):
    automatic()
    text = f"Nora Bell non preferisce {TARGET}."
    automatic(text, predicate="preference", polarity="negative")
    result = engine.reason_memory("test")
    assert any("Conflicting claim excluded" in w for w in result["warnings"])
    assert not any(r["predicate"] == "preferred_by" for r in result["deductions"])


@pytest.mark.parametrize("rule", ["symmetric", "inverse_forward", "inverse_reverse"])
def test_extension_single_value_constraints_survive_relation_orientation(tmp_path, clock, rule):
    def prop(key, term, domain, target, **flags):
        return {
            "key": key,
            "term": term,
            "domain": domain,
            "range": target,
            "label": {"en": key},
            "description": "Exclusive relation for this test",
            "single_in_context": True,
            "symmetric": False,
            "transitive": False,
            "aliases": [],
            "inverse": None,
            **flags,
        }

    extension = tmp_path / "ontology.json"
    extension.write_text(
        json.dumps(
            {
                "properties": [
                    prop("exclusive_peer", "exclusivePeer", "agent", "agent", symmetric=True),
                    prop(
                        "exclusive_employer",
                        "exclusiveEmployer",
                        "agent",
                        "organization",
                        inverse="exclusive_employee",
                    ),
                    prop(
                        "exclusive_employee",
                        "exclusiveEmployee",
                        "organization",
                        "agent",
                        inverse="exclusive_employer",
                    ),
                ]
            }
        )
    )
    engine = MemoryEngine(
        Settings(
            _env_file=None, vault_path=tmp_path / "vault", vocabulary_extension_path=extension
        ),
        graph=None,
        models=None,
        clock=clock,
    )
    try:
        if rule == "symmetric":
            first = relation(
                engine,
                predicate="exclusive_peer",
                left="Nora Bell",
                right="Ada Finch",
                right_kind="person",
            )
            original = engine.explain(first["claim_ids"][0])
            other = relation(
                engine,
                predicate="exclusive_peer",
                left="Nora Bell",
                left_id=original["subject_id"],
                right="Lia Reed",
                right_kind="person",
            )
        else:
            first = relation(
                engine,
                predicate="exclusive_employer",
                left="Nora Bell",
                right="Cedar Labs",
                right_kind="organization",
            )
            original = engine.explain(first["claim_ids"][0])
            other = relation(
                engine,
                predicate="exclusive_employer",
                left="Nora Bell" if rule == "inverse_forward" else "Ada Finch",
                left_id=original["subject_id"] if rule == "inverse_forward" else None,
                right="Maple Labs" if rule == "inverse_forward" else "Cedar Labs",
                right_id=original["object_id"] if rule == "inverse_reverse" else None,
                right_kind="organization",
            )
        assert other["conflicts"]
    finally:
        engine.close()


def test_target_filter_does_not_hide_a_conflicting_literal_preference(engine, automatic):
    positive = automatic()
    target = engine.explain(positive["claim_ids"][0])["object_id"]
    negative = automatic(
        f"Nora Bell non preferisce {TARGET}.", predicate="preference", polarity="negative"
    )
    rows = memories(engine, entity_id=target)
    assert len(rows) == 1 and rows[0]["id"] == positive["claim_ids"][0]
    assert rows[0]["conflicts"] == negative["claim_ids"]


def test_target_filter_does_not_hide_a_competing_single_value(engine):
    first = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        right="Bari",
        right_kind="place",
        scope="test",
    )
    original = engine.explain(first["claim_ids"][0])
    other = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        left_id=original["subject_id"],
        right="Como",
        right_kind="place",
        scope="test",
    )
    rows = engine.recall("Trip", scope="test", entity_id=original["object_id"])["memories"]
    assert len(rows) == 1 and rows[0]["conflicts"] == other["claim_ids"]


def test_expired_legacy_targets_do_not_make_a_current_denial_ambiguous(engine):
    def participation(target, *, subject=None, polarity="positive", typed=True, **bounds):
        text = f"Nora Bell {'non ' if polarity == 'negative' else ''}partecipa alla {target}."
        entities = [
            {"ref": "person", "name": "Nora Bell", "kind": "person", "existing_id": subject}
        ]
        claim = {
            "subject_ref": "person",
            "predicate": "participation",
            "value": "yes",
            "polarity": polarity,
            "quote": text,
            "statement": text,
            **bounds,
        }
        if typed:
            entities.append({"ref": "target", "name": target, "kind": "event"})
            claim["object_ref"] = "target"
        return engine.remember(
            text, scope="test", extraction={"entities": entities, "claims": [claim]}
        )

    old = participation(
        "Riunione Ovest", valid_from="2026-10-10T10:00:00Z", valid_to="2026-10-11T10:00:00Z"
    )
    subject = engine.explain(old["claim_ids"][0])["subject_id"]
    current = participation("Riunione Est", subject=subject, valid_from="2026-10-11T10:00:00Z")
    denied = participation(
        "Riunione Est",
        subject=subject,
        polarity="negative",
        typed=False,
        valid_from="2026-10-11T10:00:00Z",
    )
    assert denied["conflicts"] == [
        {"claim_id": denied["claim_ids"][0], "other_id": current["claim_ids"][0]}
    ]
    assert all(r["conflicts"] for r in memories(engine))


def test_compatible_legacy_text_does_not_exclude_a_valid_typed_owl_premise(engine):
    typed = relation(
        engine,
        predicate="destination",
        left="Trip",
        left_kind="event",
        right="Bari",
        right_kind="place",
        scope="test",
        context="default",
    )
    original = engine.explain(typed["claim_ids"][0])
    text = "The trip goes to Bari."
    literal = engine.remember(
        text,
        scope="test",
        extraction=extraction(
            text, value="Bari", existing_id=original["subject_id"], context="default"
        ),
    )
    assert not literal["conflicts"]
    result = engine.reason_memory("test")
    assert not any("Conflicting claim excluded" in w for w in result["warnings"])
    assert any(
        d["predicate"] == str(SB.destinationOf) and d["premise_claim_ids"] == typed["claim_ids"]
        for d in result["deductions"]
    )
