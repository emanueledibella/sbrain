"""Regressions from the real MCP test: similar identities, retries and throttling."""

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from starlette.responses import JSONResponse

from second_brain.auth import LocalTokens, RequestLimits
from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.models import Extraction, uid
from second_brain.ollama import Ollama
from second_brain.state import State

from .conftest import extraction
from .test_mcp import READ_TOKEN, WRITE_TOKEN, transport_client


def trip_draft(name, value="Torino", existing_id=None):
    text = f"La destinazione della {name} è {value}."
    draft = extraction(text, value=value, existing_id=existing_id)
    draft["entities"][0]["name"] = name
    draft["claims"][0]["polarity"] = "positive"
    return text, draft


def mocked_model(engine, monkeypatch, outputs):
    model = Ollama(engine.settings)
    bodies = []

    def post(endpoint, body):
        if endpoint == "/api/embed":
            vector = [1.0] + [0.0] * (engine.settings.embedding_dimensions - 1)
            return {"embeddings": [vector for _ in body["input"]]}
        bodies.append(json.loads(json.dumps(body)))
        return {"message": {"content": json.dumps(outputs.pop(0))}}

    monkeypatch.setattr(model, "_post", post)
    monkeypatch.setattr(model, "provenance", lambda: {})
    monkeypatch.setattr(model, "status", lambda: {"available": True})
    engine.models = model
    return bodies


def test_local_extractor_repairs_borrowed_identity_without_merging_similar_events(
    engine, monkeypatch
):
    a_text, a = trip_draft("Gita Test MCP 20261013-A")
    saved_a = engine.remember(a_text, scope="test-mcp", extraction=a)
    a_id = engine.explain(saved_a["claim_ids"][0])["subject_id"]
    b_text, bad = trip_draft("Gita Test MCP 20261013-B", existing_id=a_id)
    _, repaired = trip_draft("Gita Test MCP 20261013-B")
    bodies = mocked_model(engine, monkeypatch, [bad, repaired])
    saved_b = engine.remember(b_text, scope="test-mcp")
    assert len(saved_b["claim_ids"]) == 1
    assert engine.explain(saved_b["claim_ids"][0])["subject_id"] != a_id
    assert len(bodies) == 2
    assert json.loads(bodies[0]["messages"][1]["content"])["existing_entities"] == []
    assert "Ambiguous or cross-scope" in bodies[1]["messages"][-1]["content"]
    assert not engine.status()["pending_extraction"]


def test_offered_alias_can_reuse_approved_identity(engine, monkeypatch):
    text, draft = trip_draft("Gita di ottobre")
    saved = engine.remember(text, scope="test-mcp", extraction=draft)
    entity_id = engine.explain(saved["claim_ids"][0])["subject_id"]
    engine.identity_change(entity_id, kind="alias", alias="Gita autunnale", reason="Same event")
    alias_text, alias_draft = trip_draft("Gita autunnale", "Genova", entity_id)
    bodies = mocked_model(engine, monkeypatch, [alias_draft])
    result = engine.remember(alias_text, scope="test-mcp")
    assert engine.explain(result["claim_ids"][0])["subject_id"] == entity_id
    candidates = json.loads(bodies[0]["messages"][1]["content"])["existing_entities"]
    assert [e["name"] for e in candidates] == ["Gita autunnale"]


def test_local_identity_repair_failure_keeps_original_pending(engine, monkeypatch):
    text, bad = trip_draft("Gita B", existing_id=uid())
    mocked_model(engine, monkeypatch, [bad, bad])
    result = engine.remember(text)
    assert result["status"] == "stored_pending_extraction"
    assert result["claim_ids"] == []
    assert engine.read_source(result["source_id"])["text"] == text
    assert engine.status()["claims"] == 0


