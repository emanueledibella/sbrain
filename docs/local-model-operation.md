# Local extraction model and operation

The extraction default is `LiquidAI/lfm2.5-2.6b:q4_k_m`, served by Ollama in Docker.
`embeddinggemma` still generates the 768-dimensional retrieval vectors. Changing the
extraction model does not require replacing or regenerating existing embeddings.

## Configuration

Python defaults, Compose and `.env.example` use the same settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `SB_CHAT_MODEL` | `LiquidAI/lfm2.5-2.6b:q4_k_m` | Free-text extraction model |
| `SB_EMBEDDING_MODEL` | `embeddinggemma` | Retrieval embedding model |
| `SB_MODEL_THREADS` | `4` | Inference CPU threads |
| `SB_MODEL_CONTEXT` | `16384` | Context token budget |
| `SB_MODEL_OUTPUT_TOKENS` | `8192` | Maximum generated tokens, including thinking |
| `SB_MODEL_TIMEOUT` | `300` | HTTP timeout in seconds per Ollama request |

These are CPU settings, including on Apple Silicon when using this Compose deployment.
The previous matched benchmark measured approximately 3.15 GiB peak Ollama RSS for LFM
on its isolated runner. This is a measurement, not the total host memory requirement:
allow additional memory for embeddings, Neo4j, Python, Docker and the operating system.
See the [benchmark report](benchmarks/2026-10-16-coverage-extraction.md) for its conditions.

An existing `.env` overrides defaults. Preserve its credentials and edit the three
extraction settings when migrating from Qwen:

```dotenv
SB_CHAT_MODEL=LiquidAI/lfm2.5-2.6b:q4_k_m
SB_MODEL_OUTPUT_TOKENS=8192
SB_MODEL_TIMEOUT=300
```

Then run:

```bash
docker compose build mcp
docker compose run --rm --no-deps models-init
docker compose up -d --no-deps --wait mcp worker
docker compose exec mcp second-brain status
docker compose exec worker second-brain worker-health
```

The services must already be running for this update procedure. For a fresh installation
use the [README startup instructions](../README.md). Both MCP and worker use the rebuilt
`second-brain:local` image. Existing vault files, the SQLite queue and Neo4j volumes persist.
No database reset or embedding migration is necessary.

`memory_status.extraction` reports the configured model, source-contract version, context
and generation budgets, and request timeout. `memory_status.ollama.models_ready` checks
that both configured models are installed. Inspect an adopted claim with `explain` to
see its actual model provenance and deterministic quality checks. Old claims retain their
original provenance and are not automatically re-extracted after a model change.

## Request timeouts and recovery

`remember` performs extraction synchronously. A rejected draft can trigger one repair
request, followed by a separate embedding request. The 300-second timeout applies to
each HTTP request; it is not a 300-second deadline for the complete MCP operation.
Use a client timeout long enough for the operation (for example, 930 seconds for two
chat calls and one embedding call), and allow additional time for contention. A timeout
is an inactivity limit enforced by HTTPX; it is not a strict wall-clock execution cap.

On a CPU, LFM can take tens of seconds and a repaired extraction can take substantially
longer. Keep the host awake while using the service. Closing a MacBook lid can suspend
Docker and interrupt inference; `restart: unless-stopped` cannot run services while the
host sleeps. For a bounded macOS test, `caffeinate -i <test-command>` prevents idle sleep
while that command runs. It does not guarantee continued operation with the lid closed
or change permanent power settings. Use an always-on host for unattended operation.

In the October 16 benchmark timeout, macOS recorded **Clamshell Sleep at 15:24:12** and
a full wake at **23:22:48 Europe/Rome**. The failed Ollama request resumed/logged its
failure at **21:22:48 UTC**, the same instant. Replaying its exact request body with the
same model and generation options on the awake deployment completed in **54.45 seconds**
(2,392 generated tokens, normal `stop`). The replay used the existing deployment Ollama
container, rather than the old resource-limited benchmark container. This identifies
sleep as the interruption in that incident; it does not guarantee every future request
will finish within the configured timeout.

When extraction fails or a draft cannot pass validation, the source remains durable and
the worker retries the saved revision. After the retry budget is exhausted the job enters
`needs_review`. Inspect `recovery_jobs` and its last error, resolve the underlying issue,
then use `retry_job`; do not submit a new source just to retry extraction. A client timeout
does not prove the server failed to commit: reuse the same `request_id` and inspect the
saved source/status before retrying. See [recovery](reliability-and-recovery.md).

## Acceptance policy

The deployed model makes proposals; deterministic source contracts, identity/evidence
checks, temporal validation and SHACL decide whether the engine adopts them. LFM does
not bypass these checks. Unsupported or ambiguous language can still remain pending.
`explicit` records what a source says, not independently verified truth. Reviewed
structured submissions have their distinct manual/agent provenance and do not claim to
have passed the automatic language contracts. See [extraction quality](extraction-quality.md).
