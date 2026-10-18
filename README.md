# Second Brain — local personal memory engine

An independent memory engine that decides how to adopt, correct, version, and retrieve
memories. It uses Neo4j as its database, Ollama for local inference, and the official MCP
SDK to communicate with an agent. It does not depend on Graphiti, Mem0, or other memory engines.

## Getting started

Requires **Docker with Compose v2** running (Docker Desktop or OrbStack on macOS).
Python, the MCP engine, Neo4j, and Ollama run in containers, so a host `.venv` is not required.
From the project directory:

```bash
./scripts/init.sh
docker compose up -d --build
docker compose ps -a
docker compose exec -T mcp second-brain status
```

`init.sh` uses a temporary Python container to create the vault and a `.env` file with a
random Neo4j password, distinct MCP bearer tokens, and `600` permissions. It preserves existing credentials and settings and
repairs vault permissions (`700` directories, `600` files), rejects symlinks, and adds your UID/GID so that vault files belong to your user rather than root. It can be rerun.
Credentials and the vault are excluded from Git and the Docker build context.

Compose starts:

| Service | Purpose | Host address |
| --- | --- | --- |
| `mcp` | Memory engine and persistent MCP Streamable HTTP server | `http://127.0.0.1:8000/mcp` |
| `worker` | Automatic extraction, embedding and projection recovery | No exposed port |
| `queue-init` | Sets private recovery-volume ownership, then exits | No exposed port |
| `neo4j` | Graph, full-text search, and vector search | Browser `http://127.0.0.1:7474`, Bolt `127.0.0.1:7687` |
| `ollama` | Local extraction and embeddings | `http://127.0.0.1:11434` |
| `models-init` | Downloads missing models, then exits | No exposed port |

`mcp` and `worker` start once Neo4j and Ollama are healthy and initialization has completed.
It is normal for `models-init` and `queue-init` to show **Exited (0)**. Downloading models on the first run
may take several minutes; models already stored in the volume are reused.

```bash
docker compose logs -f models-init
docker compose logs --tail=100 mcp
curl http://127.0.0.1:8000/health
```

`/health` checks that the HTTP transport responds; `second-brain status` and `memory_status`
check the vault, database, and models. All published ports bind only to `127.0.0.1`.
Inside the container, the engine uses `bolt://neo4j:7687` and `http://ollama:11434` over the
Compose network. Use service names to reach other containers.

The default models are `LiquidAI/lfm2.5-2.6b:q4_k_m` for extraction and `embeddinggemma`
for embeddings. LFM's generation budget is 8,192 tokens (including thinking), with a
300-second HTTP timeout per model request, a 16,384-token context and four CPU threads.
Extraction may use a second model request to repair a rejected draft; allow a longer
timeout in MCP clients. See [local model operation](docs/local-model-operation.md).
No API key or paid API is required. Initial downloads need internet access; processing and
retrieval then use local services. On Apple Silicon, this Docker configuration runs Ollama
on the **CPU**. The chat model in LM Studio is separate from the models used by the engine
and can continue running in the native macOS application.
The small extraction model provides a starting point: evaluate accuracy and coverage
on your own notes. Automatic extraction uses deterministic source contracts for complete
names, relation direction, exact values, polarity, certainty and typed object references,
as well as evidence, identity, temporal and SHACL checks. It makes no AI-review call.
Unsupported or ambiguous language remains pending for retry/review. These conservative
rules cover a defined Italian/English subset, not arbitrary language understanding.
See [extraction quality](docs/extraction-quality.md) for supported forms and limits.
Explicit manual/agent submissions remain available for reviewed interpretations.
Automatic extraction uses a deterministic context and removes equivalent duplicate
claims for the same source fact. Saved memories are compared using object identities,
declared cardinality, source-supported preference representations and temporal overlap.
See [memory consistency](docs/memory-consistency.md) for conflict and context rules.