def test_indexed_identical_revision_covers_pending_without_reviving_corrected_facts(engine):
    pending = engine.remember("The trip goes to Palermo.")
    indexed = engine.remember(
        "The trip goes to Palermo.", source_id=pending["source_id"], extraction=extraction()
    )
    engine.change(
        indexed["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="Original was mistaken",
    )
    before = engine.status()
    old_key, new_key = f"{pending['source_id']}:1", f"{pending['source_id']}:2"
    assert old_key not in before["pending_extraction"]
    assert before["superseded_extraction"] == {old_key: new_key}
    retry = engine.extract_source(pending["source_id"], revision=1, extraction=extraction())
    assert retry["status"] == "superseded"
    assert retry["indexed_revision"] == 2
    assert retry["claim_ids"] == indexed["claim_ids"]
    assert engine.status()["events"] == before["events"]
    assert {c["value"] for c in engine.recall("trip")["memories"]} == {"Catania"}
    assert engine.read_source(pending["source_id"], 1)["text"] == "The trip goes to Palermo."
    with engine.vault.transaction() as events:
        rebuilt = State(events)
        incremental = engine._state(events)
        source = rebuilt.sources[old_key]
        assert rebuilt.extraction_cover(source).key == incremental.extraction_cover(source).key
        assert old_key not in rebuilt.extracted  # Covered is distinct from adopted.


def test_failed_inference_does_not_report_pending_after_another_writer_completed_revision(engine):
    class CompletingModel:
        def extract(self, text, source, entities):
            engine.remember(text, source_id=source.source_id, extraction=extraction())
            # The first extraction fails after the second writer has indexed revision 2.
            return Extraction.model_validate(extraction("Unsupported quote"))

        def embed(self, texts):
            return [[1.0] + [0.0] * (engine.settings.embedding_dimensions - 1) for _ in texts]

    engine.models = CompletingModel()
    result = engine.remember("The trip goes to Palermo.")
    assert result["status"] == "superseded"
    assert result["revision"] == 1
    assert result["indexed_revision"] == 2
    assert engine.status()["claims"] == 1
    assert not engine.status()["pending_extraction"]


@pytest.mark.parametrize("change", ["text", "observation", "title"])
def test_distinct_revision_does_not_hide_unprocessed_information(engine, change):
    pending = engine.remember("The trip goes to Palermo.")
    text = "The trip goes to Catania." if change == "text" else "The trip goes to Palermo."
    engine.remember(
        text,
        source_id=pending["source_id"],
        extraction=extraction(text),
        observed_at="2026-10-11T10:00:00Z" if change == "observation" else None,
        title="Different title" if change == "title" else "Note",
    )
    assert engine.status()["pending_extraction"] == [f"{pending['source_id']}:1"]
    assert not engine.status()["superseded_extraction"]


def test_entity_filter_isolates_similar_events_and_includes_object_links(engine):
    a_text, a = trip_draft("Gita A", "Torino")
    b_text, b = trip_draft("Gita B", "Genova")
    saved_a = engine.remember(a_text, extraction=a)
    saved_b = engine.remember(b_text, extraction=b)
    b_id = engine.explain(saved_b["claim_ids"][0])["subject_id"]
    assert len(engine.recall("gita")["memories"]) == 2
    assert {c["id"] for c in engine.recall("gita", entity_id=b_id)["memories"]} == set(
        saved_b["claim_ids"]
    )
    relation = {
        "entities": [
            {"ref": "person", "name": "Lia", "kind": "person"},
            {"ref": "trip", "name": "Gita B", "kind": "event", "existing_id": b_id},
        ],
        "claims": [
            {
                "subject_ref": "person",
                "object_ref": "trip",
                "predicate": "participation",
                "value": "Gita B",
                "statement": "Lia partecipa alla Gita B.",
                "quote": "Lia partecipa alla Gita B.",
            }
        ],
    }
    saved_relation = engine.remember("Lia partecipa alla Gita B.", extraction=relation)
    memories = engine.recall("gita", entity_id=b_id, scope="personal")["memories"]
    assert {c["id"] for c in memories} == set(saved_b["claim_ids"] + saved_relation["claim_ids"])
    assert saved_a["claim_ids"][0] not in {c["id"] for c in memories}
    with pytest.raises(ValueError, match="outside"):
        engine.recall("gita", scope="work", entity_id=b_id)
    with pytest.raises(ValueError, match="Unknown"):
        engine.recall("gita", entity_id=uid())


def test_entity_filter_respects_temporal_merges(engine, clock):
    text, draft = trip_draft("Gita")
    one = engine.remember(text, extraction=draft)
    first = engine.explain(one["claim_ids"][0])["subject_id"]
    clock.set("2026-10-12T11:00:00Z")
    two = engine.remember(text, extraction=draft)
    second = engine.explain(two["claim_ids"][0])["subject_id"]
    before_merge = clock().isoformat()
    clock.set("2026-10-12T12:00:00Z")
    engine.identity_change(second, kind="merge", target_id=first, reason="Reviewed same event")
    assert len(engine.recall("gita", entity_id=first)["memories"]) == 2
    assert len(engine.recall("gita", entity_id=first, known_at=before_merge)["memories"]) == 1
    assert len(engine.recall("gita", entity_id=second, valid_at=before_merge)["memories"]) == 1
    with pytest.raises(ValueError, match="not known"):
        engine.recall("gita", entity_id=second, known_at="2026-10-12T10:30:00Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_mcp_retry_uses_same_revision_and_exact_recall_filter(tmp_path, transport):
    async with transport_client(tmp_path, transport) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            assert "extract_source" in {t.name for t in (await session.list_tools()).tools}
            saved = (
                await session.call_tool("remember", {"text": "The trip goes to Palermo."})
            ).structuredContent
            assert saved["status"] == "stored_pending_extraction"
            args = {"source_id": saved["source_id"], "revision": 1, "extraction": extraction()}
            adopted_result = await session.call_tool("extract_source", args)
            again_result = await session.call_tool("extract_source", args)
            assert not adopted_result.isError, adopted_result
            assert not again_result.isError, again_result
            adopted, again = adopted_result.structuredContent, again_result.structuredContent
            assert adopted["claim_ids"] == again["claim_ids"]
            status = (await session.call_tool("memory_status", {})).structuredContent
            assert status["sources"] == status["claims"] == 1
            assert not status["pending_extraction"]
            found = (await session.call_tool("find_entities", {"query": "Trip"})).structuredContent
            result = (
                await session.call_tool(
                    "recall",
                    {
                        "query": "trip",
                        "entity_id": found["entities"][0]["id"],
                    },
                )
            ).structuredContent
            assert result["memories"][0]["id"] == adopted["claim_ids"][0]


@pytest.mark.integration
def test_live_graph_filters_seeds_and_neighbors(live_graph_engine):
    engine = live_graph_engine
    results = []
    for name in ("Gita A", "Gita B"):
        text, draft = trip_draft(name)
        results.append(engine.remember(text, extraction=draft))
    b = results[1]
    b_id = engine.explain(b["claim_ids"][0])["subject_id"]
    assert not engine.recall("gita", entity_id=b_id)["warnings"]
    scores = engine.graph.search("gita", [1.0, 0.0, 0.0], "personal", claim_ids=b["claim_ids"])
    assert set(scores) == set(b["claim_ids"])
    assert engine.graph.search("gita", None, "personal", claim_ids=[]) == {}


@pytest.mark.integration
def test_live_ollama_does_not_borrow_test_a_id(tmp_path):
    if os.getenv("SB_TEST_OLLAMA") != "1":
        pytest.skip("Set SB_TEST_OLLAMA=1 to run against local Ollama")
    engine = MemoryEngine(Settings(vault_path=tmp_path / "live-identities"), graph=None)
    a_text, a = trip_draft("Gita Test MCP 20261013-A")
    a_result = engine.remember(a_text, scope="test-mcp", extraction=a)
    a_id = engine.explain(a_result["claim_ids"][0])["subject_id"]
    b_name = "Gita Test MCP 20261013-B"
    b_text, _ = trip_draft(b_name)
    b_text += f"\nLia Test 20261013-B partecipa alla {b_name}."
    result = engine.remember(b_text, scope="test-mcp", observed_at="2026-10-13T09:00:00+02:00")
    assert result["status"] != "stored_pending_extraction", result
    assert len(result["claim_ids"]) >= 2, result
    claims = [engine.explain(i) for i in result["claim_ids"]]
    assert all(c["subject_id"] != a_id and c["object_id"] != a_id for c in claims)
    assert any(c["value"] == "Torino" for c in claims)
    assert any("Lia" in c["statement"] for c in claims)
    assert engine.find_entities(b_name, scope="test-mcp")["entities"]


@pytest.mark.asyncio
async def test_rate_window_retry_delay_and_rejections_do_not_extend_it(monkeypatch):
    tick = [0.0]
    monkeypatch.setattr("second_brain.auth.time", SimpleNamespace(monotonic=lambda: tick[0]))

    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    limits = RequestLimits(app, per_minute=2, max_inflight=8)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=limits), base_url="http://localhost"
    ) as client:
        assert (await client.post("/mcp")).status_code == 200
        tick[0] = 10
        assert (await client.post("/mcp")).status_code == 200
        tick[0] = 45.2
        denied = await client.post("/mcp")
        assert denied.status_code == 429
        assert denied.json()["error"] == "rate_limit_exceeded"
        assert denied.headers["Retry-After"] == "15"
        tick[0] = 60
        assert (await client.post("/mcp")).status_code == 200
        assert len(next(iter(limits.clients.values()))) == 2


