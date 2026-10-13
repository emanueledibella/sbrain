import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from second_brain.demo import run_demo
from second_brain.engine import MemoryEngine
from second_brain.state import State
from second_brain.store import CorruptVault

from .conftest import extraction


def values(result):
    return {c["value"] for c in result["memories"]}


def test_original_evidence_and_revision_survive_edits(engine, memory):
    source_id = memory["source_id"]
    edited = "The trip goes to Catania."
    engine.remember(edited, source_id=source_id, extraction=extraction(edited, value="Catania"))
    assert engine.read_source(source_id, 1)["text"] == "The trip goes to Palermo."
    assert engine.read_source(source_id, 2)["text"] == edited
    evidence = engine.explain(memory["claim_ids"][0])["evidence"][0]
    assert evidence["revision"] == 1
    assert evidence["quote"] == "The trip goes to Palermo."


def test_correction_has_two_time_axes(engine, memory, clock):
    original = memory["claim_ids"][0]
    clock.set("2026-10-14T10:00:00Z")
    correction = engine.change(
        original,
        kind="correction",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="I wrote it incorrectly",
    )
    assert values(
        engine.recall("trip", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-13T10:00:00Z")
    ) == {"Palermo"}
    assert values(
        engine.recall("trip", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-14T10:00:00Z")
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
        statement="The trip goes to Catania.",
        effective_at="2026-10-13T10:00:00Z",
        reason="Change of plans",
    )
    assert values(engine.recall("trip", valid_at="2026-10-12T11:00:00Z")) == {"Palermo"}
    assert values(engine.recall("trip", valid_at="2026-10-13T10:00:00Z")) == {"Catania"}
    assert values(
        engine.recall("trip", valid_at="2026-10-13T10:00:00Z", known_at="2026-10-12T11:00:00Z")
    ) == {"Palermo"}


def test_scoped_correction_does_not_change_other_contexts(engine, memory, clock):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    other = engine.remember(
        "The museum will be in Palermo.",
        extraction=extraction(
            "The museum will be in Palermo.", existing_id=entity_id, context="museum"
        ),
    )
    clock.set("2026-10-13T10:00:00Z")
    engine.change(
        memory["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="Correcting the destination",
    )
    result = engine.recall("Palermo Catania")
    assert values(result) == {"Palermo", "Catania"}
    assert other["claim_ids"][0] in {c["id"] for c in result["memories"]}
    assert not any(c["conflicts"] for c in result["memories"])


def test_retraction_is_historical_and_not_physical_deletion(engine, memory, clock):
    clock.set("2026-10-13T10:00:00Z")
    engine.change(memory["claim_ids"][0], kind="retraction", reason="This is incorrect")
    assert not engine.recall("trip")["memories"]
    assert values(engine.recall("trip", known_at="2026-10-12T11:00:00Z")) == {"Palermo"}
    assert engine.read_source(memory["source_id"])["text"]


def test_uncertain_claims_excluded_by_default(engine):
    engine.remember(
        "We might go to Palermo.",
        extraction=extraction("We might go to Palermo.", epistemic="proposed"),
    )
    assert not engine.recall("Palermo")["memories"]
    assert values(engine.recall("Palermo", include_uncertain=True)) == {"Palermo"}


def test_conflicts_not_silently_overwritten(engine, memory):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    new = engine.remember(
        "The trip goes to Catania.",
        extraction=extraction("The trip goes to Catania.", value="Catania", existing_id=entity_id),
    )
    assert new["conflicts"]
    result = engine.recall("trip")
    assert values(result) == {"Palermo", "Catania"}
    assert all(c["conflicts"] for c in result["memories"])


def test_homonyms_and_scopes_do_not_merge(engine, memory):
    entity_id = engine.explain(memory["claim_ids"][0])["subject_id"]
    other = engine.remember("The trip goes to Palermo.", scope="work", extraction=extraction())
    assert engine.explain(other["claim_ids"][0])["subject_id"] != entity_id
    bad = engine.remember(
        "The trip goes to Palermo.", scope="work", extraction=extraction(existing_id=entity_id)
    )
    assert bad["status"] == "stored_pending_extraction"
    assert len(engine.recall("trip", scope="personal")["memories"]) == 1


def test_bad_quote_rejects_whole_extraction_but_preserves_source(engine):
    result = engine.remember("The trip goes to Palermo.", extraction=extraction("Does not exist."))
    assert result["status"] == "stored_pending_extraction"
    assert engine.status()["claims"] == 0
    assert engine.read_source(result["source_id"])["text"] == "The trip goes to Palermo."
    retried = engine.extract_source(result["source_id"], extraction=extraction())
    assert len(retried["claim_ids"]) == 1
    assert not engine.status()["pending_extraction"]


def test_repeated_quote_requires_an_offset(engine):
    draft = extraction("Palermo")
    result = engine.remember("Palermo and Palermo", extraction=draft)
    assert result["status"] == "stored_pending_extraction"
    draft["claims"][0]["quote_start"] = 12
    adopted = engine.extract_source(result["source_id"], extraction=draft)
    assert engine.explain(adopted["claim_ids"][0])["evidence"][0]["start"] == 12


def test_request_retries_are_idempotent_and_payload_checked(engine):
    first = engine.remember("The trip goes to Palermo.", request_id="same", extraction=extraction())
    second = engine.remember(
        "The trip goes to Palermo.", request_id="same", extraction=extraction()
    )
    assert first["claim_ids"] == second["claim_ids"]
    assert engine.status()["events"] == 2
    with pytest.raises(ValueError, match="different source"):
        engine.remember("Other", request_id="same")
    payload = dict(
        kind="correction",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="Correcting this",
        request_id="change-1",
    )
    a = engine.change(first["claim_ids"][0], **payload)
    b = engine.change(first["claim_ids"][0], **payload)
    assert a["change_id"] == b["change_id"]
    with pytest.raises(ValueError, match="different change"):
        engine.change(first["claim_ids"][0], **(payload | {"reason": "Other"}))


def test_concurrent_retries_commit_once(engine):
    def write(_):
        return engine.remember(
            "The trip goes to Palermo.", request_id="concurrent", extraction=extraction()
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
        engine.recall("trip")


def test_graph_outage_leaves_canonical_data_replayable(engine):
    class Broken:
        def sync(self, *args, **kwargs):
            raise ConnectionError("offline")

    engine.graph = Broken()
    result = engine.remember("The trip goes to Palermo.", extraction=extraction())
    assert result["claim_ids"]
    assert any("projection pending" in w for w in result["warnings"])
    engine.graph = None
    assert values(engine.recall("trip")) == {"Palermo"}
    assert engine.replay()["claims"] == 1


def test_temporal_input_requires_timezone_and_intervals_are_valid(engine, memory):
    with pytest.raises(ValueError, match="timezone"):
        engine.recall("trip", valid_at="2026-10-12T10:00:00")
    with pytest.raises(ValueError, match="inside"):
        engine.change(
            memory["claim_ids"][0],
            kind="update",
            value="Catania",
            statement="Catania",
            reason="Change",
            effective_at="2026-10-11T10:00:00Z",
        )
    draft = extraction()
    draft["claims"][0].update(valid_from="2026-10-12T10:00:00Z", valid_to="2026-10-11T10:00:00Z")
    assert (
        engine.remember("The trip goes to Palermo.", extraction=draft)["status"]
        == "stored_pending_extraction"
    )


def test_budget_keeps_whole_evidence_and_reports_truncation(engine, memory):
    result = engine.recall("trip", token_budget=256)
    serialized = json.dumps(result["memories"], ensure_ascii=False)
    assert len(serialized) <= 1026  # two list delimiters in addition to the item budget
    if result["memories"]:
        assert result["memories"][0]["evidence"][0]["quote"] == "The trip goes to Palermo."
    else:
        assert result["truncated"]


def test_entity_object_relationship(engine):
    text = "Marco is joining the trip."
    result = engine.remember(
        text,
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
                    "value": "Trip",
                    "statement": text,
                    "quote": text,
                }
            ],
        },
    )
    explanation = engine.explain(result["claim_ids"][0])
    assert explanation["subject"]["name"] == "Marco"
    assert explanation["object"]["name"] == "Trip"


def test_current_knowledge_projection_has_all_versions(engine, memory, clock):
    clock.set("2026-10-14T10:00:00Z")
    engine.change(
        memory["claim_ids"][0],
        kind="update",
        value="Catania",
        statement="Catania",
        effective_at="2026-10-13T10:00:00Z",
        reason="Change",
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
    result = engine.remember("The trip goes to Palermo.", extraction=extraction())
    assert result["status"] == "stored_with_warnings"
    assert result["claim_ids"]
    assert not engine.status()["pending_extraction"]
    corrected = engine.change(
        result["claim_ids"][0],
        kind="correction",
        value="Catania",
        statement="The trip goes to Catania.",
        reason="Correcting this",
    )
    assert corrected["replacement_id"]
    assert values(engine.recall("trip")) == {"Catania"}


def test_english_demo_coexists_with_earlier_memories_and_can_be_retried(engine):
    earlier = engine.remember(
        "The trip goes to Palermo.",
        scope="demo",
        request_id="demo-source-v1",
        extraction=extraction(),
    )
    result = run_demo(engine)
    expected = {
        "before_change": {"Palermo", "Marco: yes"},
        "after_correction": {"Catania", "Marco: yes"},
    }
    for name, expected_values in expected.items():
        assert {
            item["value"]
            for item in result["examples"][name]
            if item["source"] != earlier["source_id"]
        } == expected_values
    assert engine.explain(earlier["claim_ids"][0])["versions"][-1]["status"] == "active"
    count = engine.status()["events"]
    assert run_demo(engine)["examples"] == result["examples"]
    assert engine.status()["events"] == count
