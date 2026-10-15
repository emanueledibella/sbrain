import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from second_brain.engine import MemoryEngine
from second_brain.models import Entity, instant
from second_brain.state import State
from second_brain.store import CorruptVault, Vault

from .conftest import extraction


def relation(engine):
    text = "Marco is joining the trip."
    return engine.remember(
        text,
        extraction={
            "entities": [
                {"ref": "m", "name": "Marco", "kind": "person"},
                {"ref": "t", "name": "Trip", "kind": "event"},
            ],
            "claims": [
                {
                    "subject_ref": "m",
                    "object_ref": "t",
                    "predicate": "participation",
                    "value": "yes",
                    "statement": text,
                    "quote": text,
                }
            ],
        },
    )


@pytest.mark.parametrize("kind", ["correction", "update"])
def test_changes_preserve_object_identity(engine, kind, clock):
    saved = relation(engine)
    original = engine.explain(saved["claim_ids"][0])
    clock.set("2026-10-13T10:00:00Z")
    changed = engine.change(
        original["id"],
        kind=kind,
        value="no",
        statement="Marco is not joining the trip.",
        reason="Explicit update",
        effective_at="2026-10-13T10:00:00Z" if kind == "update" else None,
    )
    replacement = engine.explain(changed["replacement_id"])
    assert replacement["object_id"] == original["object_id"]
    assert replacement["object"]["name"] == "Trip"
    assert replacement["provenance"]["origin"] == "user_change"


