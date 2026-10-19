"""Safety specifications: intentionally fail when an unsupported fact is adopted.

HTTP model output is simulated; all production extraction and adoption checks run.
All vaults are disposable pytest temporary directories, never the deployed vault.
"""

import json
import subprocess
import sys

import pytest
from mcp import ClientSession

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import instant
from second_brain.ollama import Ollama
from second_brain.quality import repair_feedback
from second_brain.state import State
from second_brain.worker import RecoveryWorker
from tests.test_mcp import transport_client
from tests.test_semantics import relation


@pytest.fixture
def engine(tmp_path):
    return MemoryEngine(
        Settings(
            _env_file=None,
            vault_path=tmp_path / "vault",
            embedding_dimensions=3,
            embedding_model="audit-only",
        ),
        graph=None,
        models=None,
        clock=lambda: instant("2026-10-18T10:00:00Z"),
    )


@pytest.fixture
def automatic(engine, monkeypatch):
    model = Ollama(engine.settings)
    engine.models = model
    monkeypatch.setattr(
        model, "status", lambda: {"model_digests": {"audit-only:latest": "synthetic-weights-v1"}}
    )

    calls = []

    def submit(text, data, **options):
        def post(endpoint, body):
            calls.append(endpoint)
            if endpoint == "/api/chat":
                return {"message": {"content": json.dumps(data)}}
            return {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]}

        monkeypatch.setattr(model, "_post", post)
        return engine.remember(text, scope="audit", **options)

    submit.calls = calls
    return submit


def pair(
    text,
    subject="Nora Bell",
    predicate="works_for",
    value="Cedar Labs",
    subject_kind="person",
    object_kind="organization",
    **fields,
):
    entities = [{"ref": "s", "name": subject, "kind": subject_kind}]
    if object_kind:
        entities.append({"ref": "o", "name": value, "kind": object_kind})
    claim = {
        "subject_ref": "s",
        "predicate": predicate,
        "value": value,
        "statement": text,
        "quote": text,
        "polarity": "positive",
        "epistemic": "explicit",
        **fields,
    }
    if object_kind:
        claim["object_ref"] = "o"
    return {"entities": entities, "claims": [claim]}


@pytest.mark.parametrize(
    "text,data",
    [
        ("Nora Bell works for Cedar Labs?", {}),
        ("Nora Bell lavora per Cedar Labs?", {}),
        (
            "Nora Bell partecipa alla Riunione Nord?",
            {"predicate": "participation", "value": "Riunione Nord", "object_kind": "event"},
        ),
        (
            "Nora Bell non partecipa alla Riunione Nord?",
            {
                "predicate": "participation",
                "value": "Riunione Nord",
                "object_kind": "event",
                "polarity": "negative",
            },
        ),
        (
            "Il costo del Piano Studio è 47,25 euro al mese?",
            {
                "subject": "Piano Studio",
                "subject_kind": "entity",
                "predicate": "price",
                "value": "47,25 euro al mese",
                "object_kind": None,
            },
        ),
    ],
)
def test_questions_do_not_become_explicit_assertions(automatic, text, data):
    result = automatic(text, pair(text, **data))
    assert not result["claim_ids"], f"Question adopted as explicit fact: {result}"


@pytest.mark.parametrize(
    "name",
    [
        "Gita del 18 ottobre 2027 alle 09:00 (UTC+02:00)",
        "Trip on 18 october 2027 at 09:00 (UTC+02:00)",
    ],
)
def test_event_date_in_a_name_does_not_date_destination(automatic, name):
    text = f"La destinazione della {name} è Catania."
    data = pair(
        text, name, "destination", "Catania", "event", "place", valid_from="2027-10-18T07:00:00Z"
    )
    result = automatic(text, data)
    assert not result["claim_ids"], f"Date in event name became fact validity: {result}"


@pytest.mark.parametrize("target_kind", ["company", "team", "institution"])
def test_new_organization_subtype_requires_source_support(engine, automatic, target_kind):
    text = "Nora Bell works for Cedar Labs."
    result = automatic(text, pair(text, object_kind=target_kind))
    if result["claim_ids"]:
        obj = engine.explain(result["claim_ids"][0])["object"]
        assert obj["kind"] == "organization", f"Unsupported entity classification: {obj}"