@pytest.mark.asyncio
async def test_concurrency_rejection_has_short_delay_and_preserves_rate_capacity():
    started, release = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        started.set()
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    limits = RequestLimits(app, per_minute=2, max_inflight=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=limits), base_url="http://localhost"
    ) as client:
        running = asyncio.create_task(client.post("/mcp"))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            rejected = await client.post("/mcp")
            assert rejected.json()["error"] == "concurrency_limit_exceeded"
            assert rejected.headers["Retry-After"] == "1"
        finally:
            release.set()
            await running
        assert (await client.post("/mcp")).status_code == 200
        assert (await client.post("/mcp")).json()["error"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_anonymous_and_reader_requests_do_not_spend_owner_rate_budget():
    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    tokens = LocalTokens(
        Settings(_env_file=None, mcp_write_token=WRITE_TOKEN, mcp_read_token=READ_TOKEN)
    )
    limits = RequestLimits(app, per_minute=1, max_inflight=8, token_verifier=tokens)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=limits), base_url="http://localhost"
    ) as client:
        assert (await client.post("/mcp")).status_code == 200
        assert (await client.post("/mcp")).status_code == 429
        assert (
            await client.post("/mcp", headers={"Authorization": f"Bearer {READ_TOKEN}"})
        ).status_code == 200
        assert (
            await client.post("/mcp", headers={"Authorization": f"Bearer {WRITE_TOKEN}"})
        ).status_code == 200
