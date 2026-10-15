import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from filelock import FileLock, Timeout

from second_brain.jobs import JobQueue, Work
from second_brain.models import Extraction
from second_brain.worker import RecoveryWorker

from .conftest import LocalVectors, extraction


class Time:
    value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def queue(tmp_path):
    return JobQueue(tmp_path / "jobs" / "queue.sqlite3", clock=Time())


def take(queue, vault="vault", attempts=3):
    return queue.claim(vault, lease_seconds=10, max_attempts=attempts)


def test_queue_restart_expired_lease_and_stale_ack_are_safe(queue):
    work = Work("extraction", "source:1", "hash", {})
    queue.reconcile("vault", [work])
    first = take(queue)
    reopened = JobQueue(queue.path, clock=queue.clock)
    assert take(reopened) is None
    queue.clock.value += 11
    second = take(reopened)
    assert second["id"] == first["id"] and second["attempts"] == 2
    assert not reopened.finish(first)
    assert reopened.finish(second)
    assert reopened.list("vault")["jobs"][0]["status"] == "done"


def test_atomic_claim_only_one_concurrent_consumer(queue):
    queue.reconcile("vault", [Work("embedding", "claim:model", "profile", {})])
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: take(queue), range(8)))
    assert sum(job is not None for job in results) == 1
    assert take(queue, "another-vault") is None


def test_retry_backoff_review_survives_reconciliation_and_operator_retry(queue):
    work = Work("extraction", "source:1", "hash", {})
    queue.reconcile("vault", [work])
    first = take(queue)
    queue.finish(first, error="validation_failed", max_attempts=2, retry_base=10)
    row = queue.list("vault")["jobs"][0]
    assert row["status"] == "retry_wait" and 1008 <= row["next_attempt_at"] <= 1010
    queue.reconcile("vault", [work])
    assert take(queue) is None
    queue.clock.value = row["next_attempt_at"] + 1
    second = take(queue)
    queue.finish(second, error="validation_failed", max_attempts=2)
    queue.reconcile("vault", [work])
    row = queue.summary("vault")["needs_review"][0]
    assert row["attempts"] == 2
    assert take(queue) is None
    with pytest.raises(ValueError):
        queue.retry("another-vault", row["job_id"])
    queue.retry("vault", row["job_id"])
    assert take(queue)["attempts"] == 1


def test_repeated_crash_exhausts_attempts(queue):
    queue.reconcile("vault", [Work("extraction", "source:1", "hash", {})])
    assert take(queue, attempts=1)
    queue.clock.value += 11
    assert take(queue, attempts=1) is None
    assert queue.summary("vault")["needs_review"][0]["last_error"] == "lease_expired"


def test_new_projection_generation_is_not_acknowledged_by_old_job(queue):
    queue.reconcile("vault", [Work("projection", "views", "head1", {})])
    old = take(queue)
    queue.reconcile("vault", [Work("projection", "views", "head2", {})])
    assert queue.finish(old)
    new = take(queue)
    assert new["generation"] == "head2" and new["attempts"] == 1
    queue.finish(new)
    assert queue.list("vault")["jobs"][0]["status"] == "done"


def test_resolved_work_clears_review_but_not_other_vault(queue):
    queue.reconcile("vault", [Work("extraction", "source:1", "hash", {})])
    job = take(queue)
    queue.finish(job, error="invalid", max_attempts=1)
    queue.reconcile("other", [])
    assert queue.summary("vault")["counts"]["needs_review"] == 1
    queue.reconcile("vault", [])
    assert queue.summary("vault")["counts"]["needs_review"] == 0


@pytest.fixture
def recovery_engine(engine, tmp_path):
    engine.settings.worker_queue_path = tmp_path / "queue" / "jobs.sqlite3"
    engine.settings.embedding_dimensions = 3
    engine.settings.worker_max_attempts = 2
    engine.job_queue().clock = Time()
    return engine


class Extractor(LocalVectors):
    calls = 0

    def extract(self, text, source, entities):
        self.calls += 1
        return Extraction.model_validate(extraction(text))


