# Project documentation

This documentation describes the memory engine as implemented in the repository. It
organizes the explanations of its components, storage, ingestion, retrieval, reasoning
and recovery by topic. Examples use fictional people, trips and decisions.

Start with the [root README](../README.md) to configure Docker Compose, start the services
and connect an MCP client. Read the following pages to understand what happens afterward.

## Reading order

| Topic | Contents |
| --- | --- |
| [Architecture and components](architecture.md) | Docker services, the MCP boundary, `MemoryEngine`, local models and component responsibilities |
| [Storage and data model](storage-and-data-model.md) | The vault, immutable Markdown originals, event ledger, entities, claims, evidence and the Neo4j graph |
| [Memory ingestion](ingestion.md) | The complete `remember` flow, identity resolution, validation, embeddings and adoption |
| [Memory retrieval](retrieval.md) | The complete `recall` flow, temporal filters, vector similarity, graph expansion, ranking and source verification |
| [Temporal memory and corrections](temporal-memory-and-corrections.md) | Valid time, knowledge time, corrections, real-world changes, retractions and identity history |
| [Reasoning and agent decisions](reasoning.md) | Who requests deductions, where OWL executes and how conclusions retain their premises |
| [Reliability and recovery](reliability-and-recovery.md) | SQLite jobs, restart recovery, automatic retries, `needs_review`, configuration and operator commands |
| [Knowledge vocabulary reference](knowledge.md) | RDF/RDFS, reused vocabularies, SHACL shapes, OWL profile, exports and vocabulary extensions |

## Implemented behavior

The system saves original sources before calling a model or graph database. It preserves
source revisions and the history of adopted operations. Neo4j and generated Markdown
profiles are derived views; RDF snapshots are built on demand.

Three operations have different responsibilities:

```text
remember       → save a source, extract and adopt supported claims
recall         → retrieve recorded claims using text, vectors and graph proximity
reason_memory  → derive supported conclusions from a selected temporal snapshot
```

OWL reasoning is implemented, but the agent or another caller must explicitly request it.
Neither `remember` nor `recall` automatically executes `reason_memory`.

**Compose includes an autonomous recovery worker and a persistent SQLite queue.** It
reconciles the ledger at startup and periodically, retries unfinished extraction, repairs
missing/incompatible embeddings and updates projections without depending on a chat agent.
Repeated failures become visible `needs_review` jobs. Outside Compose, recovery is opt-in
through `SB_WORKER_QUEUE_PATH` and the `worker` command. See the
[recovery page](reliability-and-recovery.md) for configuration and restart semantics.

## Diagrams

The pages contain Mermaid flowcharts and plain-text diagrams. GitHub renders the Mermaid
blocks directly; Markdown viewers without Mermaid support still display their source.

Documentation files are public project material. Personal sources, credentials, local
exports and runtime files belong in the ignored vault or `.runtime/`, not in `docs/`.
