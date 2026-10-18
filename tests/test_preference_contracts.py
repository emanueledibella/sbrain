"""Literal and entity-valued preferences share evidence, never global aliases."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from second_brain.config import Settings
from second_brain.models import Entity, uid
from second_brain.ollama import Ollama
from second_brain.quality import repair_feedback, source_witnesses
from second_brain.state import State
from second_brain.vocabulary import vocabulary

from .test_quality import source

TEXT = "Nora Bell preferisce il tè nero speziato."
TARGET = "il tè nero speziato"


def proposal(text=TEXT, *, predicate="prefers", target=TARGET, **fields):
    entities = [{"ref": "person", "name": "Nora Bell", "kind": "person"}]
    claim = {
        "subject_ref": "person",
        "predicate": predicate,
        "value": target,
        "statement": text,
        "quote": text,
        "epistemic": "explicit",
        "polarity": "positive",
        **fields,
    }
    if predicate in {"prefers", "preferred_by"}:
        entities.append({"ref": "tea", "name": target, "kind": "entity"})
        claim["object_ref"] = "tea"
        if predicate == "preferred_by":
            claim.update(subject_ref="tea", object_ref="person", value="Nora Bell")
    return {"entities": entities, "claims": [claim]}


def extract(data, text=TEXT, *, offered=()):
    model = Ollama(Settings(_env_file=None))
    model._post = lambda *args: {"message": {"content": json.dumps(data)}}
    return model.extract(text, source(), list(offered))


@pytest.mark.parametrize("predicate", ["preference", "prefers", "preferred_by"])
@pytest.mark.parametrize(
    "text,polarity,epistemic",
    [
        (TEXT, "positive", "explicit"),
        ("Nora Bell non preferisce il tè nero speziato.", "negative", "explicit"),
        ("Forse Nora Bell preferisce il tè nero speziato.", "positive", "proposed"),
        ("Forse Nora Bell non preferisce il tè nero speziato.", "negative", "proposed"),
        ("Nora Bell prefers il tè nero speziato.", "positive", "explicit"),
        ("Nora Bell does not prefer il tè nero speziato.", "negative", "explicit"),
    ],
)
def test_preference_forms_preserve_source_roles_and_classification(
    predicate, text, polarity, epistemic
):
    result = extract(
        proposal(text, predicate=predicate, polarity=polarity, epistemic=epistemic), text
    )
    claim = result.claims[0]
    assert claim.predicate == predicate and claim.polarity == polarity
    assert claim.epistemic == epistemic and claim.quote == text
    refs = {e.ref: e for e in result.entities}
    assert bool(claim.object_ref) == (predicate != "preference")
    if predicate == "preferred_by":
        assert refs[claim.subject_ref].name == TARGET
        assert refs[claim.object_ref].name == "Nora Bell"
    else:
        assert refs[claim.subject_ref].name == "Nora Bell"
        assert claim.value == TARGET


def test_missing_object_is_materialized_from_the_complete_source_target():
    data = proposal()
    data["entities"].pop()
    data["claims"][0].pop("object_ref")
    result = extract(data)
    claim = result.claims[0]
    target = next(e for e in result.entities if e.ref == claim.object_ref)
    assert target.name == "tè nero speziato" and target.kind == "entity"


def test_missing_subject_reuses_exact_offered_identity_for_object_preference():
    data = proposal()
    data["entities"].pop(0)
    data["claims"][0]["subject_ref"] = "Nora Bell"
    known = Entity(id=uid(), name="Nora Bell", kind="agent", scope="test")
    result = extract(data, offered=[known])
    subject = next(e for e in result.entities if e.ref == result.claims[0].subject_ref)
    assert (subject.existing_id, subject.name, subject.kind) == (known.id, known.name, known.kind)
    with pytest.raises(ValueError, match="Ambiguous"):
        extract(data, offered=[known, known.model_copy(update={"id": uid()})])


@pytest.mark.parametrize(
    "change",
    [
        "short_subject",
        "short_target",
        "other_target",
        "wrong_subject_kind",
        "contradictory_value",
        "negative_value",
        "wrong_polarity",
        "wrong_certainty",
        "wrong_direction",
        "wrong_relation",
        "tiny_quote",
        "invented_date",
        "literal_object",
    ],
)
def test_object_preference_does_not_bypass_any_acceptance_gate(change):
    data = proposal()
    person, tea = data["entities"]
    claim = data["claims"][0]
    if change == "short_subject":
        person["name"] = "Nora"
    elif change == "short_target":
        tea["name"] = claim["value"] = "tè nero"
    elif change == "other_target":
        tea["name"] = claim["value"] = "caffè"
    elif change == "wrong_subject_kind":
        person["kind"] = "place"
    elif change == "contradictory_value":
        claim["value"] = "caffè"
    elif change == "negative_value":
        claim["value"] = "non vuole il tè nero speziato"
    elif change == "wrong_polarity":
        claim["polarity"] = "negative"
    elif change == "wrong_certainty":
        claim["epistemic"] = "proposed"
    elif change == "wrong_direction":
        claim.update(subject_ref="tea", object_ref="person", value="Nora Bell")
    elif change == "wrong_relation":
        claim["predicate"] = "works_on"
    elif change == "tiny_quote":
        claim["quote"] = TARGET
    elif change == "invented_date":
        claim["valid_from"] = "2027-04-01T10:00:00Z"
    elif change == "literal_object":
        claim["predicate"] = "preference"
    with pytest.raises(ValueError):
        extract(data)


@pytest.mark.parametrize("verb", ["vuole", "desidera", "wants"])
def test_desire_is_not_silently_equated_to_entity_preference(verb):
    text = f"Nora Bell {verb} {TARGET}."
    assert extract(proposal(text, predicate="preference"), text).claims
    with pytest.raises(ValueError, match="source contract"):
        extract(proposal(text), text)
    assert (
        "alternative_object_form" not in repair_feedback(text, vocabulary())["source_contracts"][0]
    )


def test_visit_and_another_subjects_preference_do_not_license_prefers():
    text = "Forse visiteremo il Parco Nord durante la Gita Estate."
    data = proposal(text, target="il Parco Nord", epistemic="proposed")
    data["entities"][0]["name"] = "Gita Estate"
    with pytest.raises(ValueError, match="source contract"):
        extract(data, text)
    data = proposal(
        "Nora Bell partecipa alla Gita Estate. Ada Finch preferisce il tè nero speziato."
    )
    with pytest.raises(ValueError, match="source contract"):
        extract(data, data["claims"][0]["quote"])


@pytest.mark.parametrize("second", ["preference", "preferred_by"])
def test_one_fact_cannot_be_adopted_as_competing_relation_representations(second):
    data = proposal()
    alternate = proposal(predicate=second)
    data["claims"].extend(alternate["claims"])
    with pytest.raises(ValueError, match="multiple relation representations"):
        extract(data)


def test_repair_feedback_offers_two_forms_for_one_fact_without_global_aliases():
    hints = repair_feedback(TEXT, vocabulary())
    assert hints["total_contracts"] == 1 and len(source_witnesses(TEXT, vocabulary())) == 1
    literal = hints["source_contracts"][0]
    assert literal["predicate"] == "preference" and literal["object_kind_minimum"] is None
    assert literal["alternative_object_form"] == {
        "predicate": "prefers",
        "subject_kind_minimum": "agent",
        "object_kind_minimum": "entity",
        "choose_one_representation": True,
    }
    assert vocabulary().property("preference")["range"] is None
    assert vocabulary().property("prefers")["range"] == "entity"
    assert vocabulary().property_aliases["preference"] != vocabulary().property_aliases["prefers"]


def test_repair_uses_original_source_and_checks_without_echoing_rejected_json(monkeypatch):
    model = Ollama(Settings(_env_file=None))
    rejected = proposal(target="caffè inventato")
    outputs = [json.dumps(rejected), json.dumps(proposal())]
    requests = []

    def post(endpoint, body):
        requests.append(json.loads(json.dumps(body)))
        return {"message": {"content": outputs.pop(0)}}

    monkeypatch.setattr(model, "_post", post)
    assert model.extract(TEXT, source(), []).claims[0].predicate == "prefers"
    assert len(requests) == 2
    assert requests[1]["messages"][:2] == requests[0]["messages"]
    assert all(m["role"] != "assistant" for m in requests[1]["messages"])
    assert '"name": "caffè inventato"' not in requests[1]["messages"][-1]["content"]
    assert "alternative_object_form" in requests[1]["messages"][-1]["content"]


def test_incomplete_mixed_note_is_not_partially_adopted():
    text = TEXT + " Forse Nora Bell partecipa alla Riunione Est."
    data = proposal(text)
    with pytest.raises(ValueError, match="omits recognized source facts"):
        extract(data, text)


def test_separate_preference_facts_can_choose_different_representations():
    second = "Forse Nora Bell preferisce il caffè amaro."
    data = proposal()
    data["claims"].extend(
        proposal(second, predicate="preference", target="il caffè amaro", epistemic="proposed")[
            "claims"
        ]
    )
    result = extract(data, TEXT + " " + second)
    assert [(c.predicate, c.epistemic) for c in result.claims] == [
        ("prefers", "explicit"), ("preference", "proposed"),
    ]


def test_retry_and_foreground_race_adopt_saved_mixed_note_only_once(engine, monkeypatch):
    text = TEXT + " Forse Nora Bell partecipa alla Riunione Est."
    data = proposal(TEXT)
    data["entities"].append({"ref": "meeting", "name": "Riunione Est", "kind": "event"})
    data["claims"].append(
        {
            "subject_ref": "person",
            "predicate": "participation",
            "object_ref": "meeting",
            "value": "Riunione Est",
            "statement": "Forse Nora Bell partecipa alla Riunione Est.",
            "quote": "Forse Nora Bell partecipa alla Riunione Est.",
            "epistemic": "proposed",
            "polarity": "positive",
        }
    )
    engine.settings.embedding_dimensions = 3
    engine.models = Ollama(engine.settings)
    monkeypatch.setattr(engine.models, "status", lambda: {"available": True, "model_digests": {}})
    calls = []

    def outage(*args):
        raise httpx.ReadTimeout("simulated unavailable extraction")

    monkeypatch.setattr(engine.models, "_post", outage)
    saved = engine.remember(text, scope="test", request_id="mixed-preference")
    assert saved["status"] == "stored_pending_extraction" and not saved["claim_ids"]

    def post(endpoint, body):
        if endpoint == "/api/embed":
            return {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]}
        calls.append(endpoint)
        return {"message": {"content": json.dumps(data)}}

    monkeypatch.setattr(engine.models, "_post", post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: engine.extract_source(saved["source_id"], 1), range(2)))
    assert calls == ["/api/chat"]
    assert results[0]["claim_ids"] == results[1]["claim_ids"]
    assert len(results[0]["claim_ids"]) == 2
    again = engine.remember(text, scope="test", request_id="mixed-preference")
    assert again["source_id"] == saved["source_id"] and again["revision"] == 1
    with engine.vault.transaction() as events:
        state = State(events)
        assert len(state.sources) == 1 and len(state.claims) == 2
        assert sum(e.kind == "extracted" for e in events) == 1
    assert engine.read_source(saved["source_id"], 1)["text"] == text
    claims = [engine.explain(i) for i in again["claim_ids"]]
    assert {c["epistemic"] for c in claims} == {"explicit", "proposed"}
    assert all(
        c["provenance"]["quality_checks"]["version"] == "source-contracts-v7" for c in claims
    )