@pytest.mark.parametrize("event_kind", ["meeting", "appointment", "transaction"])
def test_participation_does_not_prove_specific_event_type(engine, automatic, event_kind):
    text = "Nora Bell partecipa a Evento Nord."
    result = automatic(
        text,
        pair(
            text,
            predicate="participation",
            value="Evento Nord",
            subject_kind="agent",
            object_kind=event_kind,
        ),
    )
    if result["claim_ids"]:
        obj = engine.explain(result["claim_ids"][0])["object"]
        assert obj["kind"] == "event", f"Unsupported event classification: {obj}"


def test_structured_mcp_path_cannot_flip_source_negation(engine):
    text = "Nora Bell non partecipa alla Riunione Nord."
    data = pair(
        text,
        predicate="participation",
        value="Riunione Nord",
        object_kind="event",
        statement="Nora Bell partecipa alla Riunione Nord.",
    )
    result = engine.remember(text, scope="audit", extraction=data)
    assert not result["claim_ids"], f"Structured extraction bypassed negation checks: {result}"


def test_structured_mcp_path_cannot_fabricate_subject_relation_and_target(engine):
    text = "Promemoria: compra il pane."
    data = pair(
        text,
        predicate="participation",
        value="Riunione Nord",
        object_kind="event",
        statement="Nora Bell partecipa alla Riunione Nord.",
    )
    result = engine.remember(text, scope="audit", extraction=data)
    assert not result["claim_ids"], f"Unrelated quote licensed fabricated fact: {result}"


@pytest.mark.parametrize("wrong_kind", ["company", "meeting"])
def test_classification_of_unused_object_does_not_survive_retraction(engine, wrong_kind):
    result = relation(
        engine,
        predicate="works_for" if wrong_kind == "company" else "participation",
        right="Cedar Labs" if wrong_kind == "company" else "Evento Nord",
        right_kind=wrong_kind,
    )
    engine.change(
        result["claim_ids"][0], kind="retraction", reason="Wrong extraction, including type"
    )
    # Retraction of a supporting claim should not leave unsupported specific types as
    # independent OWL premises after a separate active memory creates a reasoning context.
    relation(
        engine,
        left="Lia Moon",
        predicate="works_for",
        right="Maple Labs",
        right_kind="organization",
    )
    deductions = engine.reason_memory("personal")["deductions"]
    old_object = engine.explain(result["claim_ids"][0])["object_id"]
    assert not any(old_object in d["subject"] for d in deductions), deductions


def test_approved_identity_merge_does_not_make_unique_person_ambiguous(engine, automatic):
    first = relation(engine, left="Nora Bell", right="Evento Uno", scope="audit")
    second = relation(engine, left="Nora Bell", right="Evento Due", scope="audit")
    one = engine.explain(first["claim_ids"][0])["subject_id"]
    two = engine.explain(second["claim_ids"][0])["subject_id"]
    engine.identity_change(two, kind="merge", target_id=one, reason="Owner confirmed same person")
    text = "Nora Bell works for Cedar Labs."
    result = automatic(text, pair(text))
    assert result["claim_ids"], f"Approved single identity still treated as homonyms: {result}"


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": "Nora"},
        {"value": "Cedar"},
        {"polarity": "negative"},
        {"epistemic": "inferred"},
        {"predicate": "works_on"},
        {"valid_from": "2030-01-01T10:00:00Z"},
    ],
)
def test_existing_controls_still_reject_simple_fabrications(automatic, changes):
    text = "Nora Bell works for Cedar Labs."
    result = automatic(text, pair(text, **changes))
    assert not result["claim_ids"]


@pytest.mark.parametrize(
    "text",
    [
        "Nora Bell works for Cedar Labs.",
        "Nora Bell lavora per Cedar Labs.",
    ],
)
def test_declarative_control_is_adopted(automatic, text):
    assert automatic(text, pair(text))["claim_ids"]


@pytest.mark.parametrize(
    "text,predicate,target,subject,subject_kind,object_kind",
    [
        (
            "Nora Bell partecipa alla Riunione Nord solo se Marco conferma.",
            "participation",
            "Riunione Nord solo se Marco conferma",
            "Nora Bell",
            "person",
            "event",
        ),
        (
            "Nora Bell lavora per Cedar Labs o Maple Labs.",
            "works_for",
            "Cedar Labs o Maple Labs",
            "Nora Bell",
            "person",
            "organization",
        ),
        (
            "La destinazione della Gita Nord è Catania oppure Palermo.",
            "destination",
            "Catania oppure Palermo",
            "Gita Nord",
            "event",
            "place",
        ),
    ],
)
def test_conditions_and_disjunctions_do_not_become_one_certain_entity(
    automatic, text, predicate, target, subject, subject_kind, object_kind
):
    result = automatic(text, pair(text, subject, predicate, target, subject_kind, object_kind))
    assert not result["claim_ids"], f"Logical qualifier became an asserted entity: {result}"


