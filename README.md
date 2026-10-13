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
random password and `600` permissions. It preserves existing credentials and settings and
adds your UID/GID so that vault files belong to your user rather than root. It can be rerun.
Credentials and the vault are excluded from Git and the Docker build context.

Compose starts:

| Service | Purpose | Host address |
| --- | --- | --- |
| `mcp` | Memory engine and persistent MCP Streamable HTTP server | `http://127.0.0.1:8000/mcp` |
| `neo4j` | Graph, full-text search, and vector search | Browser `http://127.0.0.1:7474`, Bolt `127.0.0.1:7687` |
| `ollama` | Local extraction and embeddings | `http://127.0.0.1:11434` |
| `models-init` | Downloads missing models, then exits | No exposed port |

`mcp` starts once Neo4j and Ollama are healthy and `models-init` has completed successfully.
It is normal for `models-init` to show **Exited (0)**. Downloading models on the first run
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

The default models are `qwen2.5:1.5b` for extraction and `embeddinggemma` for embeddings.
No API key or paid API is required. Initial downloads need internet access; processing and
retrieval then use local services. On Apple Silicon, this Docker configuration runs Ollama
on the **CPU**. The chat model in LM Studio is separate from the models used by the engine
and can continue running in the native macOS application.
The small extraction model provides a starting point: evaluate its semantic accuracy and
entity resolution on your own examples. Quote validation checks the evidence text, not the
meaning of the model's interpretation.

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
`second-brain_neo4j_logs`, and `second-brain_ollama_models` volumes. The Compose project name
is `second-brain`, allowing existing data and model volumes to be reused.

```bash
# Stop and restart while preserving files and volumes.
docker compose down
docker compose up -d --build

# After changing the engine code.
docker compose up -d --build mcp
```

For a normal shutdown, use `docker compose down` without `-v`; adding `-v` removes the Neo4j
volumes and downloaded models. Before backing up the vault, stop processes that can write to
it, including stdio clients, and save the entire directory, including `.memory/`.
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
and predicates are extensible. To reuse an identity, the model must provide `existing_id`;
the engine checks its scope, name, and type. A matching name alone does not merge entities.
This conservative approach may create duplicates; guided alias and merge management is future work.
The prompt includes at most fifty entities from the same scope, prioritizing names found in
the text. Archives with many people or projects will need dedicated entity resolution search.

Each memory contains:

- A subject, predicate, value, and optional linked object.
- A scope (`personal`, `work`, `demo`, etc.) and specific context.
- An epistemic status: `explicit`, `proposed`, or `inferred`.
- A validity interval and recording time.
- A source, revision, hash, and quote with Unicode character offsets.
- A local embedding and model identity, when available.

`explicit` means that the source states something; it does not establish its real-world truth.
Proposals and inferences are excluded from normal retrieval; include them with `--include-uncertain`.
The model proposes JSON data; the engine checks its schema, quotes, references, and intervals.
A missing or ambiguous quote rejects the entire batch while preserving the original source.
For a schema, quote, or reference error, the local extractor attempts one repair using the
validation message. If that fails, the batch remains pending without being adopted.
Conflicting new facts coexist and are reported as conflicts rather than automatically
replacing one another. Detection compares the same subject, predicate, and context; it does
not perform universal semantic comparison between statements.

The graph contains `Entity`, `SourceRevision`, `Assertion` versions, `MemoryChange`, and
`MemoryMeta`. Relationships are `ABOUT`, `OBJECT`, `SUPPORTED_BY`, `TARGETS`, and `REPLACEMENT`.
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
These operations act on an individual memory, not the entire entity.

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
are filtered along both time axes before forming the agent's context.
Cosine similarity over visible versions prevents numerous historical nodes from hiding
valid memories within the vector index's candidate limit. If services are unavailable,
canonical text search is used with a warning. The budget is estimated at four characters
per token: evidence quotes are never truncated, although an oversized memory may be excluded.
Neo4j search also expands candidates through shared entities (`ABOUT`/`OBJECT`), allowing
a question about a person to retrieve the context of an event they participate in.

A commit is written to the JSONL ledger **before** updating the graph. There is no distributed
transaction between the filesystem and Neo4j. File locks, fsync, atomic writes, and
idempotent IDs protect retries. An interrupted final-line write is saved in `recovery/`
before restoring the boundary of the last complete event. A corrupted complete line or
broken hash chain stops the engine. The chain detects accidental alterations; it is not
a cryptographic signature against someone who can rewrite the entire vault.

Neo4j stores a projection identified by `vault_id` and the ledger hash. A new projection
replaces the old one in a single Neo4j transaction; other vaults and database data remain
separate. A failure preserves the previous projection and is reported.
In this version, every new commit rebuilds the vault projection: this is simple but costs
O(N) and will need incremental updates for large archives. Replay and canonical search
also load the ledger into RAM.

```bash
# Reuse adopted ledger data without new AI extraction.
docker compose exec -T mcp second-brain replay

# Retry a saved source after an Ollama failure.
docker compose exec -T mcp second-brain extract SOURCE_ID --revision 1

# Regenerate missing embeddings or rebuild them after a model or dimension change.
docker compose exec -T mcp second-brain reembed
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
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

In LM Studio, open **Program → Install → Edit mcp.json** and add `second-brain` to the
`mcpServers` object, preserving other configured servers. The chat model must support tool
calls. The endpoint is local and has no remote authentication; Host/Origin protection
remains enabled inside the container.

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
The project does not automatically modify a client's global configuration.

A first prompt to check retrieval without adding memories:

> Use `memory_status` to check the second brain, then `recall` in the `demo` scope.
> Show the sources of the retrieved memories.

Exposed tools: `remember`, `recall`, `read_source`, `correct`, `revise`, `retract`,
`explain`, `history`, and `memory_status`. The `memory://source/SOURCE_ID/REVISION` resource
and `memory_policy` prompt are also available.
Maintenance operations (`replay`, `reembed`) use the CLI; no tool exposes arbitrary Cypher.

MCP does not automatically capture every conversation: the client or agent must call
`remember` when there is something to save and `recall` when context is needed. Retrieved
documents are data, even when they contain instructions; the agent produces the final answer.
Scopes are organizational filters, **not authorization boundaries between different users**.

## Scope and limitations

The vault is stored in plaintext. For personal data, use disk encryption and back up the
entire vault, including `.memory/`. A Neo4j dump is useful but does not replace the originals.
Version 1 is intended for a single owner on a local machine. It does not include automatic
capture from apps, OCR or audio ingestion, notifications, multi-device synchronization,
remote authentication, guided entity merging, or coordinated physical deletion.
These are future modules, not implicit engine behavior.

Technical references: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[embeddings](https://docs.ollama.com/capabilities/embeddings),
[Neo4j Python driver](https://neo4j.com/docs/api/python-driver/current/), and
[MCP Python SDK, v1 branch](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
The MCP SDK is constrained to major version 1; migration to v2 will require checking contracts.
`requirements-runtime.lock` pins runtime versions; `requirements.lock` also includes
development dependencies. [Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)
describes waiting for healthy services and successful initialization.
