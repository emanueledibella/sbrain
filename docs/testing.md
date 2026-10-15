# Automated worker recovery tests

[Documentation index](README.md)

The complete fault/restart/review scenario is part of the pytest suite in
[`test_worker_e2e.py`](../tests/test_worker_e2e.py). It uses the actual MCP HTTP transport,
`MemoryEngine`, SQLite queue, local worker loop and durable vault storage in separate
application processes. The `worker_e2e` marker selects both variants of this scenario.

## Two variants

| Test | Dependencies | When it runs |
| --- | --- | --- |
| `test_worker_recovery_faults_restarts_and_review` | A controlled local HTTP Ollama substitute and file-backed projection double | Every ordinary pytest run |
| `test_live_worker_recovery_faults_restarts_and_review` | Real Ollama models and Neo4j; a controlled failing endpoint during the outage | Automatically in the Docker test service; otherwise requires `SB_TEST_OLLAMA=1` and `SB_TEST_NEO4J=1` |

The deterministic variant checks recovery behavior without depending on language-model
output or installed services. Its substitute implements the actual Ollama HTTP response
formats, returns HTTP 503 during the injected outage, and counts successful extractions.
The live variant uses the configured local extraction and embedding models after recovery
and verifies Neo4j's actual projection checkpoint.

The integration variant is skipped outside an explicitly enabled live environment. A
skip is not evidence that real services passed. Compose sets both integration flags and
waits for Neo4j, Ollama and model initialization before starting its test runner.

## Scenario and assertions

Both tests follow the same sequence:

1. Start an isolated MCP server with Ollama unavailable. Save an unstructured note for
   automatic extraction and a structured note to isolate missing-embedding recovery.
2. Verify that both originals are durable, with one source pending extraction and one
   adopted claim missing its vector.
3. Start the worker with failing dependencies. Observe `retry_wait`, attempt counts and
   the scheduled retry delay, rather than merely inspecting the final result.
4. Kill and recreate the worker during the outage. Wait for extraction, embedding and
   projection jobs to reach `needs_review` after the test-specific two-attempt limit.
5. Kill and recreate it again. Wait for a completed reconciliation and verify that job IDs,
   review states, counters and canonical data remain unchanged.
6. Restore dependencies, restart the MCP process and explicitly requeue reviewed jobs
   through the authenticated `retry_job` tool.
7. Pause the worker immediately after committing a successful extraction but before job
   acknowledgment. Verify the durable event and running job, then kill the process.
8. Start a fresh worker and let the lease expire. Verify all three jobs finish, without a
   second extraction event for either source, a second embedding repair event, extra source
   revisions or changed claim IDs.
9. Retrieve both memories through MCP and verify values and cited source IDs, read back
   exact originals, run SHACL validation and check the final projection.
10. Repeat the original `remember` calls with the same request IDs and verify the canonical
    snapshot does not change. The deterministic variant also requires exactly one successful
    model extraction across the interrupted and resumed executions.

These are forced process deaths and fresh application processes, not a mocked restart
method or a direct update of queue state. In Docker, the subprocesses run inside the test
container: the suite does not need the host Docker socket or stop deployment containers.
It exercises process-restart persistence; the separate manual container-recreation check
also covered Docker volume mounts and ownership. The automated tests do not claim to
remove/recreate the entire Docker daemon or simulate physical disk loss.

## Run with Docker Compose

Build the test image after changing tests, then run only this scenario:

```bash
docker compose build tests
docker compose --profile test run --rm tests -m pytest -q -p no:cacheprovider -m worker_e2e
```

This runs both the deterministic and real-service variants. The first run can download
the configured models. Existing model volumes are reused.

The normal full-suite command also includes the new tests automatically:

```bash
docker compose --profile test run --rm tests
```

For just the deterministic scenario inside Docker, excluding other integration tests:

```bash
docker compose --profile test run --rm --no-deps tests -m pytest -q -p no:cacheprovider \
  -m 'worker_e2e and not integration'
```

This last command needs the built test image but does not start Ollama or Neo4j.

## Run in a local development environment

With project development dependencies installed:

```bash
python -m pytest -q -m 'worker_e2e and not integration'
```

To run the live variant locally, start the services and provide the usual Neo4j and Ollama
settings through the environment or the local `.env`, then opt in:

```bash
SB_TEST_NEO4J=1 SB_TEST_OLLAMA=1 python -m pytest -q -m worker_e2e
```

All model and database endpoints are taken from the existing configuration. The tests
replace vault, queue and MCP credentials with disposable values. They do not read or write
the deployment's canonical vault or reuse a private vocabulary extension.

## Diagnostics and isolation

Each run creates its own temporary vault, queue, scope, MCP port, credentials and process
probe files. Waits poll actual state and reconciliation/commit markers with deadlines;
they do not depend on fixed sleeps to guess whether a phase has finished. The test uses
shorter retry/lease settings, without changing production defaults.

Subprocess logs and `recovery-report.json` are stored under pytest's temporary test
directory. On failure, pytest includes the recorded recovery stages in the failure report.
The report contains safe job metadata and canonical snapshots, not credentials or real
source content.

To retain reports after the disposable test container exits:

```bash
mkdir -p .runtime/test-results
chmod 700 .runtime/test-results
docker compose --profile test run --rm \
  -v "$PWD/.runtime/test-results:/results" \
  tests -m pytest -q -p no:cacheprovider -m worker_e2e \
  --basetemp=/results/worker-recovery --junitxml=/results/worker-recovery.xml
```

Use a dedicated `--basetemp` directory: pytest recreates it on the next run. Keep these
artifacts in the ignored `.runtime/` directory rather than committing local runtime data.
The command returns a nonzero exit status on failure and can be used directly in CI.

Cleanup runs even when an assertion fails: child processes are killed and awaited, HTTP
substitutes and sockets are closed, and the live variant deletes only nodes carrying its
random test vault ID. Other vaults, service configuration and the deployment worker remain
untouched. See [recovery operation](reliability-and-recovery.md) for production settings.
