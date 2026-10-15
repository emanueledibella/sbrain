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
| [Reliability and recovery](reliability-and-recovery.md) | Current retries, incomplete processing, operator commands and the proposed persistent worker |
| [Knowledge vocabulary reference](knowledge.md) | RDF/RDFS, reused vocabularies, SHACL shapes, OWL profile, exports and vocabulary extensions |

## Implemented behavior and proposed work

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

**Deferred recovery is not implemented as an autonomous worker.** A failed extraction can
remain pending, and missing embeddings require an explicit maintenance operation. The
[recovery page](reliability-and-recovery.md) separates current behavior from the proposed
persistent queue and worker. That proposal must not be read as an existing feature.

## Diagrams

The pages contain Mermaid flowcharts and plain-text diagrams. GitHub renders the Mermaid
blocks directly; Markdown viewers without Mermaid support still display their source.

Documentation files are public project material. Personal sources, credentials, local
exports and runtime files belong in the ignored vault or `.runtime/`, not in `docs/`.