def test_worker_finds_saved_source_without_enqueue_and_repairs_it(recovery_engine):
    engine = recovery_engine
    saved = engine.remember("The trip goes to Palermo.")
    assert saved["status"] == "stored_pending_extraction"
    assert engine.job_queue().list(engine.vault.id)["jobs"] == []
    engine.models = model = Extractor()
    worker = RecoveryWorker(engine)
    for _ in range(5):
        worker.run_once()
    status = engine.status()
    assert model.calls == 1
    assert not status["pending_extraction"]
    assert status["claims"] == 1 and status["embeddings_missing"] == 0
    assert status["recovery"]["counts"]["done"] >= 1


def test_failed_extraction_reaches_review_and_recovers_after_explicit_retry(recovery_engine):
    engine = recovery_engine
    engine.remember("The trip goes to Palermo.")
    worker = RecoveryWorker(engine)
    for _ in range(2):
        for _ in range(3):
            worker.run_once()
        engine.job_queue().clock.value += 4000
    rows = engine.recovery_jobs("needs_review")["jobs"]
    failed = next(j for j in rows if j["kind"] == "extraction")
    assert failed["attempts"] == 2
    engine.models = Extractor()
    engine.retry_job(failed["job_id"])
    for _ in range(4):
        worker.run_once()
    assert engine.status()["claims"] == 1
    assert not engine.recovery_jobs("needs_review")["jobs"]


def test_worker_repairs_vectors_without_reextracting_or_resurrecting_claims(recovery_engine):
    engine = recovery_engine
    saved = engine.remember("The trip goes to Palermo.", extraction=extraction())
    engine.change(saved["claim_ids"][0], kind="retraction", reason="Incorrect")
    engine.models = LocalVectors()
    worker = RecoveryWorker(engine)
    for _ in range(4):
        worker.run_once()
    status = engine.status()
    assert status["claims"] == 1 and status["embeddings_incompatible"] == 0
    assert not engine.recall("trip")["memories"]
    with engine.vault.transaction() as events:
        assert len([e for e in events if e.kind == "embeddings"]) == 1
    engine.repair_embedding(saved["claim_ids"][0])
    with engine.vault.transaction() as events:
        assert len([e for e in events if e.kind == "embeddings"]) == 1


def test_foreground_and_worker_share_source_lock(recovery_engine):
    engine = recovery_engine
    saved = engine.remember("The trip goes to Palermo.")
    started, release = threading.Event(), threading.Event()

    class Slow(Extractor):
        def extract(self, *args):
            started.set()
            assert release.wait(3)
            return super().extract(*args)

    engine.models = model = Slow()
    worker = RecoveryWorker(engine)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(engine.extract_source, saved["source_id"])
        assert started.wait(2)
        second = pool.submit(worker.run_once)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert model.calls == 1 and engine.status()["claims"] == 1


def test_projection_retry_after_dependency_recovers(recovery_engine):
    engine = recovery_engine
    engine.remember("The trip goes to Palermo.", extraction=extraction())
    engine.models = LocalVectors()

    class Graph:
        available, head = False, None

        def is_current(self, state):
            return self.available and self.head == state.head

        def sync(self, state):
            if not self.available:
                raise ConnectionError("private password must never reach job diagnostics")
            self.head = state.head

    engine.graph = graph = Graph()
    worker = RecoveryWorker(engine)
    for _ in range(4):
        worker.run_once()
    jobs = engine.recovery_jobs()["jobs"]
    assert any(j["kind"] == "projection" and j["status"] == "retry_wait" for j in jobs)
    assert "private password" not in json.dumps(jobs)
    graph.available = True
    engine.job_queue().clock.value += 4000
    for _ in range(3):
        worker.run_once()
    assert graph.head == engine._ledger_head()
    assert all(j["status"] == "done" for j in engine.recovery_jobs()["jobs"])


