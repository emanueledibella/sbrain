import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from rdflib import OWL, Dataset, Literal

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import Extraction, instant
from second_brain.store import CorruptVault
from second_brain.vocabulary import SB, Vocabulary

from .conftest import extraction
from .test_mcp import transport_client


def relation(
    engine,
    *,
    predicate="participation",
    left="Luca",
    left_kind="person",
    right="Trip",
    right_kind="event",
    left_id=None,
    right_id=None,
    context="attendance",
    polarity="positive",
    epistemic="explicit",
    scope="personal",
    valid_from=None,
    valid_to=None,
):
    text = f"{left} {predicate} {right}."
    draft = {
        "entities": [
            {"ref": "left", "name": left, "kind": left_kind, "existing_id": left_id},
            {"ref": "right", "name": right, "kind": right_kind, "existing_id": right_id},
        ],
        "claims": [
            {
                "subject_ref": "left",
                "predicate": predicate,
                "object_ref": "right",
                "value": right,
                "statement": text,
                "quote": text,
                "context": context,
                "polarity": polarity,
                "epistemic": epistemic,
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        ],
    }
    return engine.remember_reviewed(
        text, scope=scope, extraction=draft, review_reason="Reviewed regression fixture"
    )


def deductions(engine, **kwargs):
    return engine.reason_memory("personal", **kwargs)["deductions"]


def test_comprehensive_vocabulary_rules_and_pagination(engine):
    catalog = engine.knowledge_schema(limit=100)
    assert catalog["class_count"] >= 50
    assert catalog["property_count"] >= 90
    assert {s["name"] for s in catalog["sources"]} == {
        "Schema.org",
        "PROV-O",
        "SKOS",
        "OWL-Time",
        "ORG",
    }
    next_page = engine.knowledge_schema(offset=catalog["next_offset"], limit=100)
    assert len(catalog["terms"]) + len(next_page["terms"]) == catalog["total_matches"]
    term = engine.knowledge_schema("participation", "properties")["terms"][0]
    assert term["inverse"] == "has_participant"
    assert term["label"]["it"] == "Partecipa a"
    assert (SB.participatesIn, OWL.inverseOf, SB.hasParticipant) in engine.semantics.ontology


def test_alias_normalization_and_unknown_predicates_remain_extensible(engine):
    saved = relation(engine, predicate="participates_in")
    assert len(saved["claim_ids"]) == 1
    claim = engine.explain(saved["claim_ids"][0])
    assert claim["predicate"] == "participation"
    assert claim["predicate_iri"] == str(SB.participatesIn)
    draft = extraction()
    draft["claims"][0]["predicate"] = "custom_memory_relation"
    unknown = engine.remember_reviewed(
        "The trip goes to Palermo.", extraction=draft, review_reason="Reviewed regression fixture"
    )
    assert unknown["claim_ids"]
    assert engine.validate_knowledge()["conforms"]


def test_shacl_rejects_wrong_entity_role_without_losing_source(engine):
    result = relation(engine, predicate="works_for", right="Torino", right_kind="city")
    assert result["status"] == "stored_pending_extraction"
    assert not result["claim_ids"]
    assert "SHACL" in " ".join(result["warnings"])
    assert engine.read_source(result["source_id"])["text"] == "Luca works_for Torino."


def test_shacl_checks_dates_and_missing_evidence_on_the_graph(engine, memory):
    state, projection = engine._semantic_snapshot("personal")
    graph = projection["graph"]
    assert engine.semantics.check(graph)["conforms"]
    node = projection["claim_nodes"][memory["claim_ids"][0]]
    graph.add((node, SB.validTo, Literal(instant("2026-10-11T10:00:00Z"))))
    graph.remove((node, SB.source, None))
    report = engine.semantics.check(graph)
    assert not report["conforms"]
    paths = {issue["path"] for issue in report["issues"]}
    assert {str(SB.validFrom), str(SB.source)} <= paths
    assert len(state.claims) == 1


def test_shacl_rejects_cross_scope_links_in_an_audit(engine, memory):
    _, projection = engine._semantic_snapshot("personal")
    node = projection["entities"][next(iter(projection["entities"]))]
    projection["graph"].set((node, SB.scope, Literal("work")))
    report = engine.semantics.check(projection["graph"])
    assert not report["conforms"]
    assert any("share the claim scope" in i["message"] for i in report["issues"])


def test_parallel_extractions_keep_shacl_snapshots_and_ledger_consistent(engine):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: relation(engine, left=f"Person {index}"), range(8)))
    assert all(r["claim_ids"] for r in results), results
    assert engine.status()["claims"] == 8
    assert not engine.status()["pending_extraction"]
    assert engine.validate_knowledge("personal")["conforms"]


def test_inverse_deduction_has_exact_premise_and_source_and_does_not_write(engine):
    saved = relation(engine)
    before = engine.status()["ledger_head"]
    result = engine.reason_memory("personal")
    inferred = next(r for r in result["deductions"] if r["predicate"] == str(SB.hasParticipant))
    assert inferred["premise_claim_ids"] == saved["claim_ids"]
    assert inferred["sources"][0]["source_id"] == saved["source_id"]
    assert inferred["sources"][0]["quote"] == "Luca participation Trip."
    assert inferred["rules"] == ["owl:inverseOf"]
    assert not result["saved"]
    assert engine.status()["ledger_head"] == before