Models and resource settings are configurable in `.env` (see [.env.example](.env.example)).
On a fresh installation, copy `.env.example` to `.env` **before** running `init.sh`, which
replaces the placeholder password with a random one. With an existing database, edit the
current `.env` while preserving `NEO4J_PASSWORD`.
`SB_VAULT_PATH` specifies the host directory mounted at `/data/vault`: `init.sh` creates
`./vault`; create any alternative directory before starting the services. `SB_MCP_PORT`
changes the published HTTP port; update the client URL accordingly.
Apply changes to `.env` or the code with `docker compose up -d --build`.
Base images are pinned by digest, and runtime dependencies are pinned in `requirements-runtime.lock`.
The MCP container runs as an unprivileged user with a read-only root filesystem.
Inference defaults to four CPU threads; adjust this with `SB_MODEL_THREADS` for your hardware.
The context window and output limit are also configurable. Very long or dense notes may
exceed these limits and remain pending: split them before ingestion.

## Persistence, shutdown, and updates

The vault is a bind mount of the `vault/` directory, so Markdown files and the ledger remain
accessible from the host. Neo4j and Ollama use the `second-brain_neo4j_data`,
`second-brain_neo4j_logs`, and `second-brain_ollama_models` volumes. Persistent recovery jobs
use `second-brain_jobs_data`, shared by MCP and the worker. The Compose project name
is `second-brain`, allowing existing data and model volumes to be reused.

```bash
# Stop and restart while preserving files and volumes.
docker compose down
docker compose up -d --build

# After changing the engine code.
docker compose up -d --build mcp worker
```

For a normal shutdown, use `docker compose down` without `-v`; adding `-v` removes the Neo4j
volumes, recovery history and downloaded models. Before backing up the vault, stop processes
that can write to it, including the worker, MCP and stdio clients, and save the entire
directory, including `.memory/`. Preserve the jobs volume separately to retain retry/review
history; incomplete obligations can otherwise be rediscovered from the vault.
Resolve any port conflicts before starting the services.

## First use

A fictional example, kept in the `demo` scope:

```bash
docker compose exec -T mcp second-brain remember --file examples/note.md --title "Example trip" --scope demo
docker compose exec -T mcp second-brain recall "Who is joining the trip?" --scope demo
```

To use an explicit, reproducible extraction without chat model inference:

```bash
docker compose exec -T mcp second-brain remember --file examples/note.md --scope demo \
  --extraction examples/extraction.json --request-id example-en-1
```

`request-id` makes retries safe. Reusing it with different content is rejected.
Results contain source and claim IDs, quotes, conflicts, and warnings. The CLI returns `0`
on success, `2` for warnings or incomplete processing, and `1` for errors.
`stored_pending_extraction` means the text is saved, but the engine has not adopted an
extraction. `indexed` indicates successful indexing.

For a quick durable write without waiting for local inference, call MCP `remember` with
`background: true` and a stable `request_id`. Compose's persistent worker processes the
saved revision; the response returns `queued`, `source_id`, `revision` and `job_id`.
Poll the read-only `source_status` tool with that source ID and revision to inspect
adoption, retry details or `needs_review`. Queued originals are readable through
`read_source` but are not searchable claims yet. Background ingestion requires an enabled
queue and cannot be combined with a supplied structured extraction. The synchronous
default remains compatible with existing clients.

Generated TOML client configurations derive their tool timeout from `SB_MODEL_TIMEOUT`
(1,920 seconds at the default 300 seconds per model request). Regenerate them with
`python scripts/mcp_config.py` after changing that setting. Prefer background writes
when you need a short response. See [local model operation](docs/local-model-operation.md).

Retry a pending revision through the MCP `extract_source` tool with its `source_id` and
`revision`, or use the CLI `extract` command. This reuses the saved original and does not
create another revision. An optional reviewed `extraction` can complete it without Ollama.
`memory_status.pending_extraction` lists revisions still requiring processing.
`superseded_extraction` maps an unprocessed revision to a later indexed revision with the
same text, title, scope, kind, and observation time. Both originals remain readable.
Retrying such a revision returns `superseded` and the indexed revision's existing claim IDs;
it never recreates claims that were subsequently corrected or revised. A changed text or
observation time remains pending until processed independently.

Compose enables a persistent SQLite recovery queue on the `jobs_data` volume. The independent
worker discovers incomplete extraction, missing/incompatible embeddings and stale Neo4j or
Markdown views at startup and every 15 seconds. It retries failures with increasing delays,
resumes expired work after a restart and moves repeated failures to `needs_review` after five
attempts by default. `memory_status.recovery` reports counts and worker liveness;
`recovery_jobs` lists work and write-protected `retry_job` requeues a reviewed failure.
See [worker operation and recovery](docs/reliability-and-recovery.md).