def test_process_death_after_embedding_commit_does_not_duplicate_ledger(recovery_engine):
    engine = recovery_engine
    saved = engine.remember("The trip goes to Palermo.", extraction=extraction())
    RecoveryWorker(engine).reconcile()
    # Exit without acknowledging the durable embedding event. A new process must recover.
    script = """
import os
from second_brain.config import Settings
from second_brain.engine import MemoryEngine
from second_brain.jobs import JobQueue
from second_brain.worker import RecoveryWorker
class Models:
    def embed(self, texts): return [[1.0,0.0,0.0] for _ in texts]
engine = MemoryEngine(Settings(vault_path=os.environ['CASE_VAULT'],
    worker_queue_path=os.environ['CASE_QUEUE'], embedding_dimensions=3, _env_file=None),
    graph=None, models=Models())
engine.job_queue().clock = lambda: 1000.0
worker = RecoveryWorker(engine)
job = engine.job_queue().claim(engine.vault.id, lease_seconds=10, max_attempts=5)
assert job['kind'] == 'embedding'
worker._execute(job)
os._exit(0)
"""
    import os

    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env={
            **os.environ,
            "CASE_VAULT": str(engine.vault.root),
            "CASE_QUEUE": str(engine.settings.worker_queue_path),
        },
    )
    engine.models = LocalVectors()
    engine.job_queue().clock.value += 11
    for _ in range(4):
        RecoveryWorker(engine).run_once()
    with engine.vault.transaction() as events:
        assert len([e for e in events if e.kind == "embeddings"]) == 1
    assert engine.explain(saved["claim_ids"][0])["id"] == saved["claim_ids"][0]


def test_worker_heartbeat_and_single_leader(recovery_engine):
    engine = recovery_engine
    worker = RecoveryWorker(engine)
    assert not engine.job_queue().summary(engine.vault.id)["worker_alive"]
    lock = engine.job_queue().path.parent / f"{engine.vault.id}.worker.lock"
    with FileLock(lock, timeout=0):
        with pytest.raises(Timeout):
            worker.run(once=True)
    worker.run(once=True)
    # Graceful termination is visible rather than reporting a stopped worker as alive.
    assert not engine.job_queue().summary(engine.vault.id)["worker_alive"]


def test_heartbeat_renews_long_running_job_lease(recovery_engine, monkeypatch):
    engine = recovery_engine
    queue = engine.job_queue()
    queue.reconcile(engine.vault.id, [Work("projection", "views", "head", {})])
    engine.settings.worker_lease_seconds = 10
    worker = RecoveryWorker(engine)
    started, renewed, release = threading.Event(), threading.Event(), threading.Event()
    original = queue.renew

    def renew(job, duration):
        result = original(job, duration)
        renewed.set()
        return result

    def execute(job):
        started.set()
        assert release.wait(6)

    monkeypatch.setattr(queue, "renew", renew)
    monkeypatch.setattr(worker, "reconcile", lambda: None)
    monkeypatch.setattr(worker, "_execute", execute)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(worker.run, once=True)
        assert started.wait(2)
        queue.clock.value += 9
        try:
            assert renewed.wait(5)
            queue.clock.value += 2
            assert take(queue, engine.vault.id) is None
            assert queue.summary(engine.vault.id)["worker_alive"]
        finally:
            release.set()
        running.result(timeout=3)
    assert queue.list(engine.vault.id)["jobs"][0]["status"] == "done"


@pytest.mark.asyncio
async def test_recovery_tools_enforce_read_write_credentials(recovery_engine):
    import httpx
    from pydantic import SecretStr

    from second_brain.server import create_server

    engine = recovery_engine
    reader, writer = "read-" + "r" * 32, "write-" + "w" * 32
    engine.settings.mcp_read_token = SecretStr(reader)
    engine.settings.mcp_write_token = SecretStr(writer)
    queue = engine.job_queue()
    queue.reconcile(engine.vault.id, [Work("extraction", "source:1", "hash", {})])
    job = take(queue, engine.vault.id)
    queue.finish(job, error="validation_failed", max_attempts=1)
    server = create_server(engine)
    app = server.streamable_http_app()
    async with (
        server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client,
    ):

        async def call(name, args, token):
            result = await client.post(
                "/mcp",
                headers={"Authorization": "Bearer " + token},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
            return result.json()["result"]

        inspected = await call("recovery_jobs", {"status": "needs_review"}, reader)
        assert not inspected.get("isError")
        assert inspected["structuredContent"]["jobs"][0]["attempts"] == 1
        assert "lease_token" not in json.dumps(inspected)
        assert (await call("retry_job", {"job_id": job["id"]}, reader))["isError"]
        assert not (await call("retry_job", {"job_id": job["id"]}, writer)).get("isError")
        assert queue.list(engine.vault.id)["jobs"][0]["status"] == "pending"


def test_queue_refuses_symlink(tmp_path):
    target = tmp_path / "outside"
    target.write_text("Do not modify")
    link = tmp_path / "jobs.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        JobQueue(link)
    assert target.read_text() == "Do not modify"