def test_question_mark_inside_url_is_not_a_question(automatic):
    text = "Il sito di Nora Bell è https://example.test/search?q=tea."
    data = pair(text, predicate="url", value="https://example.test/search?q=tea", object_kind=None)
    assert automatic(text, data)["claim_ids"]


@pytest.mark.parametrize(
    "predicate,source_object,wrong_object",
    [
        ("works_for", "Cedar Labs", "Maple Labs"),
        ("participation", "Riunione Nord", "Riunione Sud"),
    ],
)
def test_automatic_facts_cannot_borrow_targets_from_another_clause(
    automatic, predicate, source_object, wrong_object
):
    verb = "works for" if predicate == "works_for" else "attends"
    text = f"Nora Bell {verb} {source_object}. Lia Moon {verb} {wrong_object}."
    data = pair(
        text,
        predicate=predicate,
        value=wrong_object,
        object_kind="organization" if predicate == "works_for" else "event",
    )
    assert not automatic(text, data)["claim_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize("tool", ["remember", "extract_source"])
async def test_structured_bypass_is_reachable_over_actual_mcp(tmp_path, transport, tool):
    text = "Nora Bell non partecipa alla Riunione Nord."
    data = pair(
        text,
        predicate="participation",
        value="Riunione Nord",
        object_kind="event",
        statement="Nora Bell partecipa alla Riunione Nord.",
    )
    async with transport_client(tmp_path, transport) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            if tool == "remember":
                arguments = {"text": text, "extraction": data}
            else:
                saved = await session.call_tool("remember", {"text": text})
                arguments = {"source_id": saved.structuredContent["source_id"], "extraction": data}
            result = await session.call_tool(tool, arguments)
            assert result.isError or not result.structuredContent.get("claim_ids"), (
                "MCP adopted an affirmative fact from explicitly negative source text",
                result.structuredContent,
            )


@pytest.mark.parametrize("chosen", [0, 1])
def test_model_cannot_select_an_arbitrary_existing_id_among_homonyms(engine, automatic, chosen):
    known = [
        relation(engine, left="Nora Bell", right="Evento Uno", scope="audit"),
        relation(engine, left="Nora Bell", right="Evento Due", scope="audit"),
    ]
    ids = [engine.explain(item["claim_ids"][0])["subject_id"] for item in known]
    assert ids[0] != ids[1]
    text = "Nora Bell works for Cedar Labs."
    data = pair(text)
    data["entities"][0]["existing_id"] = ids[chosen]
    result = automatic(text, data)
    assert not result["claim_ids"], (
        f"Model UUID selected a homonym without identifying evidence: {result}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Marta Riva lavora per Officina Cedro?",
        "Marta Riva non partecipa alla Riunione Levante?",
        "Mi chiedo se Marta Riva lavora per Officina Cedro.",
        "I wonder whether Nora Bell works for Cedar Labs.",
        "Marta Riva lavora per Officina Cedro?!",
        "Marta Riva lavora per Officina Cedro？",
        "¿Marta Riva lavora per Officina Cedro.",
    ],
)
def test_correct_abstention_is_not_repaired_into_a_fact(engine, automatic, text):
    result = automatic(text, {"entities": [], "claims": []}, request_id="nonassertive")
    assert not result["claim_ids"]
    assert result["status"] != "stored_pending_extraction", result
    assert automatic.calls.count("/api/chat") == 1, "Correct abstention must not trigger repair"
    assert not engine.recall("Marta Nora", scope="audit")["memories"]
    assert not engine.reason_memory("audit")["deductions"]
    assert repair_feedback(text, engine.vocabulary)["empty_extraction_required"]
    with engine.vault.transaction() as events:
        state = State(events)
        assert len(state.sources) == 1 and not state.entities and not state.claims
        assert len(state.extracted) == 1


def test_mixed_note_requires_only_assertive_facts(engine, automatic):
    fact = "Nora Bell works for Cedar Labs."
    question = "Lia Moon partecipa alla Riunione Levante?"
    text = fact + " " + question
    result = automatic(text, pair(fact))
    assert len(result["claim_ids"]) == 1, result
    assert engine.explain(result["claim_ids"][0])["evidence"][0]["quote"] == fact
    hints = repair_feedback(text, engine.vocabulary)
    assert hints["total_contracts"] == 1
    assert hints["source_contracts"][0]["subject_name"] == "Nora Bell"


