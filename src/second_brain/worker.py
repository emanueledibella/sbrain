"""Reconcile the canonical ledger and execute persistent recovery jobs independently of MCP."""

import hashlib
import json
import logging
import signal
import socket
import threading
import time

from filelock import FileLock

from .failures import Failure, RecoveryFailure, classify
from .jobs import Work
from .models import uid

logger = logging.getLogger(__name__)


class RecoveryWorker:
    def __init__(self, engine, *, stop=None):
        self.engine = engine
        self.settings = engine.settings
        self.queue = engine.job_queue()
        self.stop = stop or threading.Event()
        self.worker_id = f"{socket.gethostname()}:{uid()}"
        self.active = None
        self._active_lock = threading.Lock()

    def reconcile(self):
        self.engine.refresh_embedding_profile()
        with self.engine.vault.transaction() as events:
            state = self.engine._state(events)
        desired = []
        for source in state.sources.values():
            if (
                source.kind not in {"memory_change", "identity_change"}
                and source.key not in state.extracted
                and not state.extraction_cover(source)
            ):
                desired.append(
                    self.engine.extraction_work(source)
                )
        for claim in state.claims.values():
            if not self.engine.compatible_embedding(claim):
                desired.append(
                    Work(
                        "embedding",
                        f"{claim.id}:{self.engine.embedding_profile}",
                        self.engine.embedding_profile,
                        {"claim_id": claim.id},
                    )
                )
        marker = self.engine.vault.meta / "views.json"
        try:
            views_current = json.loads(marker.read_text())["head"] == state.head
        except (OSError, ValueError, KeyError, TypeError):
            views_current = False
        graph_current = self.engine.graph is None
        if self.engine.graph:
            try:
                graph_current = self.engine.graph.is_current(state)
            except Exception:
                graph_current = False
        if not views_current or not graph_current:
            generation = hashlib.sha256(
                f"{state.head}:{self.engine.vocabulary.digest}:{self.engine.embedding_profile}:"
                f"{self.settings.neo4j_uri}:{self.settings.neo4j_database}".encode()
            ).hexdigest()
            desired.append(Work("projection", "views", generation, {}))
        self.queue.reconcile(self.engine.vault.id, desired)
        return len(desired)

    def _execute(self, job):
        payload = json.loads(job["payload"])
        if job["kind"] == "extraction":
            result = self.engine.extract_source(payload["source_id"], payload["revision"])
            if result["status"] == "stored_pending_extraction":
                raise RecoveryFailure(Failure(**result["failure"]))
        elif job["kind"] == "embedding":
            if job["generation"] == self.engine.embedding_profile:
                self.engine.repair_embedding(payload["claim_id"])
        elif job["kind"] == "projection":
            with self.engine.vault.transaction() as events:
                state = self.engine._state(events)
            if self.engine.graph:
                self.engine.graph.sync(state)
            if self.engine._materialize(state):
                raise RuntimeError("Markdown projection pending")
        else:
            raise ValueError("Unsupported work kind")

    def run_once(self, *, reconcile=True):
        if reconcile:
            self.reconcile()
        job = self.queue.claim(
            self.engine.vault.id,
            lease_seconds=self.settings.worker_lease_seconds,
            max_attempts=self.settings.worker_max_attempts,
        )
        if job is None:
            return False
        with self._active_lock:
            self.active = job
        try:
            error, failure = None, None
            try:
                self._execute(job)
            except Exception as exc:
                # Never persist source excerpts, model responses or credentials in diagnostics.
                failure = classify(exc, job["kind"]).dict()
                error = f"{failure['stage']}:{failure['code']}"
                logger.warning("Recovery job %s (%s): %s", job["id"], job["kind"], error)
            self.queue.finish(
                job,
                error=error,
                failure=failure,
                max_attempts=self.settings.worker_max_attempts,
                retry_base=self.settings.worker_retry_base_seconds,
                retry_max=self.settings.worker_retry_max_seconds,
            )
        finally:
            with self._active_lock:
                self.active = None
        return True

    def _heartbeat(self, done):
        while not done.is_set():
            try:
                with self._active_lock:
                    job = self.active
                self.queue.heartbeat(
                    self.engine.vault.id, self.worker_id, "running" if job else "idle"
                )
                if job:
                    self.queue.renew(job, self.settings.worker_lease_seconds)
            except Exception as exc:
                logger.warning("Recovery heartbeat failed: %s", type(exc).__name__)
            done.wait(min(10, self.settings.worker_lease_seconds / 3))

    def run(self, *, once=False):
        # One recovery loop per vault also bounds competition for a local Ollama model.
        lock = self.queue.path.parent / f"{self.engine.vault.id}.worker.lock"
        with FileLock(lock, timeout=0, mode=0o600):
            return self._run_loop(once=once)

    def _run_loop(self, *, once=False):
        done = threading.Event()
        self.queue.heartbeat(self.engine.vault.id, self.worker_id)
        heartbeat = threading.Thread(target=self._heartbeat, args=(done,), daemon=True)
        heartbeat.start()
        last_reconcile = float("-inf")
        try:
            while not self.stop.is_set():
                try:
                    due = (
                        time.monotonic() - last_reconcile >= self.settings.worker_reconcile_seconds
                    )
                    if due:
                        self.reconcile()
                        last_reconcile = time.monotonic()
                    worked = self.run_once(reconcile=False)
                except Exception as exc:
                    logger.warning("Recovery pass failed: %s", type(exc).__name__)
                    worked = False
                    if once:
                        raise
                if once:
                    return {"processed": int(worked)}
                if not worked:
                    self.stop.wait(self.settings.worker_poll_seconds)
        finally:
            done.set()
            heartbeat.join(timeout=6)
            self.queue.heartbeat(self.engine.vault.id, self.worker_id, "stopped")


def run_worker(engine, *, once=False):
    worker = RecoveryWorker(engine)
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: worker.stop.set())
    try:
        return worker.run(once=once)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