def test_private_permissions_repair_existing_storage(tmp_path):
    root = tmp_path / "private"
    (root / "episodes").mkdir(parents=True, mode=0o755)
    old = root / "episodes" / "old.md"
    old.write_text("private content")
    old.chmod(0o644)
    vault = Vault(root)
    for path in [root, *root.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert old.read_text() == "private content"
    assert vault.id


def test_vault_rejects_symlinks_without_touching_target(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    external = tmp_path / "external"
    external.write_text("outside")
    external.chmod(0o644)
    (root / "linked").symlink_to(external)
    with pytest.raises(CorruptVault, match="link"):
        Vault(root)
    assert external.stat().st_mode & 0o777 == 0o644
    assert external.read_text() == "outside"


def test_source_symlink_is_rejected_after_startup(engine, memory, tmp_path):
    source = engine.read_source(memory["source_id"])
    original = engine.vault.root / source["path"]
    external = tmp_path / "copy.md"
    external.write_text(source["text"])
    original.unlink()
    original.symlink_to(external)
    with pytest.raises(CorruptVault, match="Symlinks"):
        engine.read_source(memory["source_id"])


def test_retry_checks_explicit_observed_time_and_normalizes_timezone(engine):
    text = "The trip goes to Palermo."
    original = engine.remember(
        text, observed_at="2026-10-01T10:00:00Z", request_id="time", extraction=extraction()
    )
    assert (
        engine.remember(text, observed_at="2026-10-01T12:00:00+02:00", request_id="time")[
            "claim_ids"
        ]
        == original["claim_ids"]
    )
    with pytest.raises(ValueError, match="different source"):
        engine.remember(text, observed_at="2026-10-02T10:00:00Z", request_id="time")


def test_episode_failure_reports_durable_source_and_adopted_claims(engine, monkeypatch):
    import second_brain.store as store

    original = store.atomic_write

    def fail_episode(path, data):
        if path.parent.name == "episodes":
            raise OSError("simulated episode failure")
        return original(path, data)

    monkeypatch.setattr(store, "atomic_write", fail_episode)
    result = engine.remember(
        "The trip goes to Palermo.", extraction=extraction(), request_id="view"
    )
    assert result["status"] == "stored_with_warnings"
    assert result["claim_ids"]
    assert engine.read_source(result["source_id"])["text"] == "The trip goes to Palermo."
    assert engine.status()["pending_extraction"] == []
    monkeypatch.setattr(store, "atomic_write", original)
    retried = engine.remember(
        "The trip goes to Palermo.", extraction=extraction(), request_id="view"
    )
    assert retried["claim_ids"] == result["claim_ids"]
    assert (engine.vault.root / "episodes" / f"{result['source_id']}.md").exists()


class SlowVectors:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()

    def embed(self, texts):
        self.started.set()
        assert self.release.wait(5)
        return [[1, 0, 0] for _ in texts]


@pytest.mark.parametrize("operation", ["change", "reembed"])
def test_model_io_does_not_hold_writer_lock(engine, memory, operation):
    engine.settings.embedding_dimensions = 3
    engine.models = models = SlowVectors()
    engine.status = lambda: None  # Inspect canonical state without invoking fake model status.
    with ThreadPoolExecutor(2) as pool:
        if operation == "change":
            pending = pool.submit(
                engine.change,
                memory["claim_ids"][0],
                kind="correction",
                value="Catania",
                statement="Catania",
                reason="Correction",
            )
        else:
            pending = pool.submit(engine.reembed)
        assert models.started.wait(2)
        try:
            read = pool.submit(engine.read_source, memory["source_id"])
            assert read.result(timeout=1)["text"] == "The trip goes to Palermo."
        finally:
            models.release.set()
        assert pending.result()["warnings"] == ["Neo4j disabled; using canonical lexical retrieval"]


def test_concurrent_change_is_revalidated_after_embedding(engine, memory):
    engine.settings.embedding_dimensions = 3
    engine.models = models = SlowVectors()
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(
            engine.change,
            memory["claim_ids"][0],
            kind="correction",
            value="Catania",
            statement="Catania",
            reason="Slow change",
        )
        assert models.started.wait(2)
        try:
            engine.change(memory["claim_ids"][0], kind="retraction", reason="Concurrent retraction")
        finally:
            models.release.set()
        with pytest.raises(ValueError, match="concurrently"):
            pending.result()
    assert engine.status()["claims"] == 1


def test_state_cache_reuses_head_and_keeps_old_snapshots_immutable(engine, memory):
    with engine.vault.transaction() as events:
        before = engine._state(events)
        assert engine._state(events) is before
    engine.settings.embedding_dimensions = 3
    engine.models = type("Vectors", (), {"embed": lambda self, texts: [[1, 0, 0] for _ in texts]})()
    engine.reembed()
    with engine.vault.transaction() as events:
        after = engine._state(events)
    claim_id = memory["claim_ids"][0]
    assert before.claims[claim_id].embedding == []
    assert after.claims[claim_id].embedding == [1, 0, 0]
    assert State(events).claims[claim_id] == after.claims[claim_id]


def test_cache_observes_another_engine_and_checks_modified_prefix(engine, memory):
    other = MemoryEngine(engine.settings, graph=None, models=None, clock=engine.clock)
    other.remember("The trip goes to Catania.", extraction=extraction("The trip goes to Catania."))
    assert engine.status()["claims"] == 2
    content = engine.vault.log.read_text().replace("Palermo", "Catania")
    engine.vault.log.write_text(content)
    with pytest.raises(CorruptVault):
        engine.status()


def test_recall_verifies_only_selected_sources_and_caches_shared_evidence(engine, monkeypatch):
    for index in range(20):
        engine.remember(
            "The trip goes to Palermo.", title=f"Source {index}", extraction=extraction()
        )
    calls = []
    original = engine.vault.read
    monkeypatch.setattr(
        engine.vault, "read", lambda source: (calls.append(source.key), original(source))[1]
    )
    result = engine.recall("trip", limit=1)
    assert len(result["memories"]) == 1 and result["truncated"]
    assert len(calls) == 1


def test_only_affected_profiles_are_rewritten(engine, memory):
    entity = engine.explain(memory["claim_ids"][0])["subject_id"]
    path = engine.vault.root / "entities" / f"{entity}.md"
    old = path.stat().st_mtime_ns
    engine.remember("The trip goes to Palermo.", extraction=extraction())  # a distinct identity
    assert path.stat().st_mtime_ns == old


def test_aliases_merges_and_conflicts_are_temporal_and_auditable(engine, clock):
    first = engine.remember("The trip goes to Palermo.", extraction=extraction())
    second = engine.remember(
        "The trip goes to Catania.",
        extraction=extraction("The trip goes to Catania.", value="Catania"),
    )
    a = engine.explain(first["claim_ids"][0])["subject_id"]
    b = engine.explain(second["claim_ids"][0])["subject_id"]
    clock.set("2026-10-13T10:00:00Z")
    alias = engine.identity_change(
        a, kind="alias", alias="Autumn holiday", reason="User's name", request_id="alias"
    )
    assert (
        engine.identity_change(
            a, kind="alias", alias="Autumn holiday", reason="User's name", request_id="alias"
        )
        == alias
    )
    assert len(engine.find_entities("Autumn")["entities"]) == 1
    assert not engine.find_entities("Autumn", known_at="2026-10-12T10:00:00Z")["entities"]
    assert not engine.recall("Autumn", known_at="2026-10-12T10:00:00Z")["memories"]
    merged = engine.identity_change(
        b,
        kind="merge",
        target_id=a,
        reason="User confirmed same trip",
        effective_at="2026-10-12T10:00:00Z",
    )
    result = engine.recall("Autumn")
    assert len(result["memories"]) == 2
    assert all(m["subject_identity_id"] == a and m["conflicts"] for m in result["memories"])
    with pytest.raises(ValueError, match="canonical|already"):
        engine.identity_change(a, kind="merge", target_id=b, reason="Must not form a cycle")
    assert engine.read_source(merged["source_id"])["kind"] == "identity_change"
    assert engine.status()["pending_extraction"] == []
    assert len(engine.history()["identity_changes"]) == 2


def test_merge_rejects_cross_scope_and_preserves_original_entity_ids(engine, memory):
    a = engine.explain(memory["claim_ids"][0])["subject_id"]
    other = engine.remember("The trip goes to Palermo.", scope="work", extraction=extraction())
    b = engine.explain(other["claim_ids"][0])["subject_id"]
    with pytest.raises(ValueError, match="same scope"):
        engine.identity_change(b, kind="merge", target_id=a, reason="Invalid merge")
    assert engine.explain(other["claim_ids"][0])["subject_id"] == b


def test_extraction_provenance_is_recorded(engine, memory):
    result = engine.explain(memory["claim_ids"][0])
    assert result["provenance"] == {
        "origin": "agent/manual",
        "extractor_version": 2,
        "vocabulary_sha256": engine.vocabulary.digest,
        "validation_shapes_sha256": engine.semantics.shapes_digest,
        "source_sha256": hashlib.sha256(b"The trip goes to Palermo.").hexdigest(),
    }


@pytest.mark.integration
def test_graph_ignores_future_and_uncertain_context(live_graph_engine, clock):
    engine = live_graph_engine
    saved = relation(engine)
    event_id = engine.explain(saved["claim_ids"][0])["object_id"]
    engine.models = None
    past = instant("2026-10-12T10:00:00Z")
    assert not engine.recall("tesseract", valid_at=past, known_at=past)["memories"]
    clock.set("2026-10-13T10:00:00Z")
    draft = extraction("Tesseract is the trip codename.", existing_id=event_id, value="Tesseract")
    draft["claims"][0]["predicate"] = "codename"
    engine.remember("Tesseract is the trip codename.", extraction=draft)
    assert not engine.recall("tesseract", valid_at=past, known_at=past)["memories"]
    assert not engine.graph.search("tesseract", None, None, valid_at=past, known_at=past)
    assert not engine.recall("tesseract", valid_at=past)["memories"]
    assert not engine.recall("tesseract", known_at=past)["memories"]
    assert engine.recall("tesseract")["memories"]
    proposed = extraction(
        "Hyperion might be the trip codename.",
        existing_id=event_id,
        value="Hyperion",
        epistemic="proposed",
    )
    engine.remember("Hyperion might be the trip codename.", extraction=proposed)
    assert not engine.graph.search("hyperion", None, None)
    assert not engine.recall("hyperion")["memories"]
    assert engine.recall("hyperion", include_uncertain=True)["memories"]


@pytest.mark.integration
def test_graph_incremental_projection_keeps_node_identity_and_rejects_stale_snapshot(
    live_graph_engine, clock
):
    engine = live_graph_engine
    saved = engine.remember("The trip goes to Palermo.", extraction=extraction())
    rows = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault}) RETURN elementId(a) AS id", vault=engine.vault.id
    )
    with engine.vault.transaction() as events:
        old = State(events)
    clock.set("2026-10-13T10:00:00Z")
    engine.change(
        saved["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="Catania",
        reason="Correction",
    )
    engine.graph.sync(old, force=True)
    current = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault}) RETURN elementId(a) AS id", vault=engine.vault.id
    )
    assert rows[0]["id"] in {row["id"] for row in current}
    assert len(current) == 3
    with engine.vault.transaction() as events:
        state = engine._state(events)
    with patch.object(
        Entity,
        "model_dump",
        side_effect=AssertionError("Unchanged projection must not serialize entities"),
    ):
        engine.graph.sync(state)