The full demo stores fictional memories in the `demo` scope, changes a trip's destination,
and corrects Marco's participation:

```bash
docker compose exec -T mcp second-brain demo
```

The English demo uses new request IDs so that it can coexist with memories from the earlier
Italian demo. Existing saved sources retain their original language.

After bootstrapping and building, run an isolated demo without starting Neo4j or Ollama:

```bash
docker compose run --rm --no-deps mcp --vault /tmp/second-brain-demo --offline demo
```

Paths passed to the CLI are **container paths**. Examples are included in the image at
`/app/examples`; the host vault is available at `/data/vault`.
To import another note from the host, send it through stdin to a temporary container file:

```bash
docker compose exec -T mcp sh -c 'cat > /tmp/note.md' < /PATH/TO/note.md
docker compose exec -T mcp second-brain remember --file /tmp/note.md --scope personal
docker compose exec -T mcp rm /tmp/note.md
```

## How memory is organized

The [project documentation](docs/README.md) explains the architecture, storage, ingestion,
retrieval, temporal changes, reasoning and automatic recovery, with Mermaid flowcharts.

The engine also includes an extensible RDF/RDFS vocabulary with **56 domain classes and 99
properties**, SHACL validation and local OWL 2 RL reasoning. It reuses references from
Schema.org, PROV-O, SKOS, OWL-Time and ORG. The agent can inspect definitions with
`knowledge_schema`; `reason_memory` executes formal deductions with premise/source links.
Everything runs in the existing Python container. See the
[knowledge layer guide](docs/knowledge.md) for the catalog, RDF exports, validation,
reasoning examples, extension format and temporal safeguards.

```text
Agent / LM Studio
        ⇅ MCP HTTP or stdio through docker compose exec
mcp container: MemoryEngine
        ├── /data/vault ⇄ ./vault: original Markdown + JSONL ledger
        ├── ollama container: JSON extraction + embeddings
        └── neo4j container: graph + full-text and vector indexes
```

```text
vault/
  episodes/<source-id>.md                      # editable copies of notes
  entities/<entity-id>.md                      # derived, regenerable profiles
  .memory/
    vault-id                                  # stable vault namespace
    writer.lock                               # one writer at a time
    events.jsonl                              # adopted operations in a SHA-256 chain
    source-versions/<source-id>/<rev>-<hash>.md # immutable originals
    recovery/                                 # fragments from interrupted appends
```

A note can describe many entities and memories; a separate file for each attribute is not
required. Entity IDs are stable, types are open strings (`person`, `event`, `project`, etc.),
and predicates are extensible. Automatic adoption also requires registered signatures and
supported source contracts; manual submissions retain the explicit caller-assertion contract.
The extractor can propose an offered `existing_id`; the engine checks scope, exact name,
and type. It can complete a missing ID only for a unique exact offered identity. Known
homonyms are ambiguous, and similar names never authorize reuse. This can still create
duplicates. Use `find_entities`, `add_alias`, and
`merge_entities` for explicit identity decisions. Merges require the same scope and type,
reject cycles, and preserve original entity/claim IDs. Each alias/merge has a source, reason,
effective time, and recording time; historical queries do not use later identity decisions.
Aliases become searchable without automatically identifying homonyms as the same person.
The prompt includes at most fifty same-scope entities whose full names or approved aliases
are mentioned in the text. Similar names alone are excluded. Identity validation runs inside
the model's repair loop and again against the latest ledger before adoption. Archives with
many people or projects will need dedicated entity resolution search.

Each memory contains:

- A subject, predicate, value, and optional linked object.
- A scope (`personal`, `work`, `demo`, etc.) and specific context.
- An epistemic status: `explicit`, `proposed`, or `inferred`, and positive/negative polarity.
- A validity interval and recording time.
- A source, revision, hash, and quote with Unicode character offsets.
- A local embedding and model identity, when available.
- Extraction origin, extractor version, source hash, and, for local inference, prompt/schema
  hashes and an observed model digest when Ollama reports one. The observed digest is not a
  guarantee that a mutable model tag was unchanged during inference. Older records retain
  their original data and may have no provenance metadata.

