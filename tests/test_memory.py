import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from second_brain.engine import MemoryEngine
from second_brain.state import State
from second_brain.store import CorruptVault

from .conftest import extraction


def values(result):
    return {c["value"] for c in result["memories"]}


def test_original_evidence_and_revision_survive_edits(engine, memory):
    source_id = memory["source_id"]
    edited = "La gita va a Catania."
    engine.remember(edited, source_id=source_id, extraction=extraction(edited, value="Catania"))
    assert engine.read_source(source_id, 1)["text"] == "La gita va a Palermo."
    assert engine.read_source(source_id, 2)["text"] == edited
    evidence = engine.explain(memory["claim_ids"][0])["evidence"][0]
    assert evidence["revision"] == 1
    assert evidence["quote"] == "La gita va a Palermo."


def test_correction_has_two_time_axes(engine, memory, clock):
    original = memory["claim_ids"][0]
    clock.set("2026-10-14T10:00:00Z")
    correction = engine.change(
        original,
        kind="correction",
        value="Catania",
        statement="La gita va a Catania.",
        reason="Avevo scritto male",
    )
    assert values(
        engine.recall("gita", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-13T10:00:00Z")
    ) == {"Palermo"}
    assert values(
        engine.recall("gita", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-14T10:00:00Z")
    ) == {"Catania"}
    explanation = engine.explain(original)
    assert explanation["versions"][0]["recorded_to"] == "2026-10-14T10:00:00Z"
    assert explanation["versions"][1]["status"] == "retracted"
    assert correction["replacement_id"] != original


def test_real_update_preserves_past_and_half_open_boundary(engine, memory, clock):
    clock.set("2026-10-14T10:00:00Z")
    engine.change(
        memory["claim_ids"][0],
        kind="update",
        value="Catania",
        statement="La gita va a Catania.",
        effective_at="2026-10-13T10:00:00Z",
        reason="Cambio programma",
    )
    assert values(engine.recall("gita", valid_at="2026-10-12T11:00:00Z")) == {"Palermo"}
    assert values(engine.recall("gita", valid_at="2026-10-13T10:00:00Z")) == {"Catania"}
    assert values(
        engine.recall("gita", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-12T11:00:00Z")
    ) == {"Palermo"}


def test_scoped_correction_does_not_change_other_contexts(engine, memory, clock):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    other = engine.remember(
        "Il museo sarà a Palermo.",
        extraction=extraction("Il museo sarà a Palermo.", existing_id=entity_id, context="museum"),
    )
    clock.set("2026-10-13T10:00:00Z")
    engine.change(
        memory["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="La gita va a Catania.",
        reason="Correggo la destinazione",
    )
    result = engine.recall("Palermo Catania")
    assert values(result) == {"Palermo", "Catania"}
    assert other["claim_ids"][0] in {c["id"] for c in result["memories"]}
    assert not any(c["conflicts"] for c in result["memories"])


def test_retraction_is_historical_and_not_physical_deletion(engine, memory, clock):
    clock.set("2026-10-13T10:00:00Z")
    engine.change(memory["claim_ids"][0], kind="retraction", reason="Non è vero")
    assert not engine.recall("gita")["memories"]
    assert values(engine.recall("gita", known_at="2026-10-12T11:00:00Z")) == {"Palermo"}
    assert engine.read_source(memory["source_id"])["text"]


def test_uncertain_claims_excluded_by_default(engine):
    engine.remember(
        "Forse andiamo a Palermo.",
        extraction=extraction("Forse andiamo a Palermo.", epistemic="proposed"),
    )
    assert not engine.recall("Palermo")["memories"]
    assert values(engine.recall("Palermo", include_uncertain=True)) == {"Palermo"}


def test_conflicts_not_silently_overwritten(engine, memory):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    new = engine.remember(
        "La gita va a Catania.",
        extraction=extraction("La gita va a Catania.", value="Catania", existing_id=entity_id),
    )
    assert new["conflicts"]
    result = engine.recall("gita")
    assert values(result) == {"Palermo", "Catania"}
    assert all(c["conflicts"] for c in result["memories"])


def test_homonyms_and_scopes_do_not_merge(engine, memory):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    other = engine.remember("La gita va a Palermo.", scope="work", extraction=extraction())
    assert engine.explain(other["claim_ids"][0])["subject_id"] != entity_id
    bad = engine.remember(
        "La gita va a Palermo.", scope="work", extraction=extraction(existing_id=entity_id)
    )
    assert bad["status"] == "stored_pending_extraction"
    assert len(engine.recall("gita", scope="personal")["memories"]) == 1


def test_bad_quote_rejects_whole_extraction_but_preserves_source(engine):
    result = engine.remember("La gita va a Palermo.", extraction=extraction("Non esiste."))
    assert result["status"] == "stored_pending_extraction"
    assert engine.status()["claims"] == 0
    assert engine.read_source(result["source_id"])["text"] == "La gita va a Palermo."
    retried = engine.extract_source(result["source_id"], extraction=extraction())
    assert len(retried["claim_ids"]) == 1
    assert not engine.status()["pending_extraction"]


def test_repeated_quote_requires_an_offset(engine):
    draft = extraction("Palermo")
    result = engine.remember("Palermo e Palermo", extraction=draft)
    assert result["status"] == "stored_pending_extraction"
    draft["claims"][0]["quote_start"] = 10
    adopted = engine.extract_source(result["source_id"], extraction=draft)
    assert engine.explain(adopted["claim_ids"][0])["evidence"][0]["start"] == 10


def test_request_retries_are_idempotent_and_payload_checked(engine):
    first = engine.remember("La gita va a Palermo.", request_id="same", extraction=extraction())
    second = engine.remember("La gita va a Palermo.", request_id="same", extraction=extraction())
    assert first["claim_ids"] == second["claim_ids"]
    assert engine.status()["events"] == 2
    with pytest.raises(ValueError, match="different source"):
        engine.remember("Altro", request_id="same")
    payload = dict(
        kind="correction",
        value="Catania",
        statement="La gita va a Catania.",
        reason="Correggo",
        request_id="change-1",
    )
    a = engine.change(first["claim_ids"][0], **payload)
    b = engine.change(first["claim_ids"][0], **payload)
    assert a["change_id"] == b["change_id"]
    with pytest.raises(ValueError, match="different change"):
        engine.change(first["claim_ids"][0], **(payload | {"reason": "Altro"}))


def test_concurrent_retries_commit_once(engine):
    def write(_):
        return engine.remember(
            "La gita va a Palermo.", request_id="concurrent", extraction=extraction()
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert len({tuple(r["claim_ids"]) for r in results}) == 1
    assert engine.status()["events"] == 2


def test_replay_does_not_call_extractor(engine, memory):
    class Never:
        def extract(self, *args):
            raise AssertionError("Replay cannot re-extract")

    engine.models = Never()
    before = engine.history()
    report = engine.replay()
    assert report["claims"] == 1
    restarted = MemoryEngine(engine.settings, models=None, graph=None, clock=engine.clock)
    assert restarted.history() == before


def test_interrupted_append_recovers_only_tail(engine, memory):
    with engine.vault.log.open("ab") as stream:
        stream.write(b'{"seq":3,"incomplete":')
    assert engine.status()["events"] == 2
    assert len(list((engine.vault.meta / "recovery").glob("*.bin"))) == 1
    assert engine.vault.log.read_bytes().endswith(b"\n")


def test_corrupt_complete_event_is_never_silently_recovered(engine, memory):
    data = engine.vault.log.read_text().replace("Palermo", "Catania")
    engine.vault.log.write_text(data)
    with pytest.raises(CorruptVault, match="checksum"):
        engine.status()


def test_source_tampering_is_detected_before_returning_evidence(engine, memory):
    source = engine.read_source(memory["source_id"])
    (engine.vault.root / source["path"]).write_text("Modified original")
    with pytest.raises(CorruptVault, match="checksum"):
        engine.recall("gita")


def test_graph_outage_leaves_canonical_data_replayable(engine):
    class Broken:
        def sync(self, *args, **kwargs):
            raise ConnectionError("offline")

    engine.graph = Broken()
    result = engine.remember("La gita va a Palermo.", extraction=extraction())
    assert result["claim_ids"]
    assert any("projection pending" in w for w in result["warnings"])
    engine.graph = None
    assert values(engine.recall("gita")) == {"Palermo"}
    assert engine.replay()["claims"] == 1


def test_temporal_input_requires_timezone_and_intervals_are_valid(engine, memory):
    with pytest.raises(ValueError, match="timezone"):
        engine.recall("gita", valid_at="2026-10-12T10:00:00")
    with pytest.raises(ValueError, match="inside"):
        engine.change(
            memory["claim_ids"][0],
            kind="update",
            value="Catania",
            statement="Catania",
            reason="Cambio",
            effective_at="2026-10-11T10:00:00Z",
        )
    draft = extraction()
    draft["claims"][0].update(valid_from="2026-10-12T10:00:00Z", valid_to="2026-10-11T10:00:00Z")
    assert (
        engine.remember("La gita va a Palermo.", extraction=draft)["status"]
        == "stored_pending_extraction"
    )


def test_budget_keeps_whole_evidence_and_reports_truncation(engine, memory):
    result = engine.recall("gita", token_budget=256)
    serialized = json.dumps(result["memories"], ensure_ascii=False)
    assert len(serialized) <= 1026  # two list delimiters in addition to the item budget
    if result["memories"]:
        assert result["memories"][0]["evidence"][0]["quote"] == "La gita va a Palermo."
    else:
        assert result["truncated"]


def test_entity_object_relationship(engine):
    text = "Marco partecipa alla gita."
    result = engine.remember(
        text,
        extraction={
            "entities": [
                {"ref": "m", "name": "Marco", "kind": "person"},
                {"ref": "g", "name": "Gita", "kind": "event"},
            ],
            "claims": [
                {
                    "subject_ref": "m",
                    "object_ref": "g",
                    "predicate": "participates_in",
                    "value": "Gita",
                    "statement": text,
                    "quote": text,
                }
            ],
        },
    )
    explanation = engine.explain(result["claim_ids"][0])
    assert explanation["subject"]["name"] == "Marco"
    assert explanation["object"]["name"] == "Gita"


def test_current_knowledge_projection_has_all_versions(engine, memory, clock):
    clock.set("2026-10-14T10:00:00Z")
    engine.change(
        memory["claim_ids"][0],
        kind="update",
        value="Catania",
        statement="Catania",
        effective_at="2026-10-13T10:00:00Z",
        reason="Cambio",
    )
    with engine.vault.transaction() as events:
        state = State(events)
    assert len(state.versions) == 3
    original, replacement, updated = state.versions
    assert original.recorded_to == updated.recorded_from
    assert replacement.claim.value == "Catania"
    assert updated.claim.valid_to.isoformat() == "2026-10-13T10:00:00+00:00"


def test_explanation_normalizes_uuid(engine, memory):
    assert engine.explain(memory["claim_ids"][0].upper())["versions"]


def test_derived_view_failure_does_not_misreport_commit(engine, monkeypatch):
    def fail(state):
        raise OSError("disk unavailable for derived profiles")

    monkeypatch.setattr(engine, "_profiles", fail)
    result = engine.remember("La gita va a Palermo.", extraction=extraction())
    assert result["status"] == "stored_with_warnings"
    assert result["claim_ids"]
    assert not engine.status()["pending_extraction"]
    corrected = engine.change(
        result["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="La gita va a Catania.",
        reason="Correggo",
    )
    assert corrected["replacement_id"]
    assert values(engine.recall("gita")) == {"Catania"}