@pytest.mark.parametrize(
    "mutation",
    [
        {"subject": "Nora"},
        {"value": "Cedar"},
        {"polarity": "negative"},
        {"epistemic": "inferred"},
        {"predicate": "works_on"},
        {"valid_from": "2030-01-01T10:00:00Z"},
    ],
)
def test_structured_submissions_share_all_source_contracts(engine, mutation):
    text = "Nora Bell works for Cedar Labs."
    result = engine.remember(text, scope="audit", extraction=pair(text, **mutation))
    assert not result["claim_ids"], result
    assert engine.read_source(result["source_id"])["text"] == text


@pytest.mark.parametrize("supplied", [None, "2027-10-18T07:00:00Z", "2027-10-18T09:00:00Z"])
def test_effective_prefix_has_one_role_and_must_be_preserved(automatic, supplied):
    text = "Dal 18 ottobre 2027 alle 09:00 (UTC+02:00), Nora Bell lavora per Cedar Labs."
    result = automatic(text, pair(text, valid_from=supplied))
    assert bool(result["claim_ids"]) == (supplied == "2027-10-18T07:00:00Z"), result


def test_timestamp_in_event_name_can_remain_part_of_identity(engine, automatic):
    name = "Gita del 18 ottobre 2027 alle 09:00 (UTC+02:00)"
    text = f"La destinazione della {name} è Catania."
    result = automatic(text, pair(text, name, "destination", "Catania", "event", "place"))
    assert result["claim_ids"], result
    claim = engine.explain(result["claim_ids"][0])
    assert claim["subject"]["name"] == name
    assert instant(claim["valid_from"]) == engine.clock()


@pytest.mark.parametrize(
    "name", ['"Research or Development"', '"Se Studio"', "Research and Development"]
)
def test_explicitly_quoted_operators_and_ordinary_and_in_names_are_not_logic(automatic, name):
    text = f"Nora Bell works for {name}."
    result = automatic(text, pair(text, value=name.strip('"')))
    assert result["claim_ids"], result


def test_supported_known_subtype_is_preserved(engine, automatic):
    known = relation(
        engine,
        left="Nora Bell",
        predicate="works_for",
        right="Cedar Labs",
        right_kind="company",
        scope="audit",
    )
    old = engine.explain(known["claim_ids"][0])
    text = "Nora Bell lavora per Cedar Labs."
    data = pair(text, object_kind="company")
    data["entities"][0]["existing_id"] = old["subject_id"]
    data["entities"][1]["existing_id"] = old["object_id"]
    result = automatic(text, data)
    assert result["claim_ids"], result
    assert engine.explain(result["claim_ids"][0])["object"]["kind"] == "company"


def test_conditional_remains_durable_needs_review_after_worker_restart(
    engine,
    automatic,
    tmp_path,
):
    engine.settings.worker_queue_path = tmp_path / "jobs.sqlite3"
    text = "Nora Bell lavora per Cedar Labs solo se Marco conferma."
    data = pair(text, value="Cedar Labs solo se Marco conferma")
    saved = automatic(text, data, request_id="conditional")
    assert saved["status"] == "stored_pending_extraction"
    worker = RecoveryWorker(engine)
    assert worker.run_once()
    assert engine.source_status(saved["source_id"])["status"] == "needs_review"
    # A new process-equivalent engine reads the same durable ledger and queue.
    reopened = MemoryEngine(engine.settings, graph=None, models=engine.models, clock=engine.clock)
    try:
        assert not RecoveryWorker(reopened).run_once()
        assert reopened.source_status(saved["source_id"])["status"] == "needs_review"
        assert not reopened.recall("Nora", scope="audit")["memories"]
        assert reopened.read_source(saved["source_id"])["text"] == text
        with reopened.vault.transaction() as events:
            state = State(events)
            assert len(state.sources) == 1 and not state.claims
    finally:
        reopened.close()