`explicit` means that the source states something; it does not establish its real-world truth.
Proposals and inferences are excluded from normal retrieval; include them with `--include-uncertain`.
The model proposes JSON data; the engine checks its schema, quotes, references, and intervals.
A missing or ambiguous quote rejects the entire batch while preserving the original source.
For a schema, quote, reference, or identity error, the local extractor attempts one repair using the
validation message and bounded source-derived diagnostics (complete names, canonical
relations, roles and evidence). Narrow decimal/date/participation equivalences and
exact-name missing subject declarations can be normalized without another model call;
see [extraction quality](docs/extraction-quality.md). If repair fails, the batch remains
pending without being adopted.
Conflicting new facts coexist and are reported as conflicts rather than automatically
replacing one another. Detection compares the same subject, predicate, and context; it does
not perform universal semantic comparison between statements.

The graph contains `Entity`, `SourceRevision`, `Assertion` versions, `MemoryChange`, and
`IdentityChange`, and `MemoryMeta`. Relationships are `ABOUT`, `OBJECT`, `SUPPORTED_BY`, `TARGETS`, `REPLACEMENT`, and `MERGED_INTO`.
A memory is an intermediate node so that sources, time, and corrections can attach to the fact itself.

Logical example:

```text
[Marco] ← ABOUT ─ ["Marco is joining the trip"] ─ OBJECT → [Trip]
                              │
                         SUPPORTED_BY
                              ↓
                     [Markdown note, revision 1]

[Correction] ─ TARGETS → [old assertion]
      ├── REPLACEMENT → [new assertion]
      └── SUPPORTED_BY → [Markdown with the explicit correction]
```

## Corrections and time

These are two different operations, both of which append new data:

```bash
# Correct a mistaken memory, including the reconstruction of the past.
docker compose exec -T mcp second-brain correct CLAIM_ID \
  --value "Marco: yes" --statement "Marco is joining the trip." \
  --reason "The previous interpretation was incorrect"

# Record an actual change of plan from a specific point in time.
docker compose exec -T mcp second-brain revise CLAIM_ID \
  --value Catania --statement "The trip destination is Catania." \
  --effective-at "2026-10-14T10:00:00+02:00" --reason "New decision"
```

`correct` retracts the old assertion from the time the correction is recorded and creates a
replacement with the same validity interval unless explicit parameters override it.
`revise` closes the old fact's validity at the time of the change and introduces the new fact.
The previous version of the old interval remains available for historical queries.
`retract CLAIM_ID --reason ...` retracts without replacement; it **does not delete** originals.
These operations act on an individual memory, not the entire entity. Replacements preserve
both subject and linked object. Use `--polarity negative` (or the MCP `polarity` argument) to
explicitly deny a relation; otherwise a correction/update preserves the target's polarity.
Opposite polarities can conflict even when the value string is the same.

```bash
docker compose exec -T mcp second-brain find-entities "Marco" --scope personal
docker compose exec -T mcp second-brain add-alias ENTITY_ID "Alternative name" --reason "User supplied alias"
docker compose exec -T mcp second-brain merge-entities DUPLICATE_ID CANONICAL_ID --reason "Confirmed same person"
```

There are two time axes, with intervals inclusive at the start and exclusive at the end:

- `valid_from / valid_to`: when the fact was valid.
- `recorded_from / recorded_to`: when the system held that version.

The date of a future trip can be an attribute of the trip; it is not automatically the date
from which a decision made today becomes valid. Timestamps require a timezone.

```bash
docker compose exec -T mcp second-brain recall "trip destination" \
  --valid-at "2026-10-13T12:00:00+02:00" \
  --known-at "2026-10-15T12:00:00+02:00"
docker compose exec -T mcp second-brain explain CLAIM_ID
docker compose exec -T mcp second-brain history --scope personal
docker compose exec -T mcp second-brain read-source SOURCE_ID --revision 1
```

Editing a note in `episodes/` does not silently change a memory. Register the edit with
`refresh SOURCE_ID` or `remember --source-id SOURCE_ID --file ...`.
The new Markdown becomes a new revision. Extracting a new revision does not automatically
retract facts from previous revisions: use `correct`, `revise`, or `retract` to choose what
to replace. `entities/` is a generated audit view; use `recall` for the state valid at a given time.

## Retrieval, availability, and recovery