def test_transitive_deduction_keeps_both_premises(engine):
    one = relation(
        engine,
        predicate="depends_on",
        left="A",
        left_kind="task",
        right="B",
        right_kind="task",
        context="planning",
    )
    b_id = engine.explain(one["claim_ids"][0])["object_id"]
    two = relation(
        engine,
        predicate="depends_on",
        left="B",
        left_kind="task",
        left_id=b_id,
        right="C",
        right_kind="task",
        context="planning",
    )
    candidates = deductions(engine, limit=100)
    derived = next(
        r
        for r in candidates
        if r["predicate"] == str(SB.dependsOn) and len(r["premise_claim_ids"]) == 2
    )
    assert set(derived["premise_claim_ids"]) == set(one["claim_ids"] + two["claim_ids"])
    assert {s["source_id"] for s in derived["sources"]} == {one["source_id"], two["source_id"]}
    assert "owl:TransitiveProperty" in derived["rules"]


def test_reasoning_does_not_combine_different_contexts(engine):
    one = relation(
        engine,
        predicate="depends_on",
        left="A",
        left_kind="task",
        right="B",
        right_kind="task",
        context="first",
    )
    b_id = engine.explain(one["claim_ids"][0])["object_id"]
    relation(
        engine,
        predicate="depends_on",
        left="B",
        left_kind="task",
        left_id=b_id,
        right="C",
        right_kind="task",
        context="second",
    )
    assert not any(len(r["premise_claim_ids"]) > 1 for r in deductions(engine, limit=100))


def test_owl_inferred_conflict_with_a_denial_suppresses_relationship_results(engine):
    first = relation(
        engine,
        predicate="depends_on",
        left="A",
        left_kind="task",
        right="B",
        right_kind="task",
        context="planning",
    )
    a = engine.explain(first["claim_ids"][0])
    second = relation(
        engine,
        predicate="depends_on",
        left="B",
        left_kind="task",
        left_id=a["object_id"],
        right="C",
        right_kind="task",
        context="planning",
    )
    b = engine.explain(second["claim_ids"][0])
    relation(
        engine,
        predicate="depends_on",
        left="A",
        left_kind="task",
        left_id=a["subject_id"],
        right="C",
        right_kind="task",
        right_id=b["object_id"],
        polarity="negative",
        context="planning",
    )
    result = engine.reason_memory("personal", context="planning")
    assert all(r["predicate"].endswith("type") for r in result["deductions"])
    assert any("closure conflicts" in w for w in result["warnings"])


def test_reasoning_recomputes_after_retraction_and_respects_known_at(engine, clock):
    saved = relation(engine)
    before = "2026-10-12T10:30:00Z"
    clock.set("2026-10-12T11:00:00Z")
    engine.change(saved["claim_ids"][0], kind="retraction", reason="Incorrect participation")
    assert not any(r["predicate"] == str(SB.hasParticipant) for r in deductions(engine))
    assert any(
        r["predicate"] == str(SB.hasParticipant) for r in deductions(engine, known_at=before)
    )


def test_future_and_uncertain_claims_never_become_current_asserted_facts(engine):
    relation(engine, valid_from="2026-10-13T10:00:00Z")
    relation(engine, left="Sara", epistemic="proposed")
    relation(engine, left="Anna", polarity="negative")
    assert not any(r["predicate"] == str(SB.hasParticipant) for r in deductions(engine))
    assert any(
        r["predicate"] == str(SB.hasParticipant)
        for r in deductions(engine, valid_at="2026-10-13T10:00:00Z")
    )


def test_negative_conflict_after_explicit_identity_merge_blocks_deduction(engine, clock):
    one = relation(engine)
    claim = engine.explain(one["claim_ids"][0])
    clock.set("2026-10-12T11:00:00Z")
    two = relation(engine, right_id=claim["object_id"], polarity="negative")
    other = engine.explain(two["claim_ids"][0])
    clock.set("2026-10-12T12:00:00Z")
    engine.identity_change(
        other["subject_id"],
        kind="merge",
        target_id=claim["subject_id"],
        reason="Reviewed same person",
    )
    result = engine.reason_memory("personal")
    assert not any(r["predicate"] == str(SB.hasParticipant) for r in result["deductions"])
    assert any("Conflicting" in w for w in result["warnings"])


@pytest.mark.parametrize("format", ["trig", "turtle", "json-ld", "nquads"])
def test_rdf_round_trip_preserves_sources_claims_and_negation(engine, format):
    relation(engine, polarity="negative")
    exported = engine.export_rdf("personal", format=format)
    dataset = Dataset()
    dataset.parse(data=exported["data"], format=format)
    all_triples = {(s, p, o) for s, p, o, _ in dataset.quads((None, None, None, None))}
    assert any(p == SB.polarity and str(o) == "negative" for s, p, o in all_triples)
    assert any(p == SB.quote for s, p, o in all_triples)
    assert not any(p == SB.participatesIn for s, p, o in all_triples)
    assert exported["claims"] == 1
    assert "embedding" not in exported["data"]


