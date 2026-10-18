"""Regression cases reproduced during the post-merge memory audit."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from second_brain.jobs import JobQueue, Work
from second_brain.models import instant
from second_brain.ollama import Ollama
from second_brain.server import create_server
from second_brain.state import State
from second_brain.worker import RecoveryWorker

from .conftest import LocalVectors, extraction
from .test_worker import Extractor


@pytest.fixture
def automatic(engine, monkeypatch):
    engine.settings.embedding_dimensions = 3
    engine.models = Ollama(engine.settings)
    tag = engine.settings.embedding_model
    tag = tag if ":" in tag else tag + ":latest"
    monkeypatch.setattr(engine.models, "status", lambda: {"model_digests": {tag: "weights-v1"}})

    def ingest(text, data):
        def post(endpoint, body):
            if endpoint == "/api/chat":
                return {"message": {"content": json.dumps(data)}}
            return {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]}

        monkeypatch.setattr(engine.models, "_post", post)
        return engine.remember(text, scope="test")

    return ingest


def literal(text, subject, predicate, value, polarity="positive"):
    return {
        "entities": [{"ref": "s", "name": subject, "kind": "entity"}],
        "claims": [
            {
                "subject_ref": "s",
                "predicate": predicate,
                "value": value,
                "quote": text,
                "statement": text,
                "epistemic": "explicit",
                "polarity": polarity,
            }
        ],
    }


PERIOD = (
    "From 1 april 2027 at 10:00 (UTC+02:00) to 1 may 2027 at 10:00 "
    "(UTC+02:00), Nora Bell works for Cedar Labs."
)
PREFERENCE = "Nora Bell prefers green tea."
BOUNDS = {"valid_from": "2027-04-01T08:00:00Z", "valid_to": "2027-05-01T08:00:00Z"}


def temporal_draft(wrong=False):
    return {
        "entities": [
            {"ref": "n", "name": "Nora Bell", "kind": "person"},
            {"ref": "c", "name": "Cedar Labs", "kind": "organization"},
            {"ref": "t", "name": "green tea", "kind": "entity"},
        ],
        "claims": [
            {
                "subject_ref": "n",
                "predicate": "works_for",
                "value": "Cedar Labs",
                "object_ref": "c",
                "quote": PERIOD,
                "statement": PERIOD,
                **BOUNDS,
                "epistemic": "explicit",
                "polarity": "positive",
            },
            {
                "subject_ref": "n",
                "predicate": "prefers",
                "value": "green tea",
                "object_ref": "t",
                "quote": PREFERENCE,
                "statement": PREFERENCE,
                "epistemic": "explicit",
                "polarity": "positive",
                **(BOUNDS if wrong else {}),
            },
        ],
    }


def test_effective_interval_belongs_only_to_its_own_fact(engine, automatic, clock):
    result = automatic(PERIOD + " " + PREFERENCE, temporal_draft())
    assert len(result["claim_ids"]) == 2
    claims = {engine.explain(i)["predicate"]: engine.explain(i) for i in result["claim_ids"]}
    assert claims["works_for"]["valid_to"] is not None
    assert claims["prefers"]["valid_to"] is None
    assert instant(claims["prefers"]["valid_from"]) == clock()


@pytest.mark.parametrize("whole_note_quote", [False, True])
def test_other_fact_dates_never_license_wrong_bounds(engine, automatic, whole_note_quote):
    text = PERIOD + " " + PREFERENCE
    data = temporal_draft(wrong=True)
    if whole_note_quote:
        data["claims"][1]["quote"] = text
    result = automatic(text, data)
    assert result["status"] == "stored_pending_extraction"
    assert result["failure"] == {
        "stage": "extraction",
        "code": "temporal_rejected",
        "retryable": False,
    }
    assert not result["claim_ids"]
    assert engine.read_source(result["source_id"])["text"] == text


def test_same_subject_two_different_intervals_are_independent(engine, automatic):
    other = (
        "From 1 june 2027 at 10:00 (UTC+02:00) to 1 july 2027 at 10:00 "
        "(UTC+02:00), Nora Bell works for Maple Labs."
    )
    data = temporal_draft()
    data["entities"][2] = {"ref": "m", "name": "Maple Labs", "kind": "organization"}
    data["claims"][1] = {
        **data["claims"][0],
        "value": "Maple Labs",
        "object_ref": "m",
        "quote": other,
        "statement": other,
        "valid_from": "2027-06-01T08:00Z",
        "valid_to": "2027-07-01T08:00Z",
    }
    assert len(automatic(PERIOD + " " + other, data)["claim_ids"]) == 2


@pytest.mark.parametrize(
    "value",
    [
        "2028-02-18T17:15:00Z",
        "18 february 2028 at 18:15 (UTC+01:00)",
    ],
)
def test_equivalent_deadlines_do_not_conflict_in_ingest_recall_or_owl(engine, automatic, value):
    first_value = "18 febbraio 2028 alle 18:15 (UTC+01:00)"
    for v in [first_value, value]:
        text = f"Task Library is due on {v}."
        result = automatic(text, literal(text, "Task Library", "deadline", v))
        assert result["claim_ids"] and not result["conflicts"]
    rows = engine.recall("Task Library", scope="test")["memories"]
    assert len(rows) == 2 and all(not r["conflicts"] for r in rows)
    assert {r["value"] for r in rows} == {first_value, value}
    assert not any("single-value" in w for w in engine.reason_memory("test")["warnings"])


@pytest.mark.parametrize(
    "negative_value,conflict",
    [
        ("47.25 euro al mese", True),
        ("47.25 euro all'anno", False),
        ("47.25 dollari al mese", False),
        ("-47.25 euro al mese", False),
    ],
)
def test_price_comparison_preserves_currency_recurrence_and_sign(
    engine, automatic, negative_value, conflict
):
    text = "Il costo del Piano Studio è 47,25 euro al mese."
    a = automatic(text, literal(text, "Piano Studio", "price", "47,25 euro al mese"))
    text = f"Il costo del Piano Studio non è {negative_value}."
    b = automatic(text, literal(text, "Piano Studio", "price", negative_value, "negative"))
    assert a["claim_ids"] and b["claim_ids"]
    assert bool(b["conflicts"]) == conflict
    assert all(bool(r["conflicts"]) == conflict for r in engine.recall("Piano Studio")["memories"])


def test_context_filter_precedes_semantic_claim_limit(engine):
    engine.semantics.max_claims = 1
    for context in ["one", "two"]:
        engine.remember(
            "The trip goes to Palermo.", scope="test", extraction=extraction(context=context)
        )
    assert engine.reason_memory("test", context="one")["scope"] == "test"
    _, projection = engine._semantic_snapshot("test", context="one")
    assert len(projection["claims"]) == 1
    with pytest.raises(ValueError, match="exceeds 1"):
        engine.reason_memory("test")


def test_background_ingestion_survives_reopen_and_never_duplicates(engine, tmp_path):
    engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
    engine.settings.embedding_dimensions = 3
    engine.models = Extractor()
    text = "The trip goes to Palermo."
    first = engine.remember(text, request_id="async-one", background=True)
    again = engine.remember(text, request_id="async-one", background=True)
    assert first == again and first["status"] == "queued"
    assert engine.models.calls == 0
    assert engine.read_source(first["source_id"])["text"] == text
    assert engine.source_status(first["source_id"])["job"]["status"] == "pending"
    engine._queue = JobQueue(engine.settings.worker_queue_path)
    for _ in range(4):
        RecoveryWorker(engine).run_once()
    final = engine.source_status(first["source_id"])
    assert len(final["claim_ids"]) == 1 and engine.models.calls == 1
    assert (
        engine.remember(text, request_id="async-one", background=True)["claim_ids"]
        == final["claim_ids"]
    )
    assert engine.status()["sources"] == engine.status()["claims"] == 1


def test_background_enqueue_preserves_other_jobs_and_requires_worker(engine, tmp_path):
    with pytest.raises(ValueError, match="requires"):
        engine.remember("The trip goes to Palermo.", background=True)
    assert engine.status()["sources"] == 0
    engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
    for i in range(2):
        engine.remember(f"The trip goes to Palermo {i}.", background=True)
    jobs = engine.recovery_jobs()["jobs"]
    assert len(jobs) == 2 and all(j["status"] == "pending" for j in jobs)


def test_background_retry_does_not_hide_needs_review(engine, tmp_path):
    engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
    saved = engine.remember("The trip goes to Palermo.", background=True, request_id="review")
    queue = engine.job_queue()
    job = queue.claim(engine.vault.id, lease_seconds=60, max_attempts=1)
    queue.finish(job, error="validation_rejected", max_attempts=1)
    again = engine.remember("The trip goes to Palermo.", background=True, request_id="review")
    assert again["source_id"] == saved["source_id"] and again["status"] == "needs_review"
    assert again["job"]["attempts"] == 1


@pytest.mark.asyncio
async def test_background_remember_and_source_status_cross_mcp_boundary(engine, tmp_path):
    engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
    server = create_server(engine)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["source_status"].annotations.readOnlyHint
    assert "background" in tools["remember"].inputSchema["properties"]
    _, saved = await server.call_tool(
        "remember",
        {
            "text": "The trip goes to Palermo.",
            "background": True,
            "request_id": "mcp-async",
        },
    )
    assert saved["status"] == "queued"
    _, status = await server.call_tool("source_status", {"source_id": saved["source_id"]})
    assert status["job"]["job_id"] == saved["job_id"]
    assert status["job"]["status"] == "pending"


@pytest.mark.parametrize("transient", [False, True])
def test_worker_persists_safe_stage_reason_and_retry_policy(engine, tmp_path, transient):
    engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
    engine.settings.embedding_dimensions = 3

    class Bad(LocalVectors):
        def extract(self, *args):
            if transient:
                raise httpx.ReadTimeout("private source text and credentials")
            raise ValueError("private source text and credentials")

    engine.models = Bad()
    saved = engine.remember("The trip goes to Palermo.", background=True)
    worker = RecoveryWorker(engine)
    worker.run_once()
    result = engine.source_status(saved["source_id"])
    assert result["job"]["status"] == ("retry_wait" if transient else "needs_review")
    assert result["job"]["failure"] == {
        "stage": "extraction",
        "code": "dependency_timeout" if transient else "validation_rejected",
        "retryable": transient,
    }
    assert "private" not in json.dumps(engine.recovery_jobs())
    assert result["job"]["attempts"] == 1


@pytest.mark.parametrize("through_worker", [False, True])
def test_embedding_digest_change_repairs_vectors_without_reextracting(
    engine, tmp_path, monkeypatch, through_worker
):
    engine.settings.embedding_dimensions = 3
    engine.models = Ollama(engine.settings)
    digest = ["old-weights"]
    name = engine.settings.embedding_model + ":latest"
    monkeypatch.setattr(engine.models, "status", lambda: {"model_digests": {name: digest[0]}})
    monkeypatch.setattr(
        engine.models,
        "_post",
        lambda path, body: {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]},
    )
    saved = engine.remember("The trip goes to Palermo.", extraction=extraction())
    claim_id = saved["claim_ids"][0]
    before = engine.explain(claim_id)
    assert before["embedding_profile"] is not None
    digest[0] = "new-weights"
    assert engine.status()["embeddings_incompatible"] == 1
    if through_worker:
        engine.settings.worker_queue_path = tmp_path / "queue.sqlite3"
        for _ in range(4):
            RecoveryWorker(engine).run_once()
    else:
        engine.repair_embedding(claim_id)
    after = engine.explain(claim_id)
    assert after["embedding_profile"] != before["embedding_profile"]
    assert engine.status()["embeddings_incompatible"] == 0
    assert after["evidence"] == before["evidence"]
    assert engine.status()["sources"] == engine.status()["claims"] == 1
    reopened = State(engine.vault.events())
    assert reopened.claims[claim_id].embedding_profile == after["embedding_profile"]


@pytest.mark.integration
def test_live_neo4j_indexes_keep_vector_profiles_separate(live_graph_engine):
    from second_brain.embeddings import profile_id

    engine = live_graph_engine

    class MutableVectors(LocalVectors):
        digest = "before"

        def embedding_identity(self):
            return profile_id(engine.settings.embedding_model, 3, self.digest)

    engine.models = MutableVectors()
    a = engine.remember("The trip goes to Palermo.", extraction=extraction())
    assert not a["warnings"]
    first_profile = engine.embedding_profile
    engine.models.digest = "after"
    b = engine.remember(
        "The trip goes to Catania.",
        extraction=extraction(
            "The trip goes to Catania.",
            value="Catania",
            context="other",
        ),
    )
    assert not b["warnings"]
    second_profile = engine.embedding_profile
    assert first_profile != second_profile
    rows = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault}) RETURN a.claim_id AS id, "
        "a.embedding_profile AS profile, labels(a) AS labels",
        vault=engine.vault.id,
    )
    assert len(rows) == 2
    for row in rows:
        if row["profile"] == first_profile:
            assert f"sb_vector_{second_profile[:12]}" not in row["labels"]
        else:
            assert f"sb_vector_{second_profile[:12]}" in row["labels"]
    engine.repair_embedding(a["claim_ids"][0])
    engine.replay()
    rows = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault}) RETURN a.embedding_profile AS profile",
        vault=engine.vault.id,
    )
    assert {r["profile"] for r in rows} == {second_profile}


def test_model_swap_during_embedding_cannot_adopt_mixed_vectors(engine, monkeypatch):
    engine.settings.embedding_dimensions = 3
    engine.models = Ollama(engine.settings)
    identities = iter(["profile-one", "profile-two"])
    monkeypatch.setattr(engine.models, "embedding_identity", lambda: next(identities))
    monkeypatch.setattr(
        engine.models,
        "_post",
        lambda path, body: {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]},
    )
    result = engine.remember("The trip goes to Palermo.", extraction=extraction())
    claim = engine._state(engine.vault.events()).claims[result["claim_ids"][0]]
    assert not claim.embedding and claim.embedding_profile is None
    assert any("model_changed_during_embedding" in w for w in result["warnings"])


def test_legacy_queue_migration_preserves_jobs(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    queue = JobQueue(path)
    queue.reconcile("vault", [Work("extraction", "one", "generation", {})])
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE jobs DROP COLUMN failure")
    reopened = JobQueue(path)
    assert len(reopened.list("vault")["jobs"]) == 1
    assert reopened.list("vault")["jobs"][0]["failure"] is None


@pytest.mark.parametrize("timeout,expected", [(300, 1920), (600, 3720), (10, 600)])
def test_generated_mcp_timeout_matches_model_budget(tmp_path, monkeypatch, timeout, expected):
    path = Path(__file__).parents[1] / "scripts" / "mcp_config.py"
    spec = importlib.util.spec_from_file_location("mcp_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("SB_MODEL_TIMEOUT", raising=False)
    (tmp_path / ".env").write_text(f"SB_MODEL_TIMEOUT={timeout}\n")
    assert module.client_timeout(tmp_path) == expected