Retrieval combines full-text search, semantic search, and graph relationships. Candidates
are filtered along both time axes before scoring or graph expansion and before forming the
agent's context. Retracted or uncertain facts cannot seed ordinary historical graph searches.
Cosine similarity over visible versions prevents numerous historical nodes from hiding
valid memories within the vector index's candidate limit. If services are unavailable,
canonical text search is used with a warning. The budget is estimated at four characters
per token: evidence quotes are never truncated, although an oversized memory may be excluded. Conflict
candidates are grouped once, and only whole results that fit the limit/budget read and verify
originals. Sources shared by selected results are verified once per request, never cached
across requests; tampering is still detected before returning evidence.
Neo4j search also expands candidates through shared entities (`ABOUT`/`OBJECT`), allowing
a question about a person to retrieve the context of an event they participate in.

A commit is written to the JSONL ledger **before** updating the graph. There is no distributed
transaction between the filesystem and Neo4j. File locks, fsync, atomic writes, and
idempotent IDs protect retries, including explicit observed timestamps. Ollama and Neo4j I/O
runs outside the canonical writer lock. Changes revalidate their target version at commit
and reject concurrent edits rather than overwriting them. Embeddings are generated in bounded
batches; concurrent new claims are left for the next `reembed`. A failed derived Markdown
write returns the durable IDs with a warning and can be repaired by retry/replay. An interrupted final-line write is saved in `recovery/`
before restoring the boundary of the last complete event. A corrupted complete line or
broken hash chain stops the engine. The chain detects accidental alterations; it is not
a cryptographic signature against someone who can rewrite the entire vault.

Neo4j stores an incremental projection identified by `vault_id`, event sequence, and ledger
hash. A transaction updates touched entities, source revisions, assertion versions, and
changes while retaining other vaults. A metadata lock serializes concurrent projections;
an older snapshot cannot roll back a newer head. `replay` can rebuild the full projection,
and failures roll back the database transaction.

The engine caches verified events and immutable state snapshots by ledger file signature
and hash. Unchanged reads avoid JSON/vector decoding; changed files verify the cached prefix
before incrementally validating new events. A cold process still validates the entire log.
Derived profiles use a separate view lock and persisted head checkpoint, then regenerate
only affected entities from the newest snapshot. Editable episode files are never overwritten
by view regeneration. `replay` repairs all profiles without a model.
Canonical visible-vector cosine retrieval remains O(N × dimensions), and snapshots/indexes
reside in RAM; very large archives will still need additional retrieval/storage work.

```bash
# Reuse adopted ledger data without new AI extraction.
docker compose exec -T mcp second-brain replay

# Retry a saved source after an Ollama failure.
docker compose exec -T mcp second-brain extract SOURCE_ID --revision 1

# Regenerate missing embeddings or rebuild them after a model or dimension change.
docker compose exec -T mcp second-brain reembed

# Evaluate the local extractor on six fictional English/Italian semantic probes.
# Does not adopt any evaluation memories in the vault or graph.
docker compose exec -T mcp second-brain evaluate
```

Summaries and contexts are composed from retrieved memories without storing an independent
copy that could become stale after a correction. Version 1 does not yet cache AI-generated
summaries with transitive invalidation.

## MCP connection

The primary transport is **Streamable HTTP**, with a persistent server in the `mcp` service.
With Compose running, clients that use `mcpServers` can use [mcp.http.json](mcp.http.json):

```json
{
  "mcpServers": {
    "second-brain": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer REPLACE_WITH_YOUR_LOCAL_TOKEN"}
    }
  }
}
```

In LM Studio, open **Program → Install → Edit mcp.json** and add `second-brain` to the
`mcpServers` object, preserving other configured servers. The chat model must support tool
calls. HTTP requires the bearer token generated by `init.sh`. `SB_MCP_WRITE_TOKEN` grants
reads and writes; `SB_MCP_READ_TOKEN` grants reads only. Host/Origin protection remains enabled.
The server limits request bodies (default 2 MiB), concurrent MCP requests (8), and HTTP requests
in a sliding 60-second window (300 by default); tune these through `.env`. MCP initialization,
notifications, and tool discovery also use HTTP requests. Budgets are separated by verified
credential role and client IP, so unauthenticated traffic does not consume the owner's rate
budget. Throttled requests do not consume rate capacity. A `429` identifies
`rate_limit_exceeded` or `concurrency_limit_exceeded` and includes `Retry-After` (the remaining
rate-window delay or one second for concurrency). Clients should respect it before retrying;
use the same `request_id` for write retries. Existing explicit limits in `.env` take precedence.
`memory_status.http_limits` reports the configured limits. `/health` remains public liveness.
An HTTP server without an owner credential fails closed. This is owner-managed local bearer
authentication, without an external OAuth provider.