def test_negated_relation_conflicts_with_affirmed_relation(engine):
    saved = relation(engine)
    subject = engine.explain(saved["claim_ids"][0])["subject_id"]
    text = "Marco is not joining the trip."
    draft = {
        "entities": [{"ref": "m", "name": "Marco", "kind": "person", "existing_id": subject}],
        "claims": [
            {
                "subject_ref": "m",
                "predicate": "participation",
                "value": "yes",
                "polarity": "negative",
                "statement": text,
                "quote": text,
            }
        ],
    }
    result = engine.remember(text, extraction=draft)
    assert result["conflicts"]
    assert all(m["conflicts"] for m in engine.recall("Marco")["memories"])


def test_local_semantic_guard_rejects_dropped_negation_or_proposal():
    from second_brain.models import Extraction
    from second_brain.validation import validate_extraction

    for text, error in (
        ("Marco is not joining the trip.", "negation"),
        ("We might choose Palermo.", "tentative"),
        ("Forse sceglieremo Palermo.", "tentative"),
    ):
        draft = extraction(text)
        # Exact quote alone must not legitimize an overconfident interpretation.
        draft["claims"][0]["statement"] = "The trip goes to Palermo."
        with pytest.raises(ValueError, match=error):
            validate_extraction(Extraction.model_validate(draft), text, semantic_checks=True)


