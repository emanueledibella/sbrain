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
| [Storage and data model](storage-and-data-model.md) | The vault, originals, event replay and state reconstruction, claims, evidence and Neo4j assertion versions |
| [Memory ingestion](ingestion.md) | The complete `remember` flow, exact validation checks, temporary RDF, embeddings, ledger adoption and Neo4j writes |
| [Memory retrieval](retrieval.md) | The complete `recall` flow, vector fundamentals and learning resources, temporal filters, graph expansion, scoring and source verification |
| [Temporal memory and corrections](temporal-memory-and-corrections.md) | Valid time, knowledge time, corrections, real-world changes, retractions and identity history |
| [Reasoning and agent decisions](reasoning.md) | Who requests deductions, where OWL executes and how conclusions retain their premises |
| [Reliability and recovery](reliability-and-recovery.md) | SQLite jobs, restart recovery, automatic retries, `needs_review`, configuration and operator commands |
| [Knowledge vocabulary reference](knowledge.md) | RDF/RDFS, reused vocabularies, SHACL shapes, OWL profile, exports and vocabulary extensions |
| [Extraction quality](extraction-quality.md) | Deterministic source contracts, complete identities, typed object references, temporal anchors and acceptance limits |
| [Local model operation](local-model-operation.md) | LFM defaults, container updates, generation budgets, client timeouts and host sleep |
| [LFM deployment verification](benchmarks/2026-10-17-lfm-deployment.md) | New Italian memories over real MCP, remaining coverage limits, source/graph integrity and timeout investigation |
| [Preference recovery verification](benchmarks/2026-10-17-preference-recovery.md) | Literal/object preference contracts, recovery of the same saved revision, concurrent retries and duplicate/integrity checks |
| [Qwen/LFM regression results](benchmarks/2026-10-16-deterministic-extraction.md) | Measured errors, fact coverage, latency, RAM and typed edges with deterministic controls |
| [Qwen/LFM coverage improvements](benchmarks/2026-10-16-coverage-extraction.md) | False-rejection fixes, repeated matched benchmark, remaining failures and measured costs |
| [Automated tests and CI](testing.md) | Worker outage, retry, restart, review and deduplication checks; full-suite GitHub Actions with real Ollama and Neo4j |

## Common implementation questions

| Question | Explanation |
| --- | --- |
| What do quote, identity and date checks actually verify? | [Concrete validation checks](ingestion.md#evidence-identity-and-time-checks) |
| Where does RDF enter the extraction pipeline? | [Temporary RDF and SHACL validation](ingestion.md#rdf-and-shacl-during-validation) |
| What happens after validation, and is the claim saved in Neo4j? | [Ledger commit, graph writes and failure outcomes](ingestion.md#7-update-projections-and-report-the-outcome) |
| What does reconstructing memory state mean? | [Event replay, versions, temporal filtering and caching](storage-and-data-model.md#reconstructing-state-from-events) |
| How do vectors work, and where can I learn more? | [Cosine similarity and primary learning resources](retrieval.md#how-vectors-are-compared) |
| How are text, vector and graph results combined? | [Neo4j search](retrieval.md#4-search-neo4j-and-expand-through-shared-entities) and [canonical scoring](retrieval.md#5-rank-canonical-candidates) |
| Does OWL reasoning run after search on its results? | [Separate retrieval and reasoning paths](reasoning.md#where-reasoning-fits-in-the-workflow) |

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
