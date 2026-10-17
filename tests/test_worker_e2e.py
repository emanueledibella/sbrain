"""Repeatable fault/restart/review scenarios over real MCP and worker processes."""

import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from second_brain.config import Settings
from second_brain.graph import Neo4jGraph
from second_brain.models import uid
from second_brain.state import State
from second_brain.store import Vault

ALPHA = "The destination of Recovery Trip Alpha is Palermo."
BETA = "The destination of Recovery Trip Beta is Catania."


class ControlledOllama:
    """Actual HTTP boundary with deterministic output, plus an injected HTTP 503 outage."""

    def __init__(self):
        self.available = False
        self.successful_extractions = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not owner.available:
                    return self.reply(503, {"error": "simulated Ollama outage"})
                self.reply(
                    200,
                    {
                        "models": [
                            {"name": "test-chat:latest", "digest": "test-chat-v1"},
                            {"name": "test-embedding:latest", "digest": "test-embedding-v1"},
                        ]
                    },
                )

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not owner.available:
                    return self.reply(503, {"error": "simulated Ollama outage"})
                if self.path == "/api/embed":
                    return self.reply(200, {"embeddings": [[1.0, 0.0, 0.0] for _ in data["input"]]})
                assert self.path == "/api/chat"
                text = json.loads(data["messages"][1]["content"])["source"]
                assert text == ALPHA
                draft = {
                    "entities": [{"ref": "trip", "name": "Recovery Trip Alpha", "kind": "event"}],
                    "claims": [
                        {
                            "subject_ref": "trip",
                            "predicate": "destination",
                            "value": "Palermo",
                            "statement": text,
                            "quote": text,
                            "context": "destination",
                            "epistemic": "explicit",
                            "polarity": "positive",
                        }
                    ],
                }
                owner.successful_extractions += 1
                self.reply(200, {"model": "test-chat", "message": {"content": json.dumps(draft)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RecoveryScenario:
    def __init__(self, directory, *, live):
        self.directory, self.live = directory, live
        self.vault = Vault(directory / "vault")
        self.scope = "test-worker-" + uid()
        self.dependency = ControlledOllama()
        self.processes, self.logs, self.stages = {}, [], []
        self.live_settings = Settings(
            _env_file=".env" if live else None, vocabulary_extension_path=None
        )
        self.graph = Neo4jGraph(self.live_settings, self.vault.id) if live else None
        self.writer = "worker-e2e-write-" + uid() + uid()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        # Keep this address reserved without a listener, so graph connection attempts fail.
        self.unavailable = socket.socket()
        self.unavailable.bind(("127.0.0.1", 0))
        self.bad_graph = f"bolt://127.0.0.1:{self.unavailable.getsockname()[1]}"
        (directory / "graph-down").touch()

    def env(self, *, fault, probe):
        settings = self.live_settings
        result = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    [
                        str(Path(__file__).resolve().parents[1]),
                        os.getenv("PYTHONPATH"),
                    ],
                )
            ),
            "SB_VAULT_PATH": str(self.vault.root),
            "SB_WORKER_QUEUE_PATH": str(self.directory / "queue" / "jobs.sqlite3"),
            "SB_MCP_WRITE_TOKEN": self.writer,
            "SB_MCP_READ_TOKEN": "worker-e2e-read-" + uid() + uid(),
            "SB_MCP_REQUESTS_PER_MINUTE": "1000",
            "SB_WORKER_MAX_ATTEMPTS": "2",
            "SB_WORKER_LEASE_SECONDS": "10",
            "SB_WORKER_RECONCILE_SECONDS": "1",
            "SB_WORKER_POLL_SECONDS": "0.1",
            "SB_WORKER_RETRY_BASE_SECONDS": "3",
            "SB_WORKER_RETRY_MAX_SECONDS": "6",
            "RECOVERY_TEST_DIRECTORY": str(self.directory),
            "RECOVERY_TEST_PROBE": probe,
            "SB_OLLAMA_URL": settings.ollama_url
            if self.live and not fault
            else self.dependency.url,
            "SB_NEO4J_URI": self.bad_graph if fault else settings.neo4j_uri,
            "SB_NEO4J_USER": settings.neo4j_user,
            "SB_NEO4J_DATABASE": settings.neo4j_database,
            "NEO4J_PASSWORD": settings.neo4j_password.get_secret_value() if self.live else "",
            "SB_CHAT_MODEL": settings.chat_model if self.live else "test-chat",
            "SB_EMBEDDING_MODEL": settings.embedding_model if self.live else "test-embedding",
            "SB_EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions if self.live else 3),
            "SB_MODEL_TIMEOUT": str(settings.model_timeout if self.live else 5),
        }
        # Do not accidentally apply a deployment's private extension to disposable test data.
        result.pop("SB_VOCABULARY_EXTENSION_PATH", None)
        return result

    async def start(self, mode, *, fault, pause=False):
        probe = uid()
        args = [sys.executable, "-m", "tests.helpers.recovery_process", mode]
        if mode == "server":
            args += ["--port", str(self.port)]
        if not self.live:
            args += ["--file-graph"]
        if pause:
            args += ["--pause-after-extraction"]
        path = self.directory / f"{mode}-{probe}.log"
        log = path.open("wb")
        self.logs.append(log)
        process = await asyncio.create_subprocess_exec(
            *args, env=self.env(fault=fault, probe=probe), stdout=log, stderr=log
        )
        self.processes[mode] = process
        if mode == "worker":

            async def reconciled():
                self.assert_alive(mode)
                return (self.directory / f"reconciled-{probe}").exists()

            await self.wait(reconciled)
        else:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as http:

                async def ready():
                    self.assert_alive(mode)
                    try:
                        return (
                            await http.get(f"http://127.0.0.1:{self.port}/health")
                        ).status_code == 200
                    except httpx.TransportError:
                        return False

                await self.wait(ready, timeout=20)

    def assert_alive(self, mode):
        assert self.processes[mode].returncode is None, f"{mode} exited; see {self.directory}"

    async def kill(self, mode):
        process = self.processes.pop(mode, None)
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)

    async def wait(self, predicate, timeout=None):
        # A live extraction can make two chat calls followed by an embedding call.
        # Keep readiness waits short via their explicit override, but do not impose
        # the old Qwen deadline on a valid LFM recovery operation.
        if timeout is None:
            timeout = 3 * self.live_settings.model_timeout + 30 if self.live else 180
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(0.1)
        pytest.fail("Timed out waiting for recovery state")

    @asynccontextmanager
    async def client(self):
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + self.writer},
            trust_env=False,
            timeout=3 * self.live_settings.model_timeout + 30 if self.live else 180,
        ) as http:
            async with streamable_http_client(
                f"http://127.0.0.1:{self.port}/mcp", http_client=http
            ) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session

    @staticmethod
    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        assert not result.isError, (name, result.content)
        return result.structuredContent

    def snapshot(self):
        with self.vault.transaction() as events:
            state = State(events)
            return {
                "head": state.head,
                "sources": len(state.sources),
                "claims": len(state.claims),
                "claim_ids": sorted(state.claims),
                "extractions": {
                    s.key: sum(
                        e.kind == "extracted" and e.metadata.get("source_key") == s.key
                        for e in events
                    )
                    for s in state.sources.values()
                },
                "embedding_events": sum(e.kind == "embeddings" for e in events),
                "missing": sum(not c.embedding for c in state.claims.values()),
            }

    def record(self, stage, **details):
        self.stages.append({"stage": stage, **details})
        (self.directory / "recovery-report.json").write_text(json.dumps(self.stages, indent=2))

    async def run(self):
        alpha_args = {"text": ALPHA, "scope": self.scope, "request_id": "alpha"}
        beta_args = {
            "text": BETA,
            "scope": self.scope,
            "request_id": "beta",
            "extraction": {
                "entities": [{"ref": "trip", "name": "Recovery Trip Beta", "kind": "event"}],
                "claims": [
                    {
                        "subject_ref": "trip",
                        "predicate": "destination",
                        "value": "Catania",
                        "statement": BETA,
                        "quote": BETA,
                        "context": "destination",
                    }
                ],
            },
        }
        await self.start("server", fault=True)
        async with self.client() as session:
            alpha = await self.call(session, "remember", alpha_args)
            beta = await self.call(session, "remember", beta_args)
            assert alpha["status"] == "stored_pending_extraction"
            assert beta["status"] == "stored_with_warnings"
            initial = self.snapshot()
            assert (initial["sources"], initial["claims"], initial["missing"]) == (2, 1, 1)
            self.record("outage_preserves_sources", snapshot=initial)
            await self.start("worker", fault=True)

            async def jobs():
                return (await self.call(session, "recovery_jobs", {}))["jobs"]

            async def retry_seen():
                rows = await jobs()
                return (
                    rows
                    if any(j["status"] == "retry_wait" and j["attempts"] == 1 for j in rows)
                    else None
                )

            retry = await self.wait(retry_seen)
            delayed = next(j for j in retry if j["status"] == "retry_wait")
            assert 2.3 <= delayed["next_attempt_at"] - delayed["updated_at"] <= 3.1
            self.record("automatic_retry", jobs=retry)
            await self.kill("worker")
            await self.start("worker", fault=True)

            async def reviewed():
                rows = await jobs()
                return (
                    rows
                    if {j["kind"] for j in rows} == {"extraction", "embedding", "projection"}
                    and all(j["status"] == "needs_review" and j["attempts"] == 2 for j in rows)
                    else None
                )

            review = await self.wait(reviewed)
            await self.kill("worker")
            await self.start("worker", fault=True)
            assert await jobs() == review
            assert self.snapshot() == initial
            self.record("needs_review_survives_restart", jobs=review)
        await self.kill("worker")
        await self.kill("server")
        self.dependency.available = True
        (self.directory / "graph-down").unlink()
        await self.start("server", fault=False)
        async with self.client() as session:
            retained = (await self.call(session, "recovery_jobs", {}))["jobs"]
            assert retained == review
            for job in retained:
                await self.call(session, "retry_job", {"job_id": job["job_id"]})
            await self.start("worker", fault=False, pause=True)

            async def committed():
                self.assert_alive("worker")
                marker = self.directory / "extraction-committed.json"
                return json.loads(marker.read_text()) if marker.exists() else None

            marker = await self.wait(committed)
            committed_state = self.snapshot()
            assert committed_state["extractions"][alpha["source_id"] + ":1"] == 1
            rows = (await self.call(session, "recovery_jobs", {}))["jobs"]
            assert next(j for j in rows if j["job_id"] == marker["job_id"])["status"] == "running"
            self.record(
                "commit_before_acknowledgment", snapshot=committed_state, job_id=marker["job_id"]
            )
            await self.kill("worker")
            await self.start("worker", fault=False)

            async def completed():
                rows = (await self.call(session, "recovery_jobs", {}))["jobs"]
                return rows if len(rows) == 3 and all(j["status"] == "done" for j in rows) else None

            done = await self.wait(completed)
            final = self.snapshot()
            assert final["sources"] == 2 and final["claims"] >= 2 and final["missing"] == 0
            assert final["claim_ids"] == committed_state["claim_ids"]
            assert set(final["extractions"].values()) == {1}
            assert final["embedding_events"] == 1
            for source, text, value in ((alpha, ALPHA, "Palermo"), (beta, BETA, "Catania")):
                original = await self.call(
                    session, "read_source", {"source_id": source["source_id"], "revision": 1}
                )
                assert original["text"] == text
                recall = await self.call(
                    session, "recall", {"query": text, "scope": self.scope, "limit": 20}
                )
                assert not recall["warnings"]
                assert any(
                    m["value"] == value
                    and any(e["source_id"] == source["source_id"] for e in m["evidence"])
                    for m in recall["memories"]
                )
            assert (await self.call(session, "validate_knowledge", {"scope": self.scope}))[
                "conforms"
            ]
            if self.graph:
                with self.vault.transaction() as events:
                    assert self.graph.is_current(State(events))
            else:
                assert (self.directory / "graph-head").read_text() == final["head"]
                assert self.dependency.successful_extractions == 1
            before = self.snapshot()
            await self.call(session, "remember", alpha_args)
            await self.call(session, "remember", beta_args)
            assert self.snapshot() == before
            self.record("recovered_without_duplicates", snapshot=final, jobs=done)

    async def close(self):
        try:
            stopped = await asyncio.gather(
                *(self.kill(mode) for mode in list(self.processes)), return_exceptions=True
            )
            if self.graph:
                self.graph.query(
                    "MATCH (n) WHERE n.vault_id=$vault DETACH DELETE n", vault=self.vault.id
                )
            for result in stopped:
                if isinstance(result, BaseException):
                    raise result
        finally:
            if self.graph:
                self.graph.close()
            self.dependency.close()
            self.unavailable.close()
            for log in self.logs:
                log.close()


async def exercise(directory, *, live, request):
    scenario = RecoveryScenario(directory, live=live)
    try:
        await scenario.run()
    finally:
        try:
            await scenario.close()
        finally:
            request.node.add_report_section(
                "call", "worker recovery stages", json.dumps(scenario.stages)
            )


@pytest.mark.asyncio
@pytest.mark.worker_e2e
async def test_worker_recovery_faults_restarts_and_review(tmp_path, request):
    await exercise(tmp_path, live=False, request=request)


@pytest.mark.asyncio
@pytest.mark.worker_e2e
@pytest.mark.integration
async def test_live_worker_recovery_faults_restarts_and_review(tmp_path, request):
    if os.getenv("SB_TEST_NEO4J") != "1" or os.getenv("SB_TEST_OLLAMA") != "1":
        pytest.skip("Set SB_TEST_NEO4J=1 and SB_TEST_OLLAMA=1 for real worker recovery")
    await exercise(tmp_path, live=True, request=request)