def test_semantic_evaluation_does_not_adopt_memories(engine):
    from second_brain.evaluation import CASES, evaluate_models
    from second_brain.models import Extraction

    class FixtureModel:
        def extract(self, text, source, entities):
            case = next(c for c in CASES if c["text"] == text)
            draft = extraction(text, epistemic=case["epistemic"])
            if case["id"].startswith("negation"):
                draft["claims"][0]["polarity"] = "negative"
            return Extraction.model_validate(draft)

    engine.models = FixtureModel()
    report = evaluate_models(engine)
    assert report["passed"] == report["total"] == 6
    assert engine.status()["events"] == 0


def test_oversized_results_do_not_read_unreturned_sources(engine, monkeypatch):
    for index in range(10):
        text = "The trip goes to Palermo. " + ("details " * 180)
        engine.remember(text, title=str(index), extraction=extraction(text))
    reads = []
    original = engine.vault.read
    monkeypatch.setattr(engine.vault, "read", lambda s: (reads.append(s.key), original(s))[1])
    result = engine.recall("trip", limit=1, token_budget=256)
    assert result["truncated"] and not result["memories"]
    assert not reads


def test_shared_source_is_hash_verified_once_per_response(engine, monkeypatch):
    text = "The trip goes to Palermo. Marco is joining the trip."
    draft = extraction("The trip goes to Palermo.")
    draft["claims"].append(
        {
            "subject_ref": "trip",
            "predicate": "participation",
            "value": "Marco",
            "statement": "Marco is joining the trip.",
            "quote": "Marco is joining the trip.",
        }
    )
    engine.remember(text, extraction=draft)
    reads = []
    original = engine.vault.read
    monkeypatch.setattr(engine.vault, "read", lambda s: (reads.append(s.key), original(s))[1])
    assert len(engine.recall("trip")["memories"]) == 2
    assert len(reads) == 1
    engine.recall("trip")
    assert len(reads) == 2  # a subsequent request must verify again


def test_slow_graph_projection_does_not_block_canonical_reads(engine):
    started, release = threading.Event(), threading.Event()

    class SlowGraph:
        def sync(self, *args, **kwargs):
            started.set()
            assert release.wait(5)

    engine.graph = SlowGraph()
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(engine.remember, "The trip goes to Palermo.", extraction=extraction())
        assert started.wait(2)
        try:

            def read():
                with engine.vault.transaction() as events:
                    return len(events)

            assert pool.submit(read).result(timeout=1) == 2
        finally:
            release.set()
        assert pending.result()["claim_ids"]


@pytest.mark.integration
def test_explicit_replay_restores_projection_from_restored_canonical_backup(
    live_graph_engine, clock
):
    engine = live_graph_engine
    engine.remember("The trip goes to Palermo.", extraction=extraction())
    backup = engine.vault.log.read_bytes()
    clock.set("2026-10-13T10:00:00Z")
    engine.remember("The trip goes to Catania.", extraction=extraction("The trip goes to Catania."))
    with engine.vault.transaction():
        engine.vault.log.write_bytes(backup)
    assert not engine.replay()["warnings"]
    rows = engine.graph.query(
        "MATCH (a:Assertion {vault_id:$vault}) RETURN count(a) AS n", vault=engine.vault.id
    )
    assert rows[0]["n"] == 1
