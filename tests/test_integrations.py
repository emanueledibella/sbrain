import os

import httpx
import pytest
from neo4j.exceptions import CypherTypeError

from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import SourceRevision, instant, uid
from second_brain.ollama import Ollama
from second_brain.state import State

from .conftest import extraction


class LocalVectors:
    def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def live_graph_engine(tmp_path, clock):
    if os.getenv("SB_TEST_NEO4J") != "1":
        pytest.skip("Set SB_TEST_NEO4J=1 to run against local Neo4j")
    settings = Settings(
        vault_path=tmp_path / "integration", embedding_dimensions=3, embedding_model="test-only"
    )
    engine = MemoryEngine(settings, models=LocalVectors(), clock=clock)
    engine.graph.initialize()
    yield engine
    engine.graph.query("MATCH (n) WHERE n.vault_id=$vault DETACH DELETE n", vault=engine.vault.id)
    engine.close()


@pytest.mark.integration
def test_live_neo4j_projection_relations_search_and_temporal_replay(live_graph_engine, clock):
    engine = live_graph_engine
    result = engine.remember("The trip goes to Palermo.", extraction=extraction())
    assert not result["warnings"], result
    clock.set("2026-10-14T10:00:00Z")
    changed = engine.change(
        result["claim_ids"][0],
        kind="update",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="Change of plans",
        effective_at="2026-10-13T10:00:00Z",
    )
    assert not changed["warnings"], changed
    rows = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault})-[:ABOUT]->(e:Entity), "
        "(a)-[r:SUPPORTED_BY]->(s:SourceRevision) "
        "RETURN a.value AS value, r.quote AS quote, s.revision AS revision",
        vault=engine.vault.id,
    )
    assert len(rows) == 3
    assert {r["value"] for r in rows} == {"Palermo", "Catania"}
    recalled = engine.recall("trip destination", valid_at="2026-10-12T12:00:00Z")
    assert not recalled["warnings"], recalled
    assert {c["value"] for c in recalled["memories"]} == {"Palermo"}
    # Delete only this disposable test vault's projection, then recover from canonical records.
    engine.graph.query("MATCH (n) WHERE n.vault_id=$vault DETACH DELETE n", vault=engine.vault.id)
    assert not engine.replay()["warnings"]
    assert (
        engine.graph.query(
            "MATCH (a:Assertion {vault_id:$vault}) RETURN count(a) AS n", vault=engine.vault.id
        )[0]["n"]
        == 3
    )
    assert engine.graph.search("Palermo", [1.0, 0.0, 0.0], "personal")


@pytest.mark.integration
def test_live_neo4j_projection_rolls_back_on_failure(live_graph_engine):
    engine = live_graph_engine
    engine.remember("The trip goes to Palermo.", extraction=extraction())
    with engine.vault.transaction() as events:
        state = State(events)
    # A nested object is not a legal Neo4j property; error after DELETE must roll it back.
    state.entities[next(iter(state.entities))].kind = {"invalid": "property"}
    with pytest.warns(UserWarning, match="Pydantic serializer"), pytest.raises(CypherTypeError):
        engine.graph.sync(state, force=True)
    assert (
        engine.graph.query(
            "MATCH (a:Assertion {vault_id:$vault}) RETURN count(a) AS n", vault=engine.vault.id
        )[0]["n"]
        == 1
    )


@pytest.mark.integration
def test_live_neo4j_separate_vault_is_not_deleted(live_graph_engine, tmp_path):
    engine = live_graph_engine
    engine.remember("The trip goes to Palermo.", extraction=extraction())
    other = MemoryEngine(
        engine.settings.model_copy(update={"vault_path": tmp_path / "other"}),
        models=LocalVectors(),
        clock=engine.clock,
    )
    try:
        result = other.remember(
            "The trip goes to Catania.",
            extraction=extraction("The trip goes to Catania.", value="Catania"),
        )
        assert not result["warnings"]
        engine.replay()
        assert (
            other.graph.query(
                "MATCH (a:Assertion {vault_id:$vault}) RETURN count(a) AS n", vault=other.vault.id
            )[0]["n"]
            == 1
        )
        assert {c["value"] for c in engine.recall("trip")["memories"]} == {"Palermo"}
    finally:
        other.graph.query("MATCH (n) WHERE n.vault_id=$vault DETACH DELETE n", vault=other.vault.id)
        other.close()


@pytest.mark.integration
def test_live_ollama_structured_extraction_and_embeddings(tmp_path):
    if os.getenv("SB_TEST_OLLAMA") != "1":
        pytest.skip("Set SB_TEST_OLLAMA=1 to run actual local inference")
    settings = Settings(vault_path=tmp_path / "ollama")
    models = Ollama(settings)
    text = "The trip destination is Palermo."
    source = SourceRevision(
        source_id=uid(),
        revision=1,
        title="Test",
        scope="test",
        kind="note",
        sha256="unused",
        path="unused",
        observed_at=instant("2026-10-12T10:00:00Z"),
        recorded_at=instant("2026-10-12T10:00:00Z"),
    )
    draft = models.extract(text, source, [])
    MemoryEngine._validate_evidence(draft, text)
    assert draft.claims, "The actual local model must extract this explicit simple fact"
    assert any("Palermo" in c.value for c in draft.claims)
    embeddings = models.embed(["The trip goes to Palermo.", "Where are we going on the trip?"])
    assert len(embeddings) == 2
    assert len(embeddings[0]) == settings.embedding_dimensions