def test_export_named_graphs_separate_personal_and_work(engine):
    relation(engine, scope="personal")
    relation(engine, scope="work")
    exported = engine.export_rdf()
    dataset = Dataset().parse(data=exported["data"], format="trig")
    names = {str(g.identifier) for g in dataset.graphs()}
    assert any("asserted:personal" in n for n in names)
    assert any("asserted:work" in n for n in names)
    assert len(engine.reason_memory("personal")["deductions"]) > 0
    with pytest.raises(ValueError, match="scope"):
        engine.reason_memory("")


def test_legacy_text_relations_are_kept_without_inventing_entity_objects(engine, memory):
    exported = engine.export_rdf("personal")
    assert exported["claims"] == 1
    assert any("Text-only" in w for w in exported["warnings"])
    assert engine.validate_knowledge("personal")["conforms"]
    assert not engine.reason_memory("personal")["deductions"]


def test_local_extensions_add_classes_and_reject_overwrites(tmp_path, clock):
    extension = {
        "classes": [
            {
                "key": "research_project",
                "term": "ResearchProject",
                "parents": ["project"],
                "label": {"en": "Research project", "it": "Progetto di ricerca"},
                "aliases": [],
                "external_class": None,
            }
        ]
    }
    path = tmp_path / "extension.json"
    path.write_text(json.dumps(extension))
    engine = MemoryEngine(
        Settings(_env_file=None, vault_path=tmp_path / "vault", vocabulary_extension_path=path),
        graph=None,
        models=None,
        clock=clock,
    )
    result = relation(engine, predicate="works_on", right="Research", right_kind="research_project")
    assert result["claim_ids"]
    assert any(r["predicate"] == str(SB.hasContributor) for r in deductions(engine))
    extension["classes"][0]["key"] = "person"
    path.write_text(json.dumps(extension))
    with pytest.raises(ValueError, match="overwrite"):
        Vocabulary(path)


def test_semantic_export_and_reasoning_fail_closed_on_source_tampering(engine, memory):
    with engine.vault.transaction() as events:
        source = engine._state(events).sources[f"{memory['source_id']}:1"]
    (engine.vault.root / source.path).write_text("Tampered")
    with pytest.raises(CorruptVault):
        engine.export_rdf("personal")
    with pytest.raises(CorruptVault):
        engine.reason_memory("personal")


def test_semantic_snapshot_is_bounded(engine, memory):
    engine.remember_reviewed(
        "The trip goes to Palermo.",
        extraction=extraction(),
        review_reason="Reviewed regression fixture",
    )
    engine.semantics.max_claims = 1
    with pytest.raises(ValueError, match="exceeds"):
        engine.export_rdf("personal")


def test_shacl_model_repair_uses_formal_validation(engine, monkeypatch):
    from .test_mcp_regressions import mocked_model

    pending = engine.remember("Luca works_for Company.")
    bad = {
        "entities": [
            {"ref": "l", "name": "Luca", "kind": "person"},
            {"ref": "c", "name": "Company", "kind": "city"},
        ],
        "claims": [
            {
                "subject_ref": "l",
                "object_ref": "c",
                "predicate": "works_for",
                "value": "Company",
                "statement": "Luca works_for Company.",
                "quote": "Luca works_for Company.",
                "epistemic": "explicit",
                "polarity": "positive",
            }
        ],
    }
    good = Extraction.model_validate(bad).model_dump(mode="json")
    good["entities"][1]["kind"] = "company"
    bodies = mocked_model(engine, monkeypatch, [bad, good])
    indexed = engine.extract_source(pending["source_id"])
    assert indexed["claim_ids"]
    assert len(bodies) == 2
    assert "SHACL" in bodies[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_semantic_tools_over_real_mcp_transport(tmp_path, transport):
    from mcp import ClientSession

    async with transport_client(tmp_path, transport) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            for name in ("knowledge_schema", "validate_knowledge", "export_rdf", "reason_memory"):
                assert tools[name].annotations.readOnlyHint
            schema = await session.call_tool("knowledge_schema", {"query": "participation"})
            assert schema.structuredContent["terms"]
            export = await session.call_tool("export_rdf", {"format": "trig"})
            assert not export.isError
            assert "owl:inverseOf" in export.structuredContent["data"]
            audit = await session.call_tool("validate_knowledge", {})
            assert audit.structuredContent["conforms"]
            reasoned = await session.call_tool("reason_memory", {"scope": "personal"})
            assert reasoned.structuredContent["saved"] is False


def test_cli_exports_actual_rdf_instead_of_a_json_envelope(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "second_brain",
            "--offline",
            "--vault",
            str(tmp_path / "vault"),
            "export-rdf",
            "--scope",
            "personal",
            "--format",
            "trig",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    dataset = Dataset().parse(data=result.stdout, format="trig")
    assert any(p == OWL.inverseOf for _, p, _, _ in dataset.quads((None, None, None, None)))