def test_type_support_respects_retraction_and_both_time_axes(tmp_path):
    at = [instant("2026-10-18T10:00:00Z")]
    engine = MemoryEngine(
        Settings(_env_file=None, vault_path=tmp_path / "types"),
        graph=None,
        models=None,
        clock=lambda: at[0],
    )
    original = relation(engine, predicate="works_for", right="Cedar Labs", right_kind="company")
    obj = engine.explain(original["claim_ids"][0])["object_id"]
    before = "2026-10-18T10:01:00Z"
    at[0] = instant("2026-10-18T11:00:00Z")
    engine.change(original["claim_ids"][0], kind="retraction", reason="Type was also wrong")
    relation(
        engine,
        left="Lia Moon",
        predicate="works_for",
        right="Maple Labs",
        right_kind="organization",
    )
    current = engine.reason_memory("personal")["deductions"]
    assert not any(obj in d["subject"] for d in current)
    past = engine.reason_memory("personal", known_at=before)["deductions"]
    assert any(
        obj in d["subject"] and d["premise_claim_ids"] == original["claim_ids"] for d in past
    )
    # The archival identity/type record and original source are not deleted.
    assert engine.explain(original["claim_ids"][0])["object"]["kind"] == "company"


def test_owner_review_is_explicit_auditable_and_unavailable_as_an_mcp_tool(engine):
    from second_brain.server import create_server

    text = "Owner-reviewed shorthand: Nora / Cedar."
    data = pair(text, statement="Nora Bell works for Cedar Labs.")
    assert not engine.remember(text, extraction=data)["claim_ids"]
    with pytest.raises(ValueError, match="nonempty"):
        engine.remember_reviewed(text, extraction=data, review_reason=" ")
    saved = engine.remember_reviewed(
        text, extraction=data, review_reason="Owner confirmed imported mapping"
    )
    assert saved["claim_ids"], saved
    provenance = engine.explain(saved["claim_ids"][0])["provenance"]
    assert provenance["origin"] == "owner_reviewed"
    assert provenance["owner_review_reason"] == "Owner confirmed imported mapping"
    assert "adoption_checks" not in provenance
    tools = create_server(engine)._tool_manager._tools
    assert "remember_reviewed" not in tools and "extract_source_reviewed" not in tools


def test_cli_owner_review_requires_explicit_flag_and_reuses_saved_source(tmp_path):
    text = "Owner-reviewed shorthand: Nora / Cedar."
    source_file = tmp_path / "note.md"
    source_file.write_text(text)
    draft_file = tmp_path / "reviewed.json"
    draft_file.write_text(json.dumps(pair(text, statement="Nora Bell works for Cedar Labs.")))
    base = [sys.executable, "-m", "second_brain", "--offline", "--vault", str(tmp_path / "vault")]

    def run(*args):
        return subprocess.run(
            base + list(args), cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
        )

    ordinary = run("remember", "--file", str(source_file), "--extraction", str(draft_file))
    assert ordinary.returncode == 2, ordinary.stderr
    saved = json.loads(ordinary.stdout)
    assert not saved["claim_ids"] and saved["status"] == "stored_pending_extraction"
    missing = run("extract", saved["source_id"], "--owner-review-reason", "Confirmed mapping")
    assert missing.returncode == 1 and "requires --extraction" in missing.stderr
    imported = run(
        "extract",
        saved["source_id"],
        "--extraction",
        str(draft_file),
        "--owner-review-reason",
        "Owner confirmed imported mapping",
    )
    assert imported.returncode == 2, imported.stderr  # Offline indexing reports disabled services.
    result = json.loads(imported.stdout)
    assert len(result["claim_ids"]) == 1
    repeated = run(
        "extract",
        saved["source_id"],
        "--extraction",
        str(draft_file),
        "--owner-review-reason",
        "Owner confirmed imported mapping",
    )
    assert json.loads(repeated.stdout)["claim_ids"] == result["claim_ids"]
    status = json.loads(run("status").stdout)
    assert status["sources"] == status["claims"] == 1


def test_independently_active_type_support_survives_another_claim_retraction(engine):
    first = "Nora Bell works for Cedar Labs."
    second = "Lia Moon works for Cedar Labs."
    data = pair(first, object_kind="company")
    data["entities"].append({"ref": "lia", "name": "Lia Moon", "kind": "person"})
    data["claims"].append(
        {**data["claims"][0], "subject_ref": "lia", "quote": second, "statement": second}
    )
    saved = engine.remember_reviewed(
        first + " " + second,
        extraction=data,
        review_reason="Owner confirmed both jobs and company type",
    )
    assert len(saved["claim_ids"]) == 2
    obj = engine.explain(saved["claim_ids"][0])["object_id"]
    engine.change(saved["claim_ids"][0], kind="retraction", reason="First job was mistaken")
    deductions = engine.reason_memory("personal")["deductions"]
    assert any(
        obj in d["subject"] and d["premise_claim_ids"] == [saved["claim_ids"][1]]
        for d in deductions
    )