@pytest.mark.integration
def test_live_engine_end_to_end(tmp_path):
    if os.getenv("SB_TEST_NEO4J") != "1" or os.getenv("SB_TEST_OLLAMA") != "1":
        pytest.skip("Set SB_TEST_NEO4J=1 and SB_TEST_OLLAMA=1 for complete local path")
    engine = MemoryEngine(Settings(vault_path=tmp_path / "end-to-end"))
    try:
        result = engine.remember("The trip destination is Palermo.", scope="test")
        assert result["status"] == "indexed", result
        assert result["claim_ids"]
        recalled = engine.recall("Where are we going on the trip?", scope="test")
        assert not recalled["warnings"], recalled
        assert any(c["value"] == "Palermo" for c in recalled["memories"])
        original = engine.explain(result["claim_ids"][0])
        before = original["versions"][0]["recorded_from"]
        correction = engine.change(
            original["id"],
            kind="correction",
            value="Catania",
            statement="The trip destination is Catania.",
            reason="Explicit correction",
        )
        assert not correction["warnings"]
        assert {
            c["value"] for c in engine.recall("trip destination", scope="test")["memories"]
        } == {"Catania"}
        past = engine.recall("trip destination", scope="test", known_at=before)
        assert {c["value"] for c in past["memories"]} == {"Palermo"}
        assert (
            engine.read_source(result["source_id"], 1)["text"] == "The trip destination is Palermo."
        )
    finally:
        engine.graph.query(
            "MATCH (n) WHERE n.vault_id=$vault DETACH DELETE n", vault=engine.vault.id
        )
        engine.close()


@pytest.mark.integration
def test_live_graph_expands_context_through_object(live_graph_engine):
    engine = live_graph_engine
    note = "Marco will join. The destination is Palermo."
    result = engine.remember(
        note,
        extraction={
            "entities": [
                {"ref": "m", "name": "Marco", "kind": "person"},
                {"ref": "g", "name": "Trip", "kind": "event"},
            ],
            "claims": [
                {
                    "subject_ref": "m",
                    "object_ref": "g",
                    "predicate": "participates_in",
                    "value": "yes",
                    "statement": "Marco will join.",
                    "quote": "Marco will join.",
                },
                {
                    "subject_ref": "g",
                    "predicate": "destination",
                    "value": "Palermo",
                    "statement": "The destination is Palermo.",
                    "quote": "The destination is Palermo.",
                },
            ],
        },
    )
    assert not result["warnings"]
    # No vector and no "Marco" in the destination: result must use ABOUT/OBJECT expansion.
    scores = engine.graph.search("Marco", None, "personal")
    assert set(result["claim_ids"]) <= set(scores)


def test_ollama_repairs_invalid_entity_references_once(monkeypatch):
    settings = Settings(_env_file=None)
    models = Ollama(settings)
    original = extraction()
    invalid = {"entities": [], "claims": original["claims"]}
    outputs = [invalid, original]
    bodies = []

    def post(endpoint, body):
        bodies.append(body.copy())
        return {"message": {"content": __import__("json").dumps(outputs.pop(0))}}

    monkeypatch.setattr(models, "_post", post)
    source = SourceRevision(
        source_id=uid(),
        revision=1,
        title="Test",
        scope="test",
        kind="note",
        sha256="unused",
        path="unused",
        observed_at=instant("2026-10-12T10:00:00Z"),
        recorded_at=instant("2026-10-12T10:00:00Z"),
    )
    assert models.extract("The trip goes to Palermo.", source, []).claims[0].value == "Palermo"
    assert len(bodies) == 2
    assert bodies[0]["format"]["required"] == ["entities", "claims"]
    assert "unknown entity" in bodies[1]["messages"][-1]["content"]


def test_ollama_does_not_retry_validation_forever(monkeypatch):
    models = Ollama(Settings(_env_file=None))
    calls = []

    def post(endpoint, body):
        calls.append(endpoint)
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(models, "_post", post)
    source = SourceRevision(
        source_id=uid(),
        revision=1,
        title="Test",
        scope="test",
        kind="note",
        sha256="unused",
        path="unused",
        observed_at=instant("2026-10-12T10:00:00Z"),
        recorded_at=instant("2026-10-12T10:00:00Z"),
    )
    with pytest.raises(ValueError):
        models.extract("The trip goes to Palermo.", source, [])
    assert len(calls) == 2


def test_ollama_uses_schema_and_validates_dimensions(monkeypatch):
    settings = Settings(_env_file=None, embedding_dimensions=3)
    models = Ollama(settings)
    requests = []

    def fake_post(self, url, json):
        requests.append(json)
        return httpx.Response(
            200, json={"embeddings": [[1, 2]]}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(ValueError, match="dimensions"):
        models.embed(["test"])
    assert requests[0]["truncate"] is False


def test_reembedding_keeps_claims_and_evidence(engine, memory):
    engine.models = LocalVectors()
    before = engine.explain(memory["claim_ids"][0])
    assert engine.reembed()["updated"] == 1
    after = engine.explain(memory["claim_ids"][0])
    assert before["evidence"] == after["evidence"]
    assert before["id"] == after["id"]
    with engine.vault.transaction() as events:
        state = State(events)
    assert state.current[before["id"]].claim.embedding == [1, 0, 0]