```bash
# Requires Docker only; creates private files in ignored .runtime/.
./scripts/mcp-config.sh
./scripts/mcp-config.sh --read-only
```

Copy `.runtime/mcp.owner.json` into an HTTP client's configuration, or use the read-only
variant for an agent that should only retrieve memories. These files contain credentials:
keep them private. Never copy a real token into the public example files.
For Codex, copy `.runtime/mcp.owner.toml` into your private configuration, preserving other
settings. [mcp.codex.toml](mcp.codex.toml) instead demonstrates `bearer_token_env_var`; it
requires exporting the credential in the environment inherited by Codex. The private snippet
uses `http_headers`, as described in the [official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
After changing client credentials or adding tools, reload the client's MCP connection.
Rotating a token means replacing it in `.env`, recreating the MCP service, and regenerating
private client snippets.

For clients that require **stdio**, use [mcp.docker.example.json](mcp.docker.example.json),
adapting the Docker executable path (`command -v docker`) and project path:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/PATH/TO/docker",
      "args": [
        "compose", "--project-directory", "/PATH/TO/second_brain",
        "exec", "-T", "mcp", "second-brain", "serve", "--transport", "stdio"
      ]
    }
  }
}
```

`exec -T` starts an MCP stdio process **inside the running container** without a terminal
interfering with the protocol. It inherits service connections and shares the HTTP server's
vault. An absolute project path avoids relying on the client's working directory.
An optional `mcp.local.json` with actual paths is excluded from Git. Replace any previous
client configuration that launched `.venv/bin/second-brain` with the Docker configuration.
Keep the Neo4j password out of MCP client configuration.
The snippet generator preserves client configurations by default. For an explicit local
Codex installation, `python3 scripts/mcp_config.py --install-codex` updates only the matching
server entry and keeps a private backup; this option requires host Python 3.11+.

A first prompt to check retrieval without adding memories:

> Use `memory_status` to check the second brain, then `recall` in the `demo` scope.
> Show the sources of the retrieved memories.

Exposed tools: `remember`, `extract_source`, `recall`, `read_source`, `correct`, `revise`, `retract`,
`explain`, `history`, `find_entities`, `add_alias`, `merge_entities`, `memory_status`,
`knowledge_schema`, `validate_knowledge`, `export_rdf`, `reason_memory`, `recovery_jobs`, and
`retry_job`. The `memory://source/SOURCE_ID/REVISION` resource
and `memory_policy` prompt are also available.
Maintenance operations (`replay`, `reembed`) use the CLI; no tool exposes arbitrary Cypher.

MCP does not automatically capture every conversation: the client or agent must call
`remember` when there is something to save and `recall` when context is needed. Retrieved
documents are data, even when they contain instructions; the agent produces the final answer.
Scopes are organizational filters, **not authorization boundaries between different users**.

Semantic retrieval can return similar events in the same scope. To ask about one precise
event or person, call `find_entities` and select the intended ID, then pass `entity_id` to
`recall` (CLI: `--entity-id`). This restricts subject/object matches before ranking and graph
expansion, and respects aliases, approved merges, `valid_at`, and `known_at`. Unknown IDs and
IDs outside the supplied scope are rejected. A broad query has no identity filter.

## Scope and limitations

The vault is stored in plaintext. For personal data, use disk encryption and back up the
entire vault, including `.memory/`. A Neo4j dump is useful but does not replace the originals.
Version 1 is intended for a single owner on a local machine. It does not include automatic
capture from apps, OCR or audio ingestion, notifications, multi-device synchronization,
a remote OAuth flow, automatic identity matching, or coordinated physical deletion.
These are future modules, not implicit engine behavior.

Technical references: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[embeddings](https://docs.ollama.com/capabilities/embeddings),
[Neo4j Python driver](https://neo4j.com/docs/api/python-driver/current/), and
[MCP Python SDK, v1 branch](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
The MCP SDK is constrained to major version 1; migration to v2 will require checking contracts.
`requirements-runtime.lock` pins runtime versions; `requirements.lock` also includes
development dependencies. [Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)
describes waiting for healthy services and successful initialization.
