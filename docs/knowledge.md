# Knowledge vocabulary, RDF, SHACL and OWL

[Documentation index](README.md) · [Reasoning execution and agent decisions](reasoning.md)

The memory engine includes an offline knowledge application profile with **56 domain classes
and 99 properties**. It covers people, contacts, organizations, work, projects, tasks,
decisions, commitments, daily activities, events, travel, education, documents, websites,
software, expenses, health notes, preferences and abstract concepts.

Markdown originals and the append-only event ledger remain authoritative. RDF and Neo4j
are projections. Existing memory IDs, corrections and source revisions are not rewritten
when the vocabulary is installed or extended.

## Reused vocabularies

| Vocabulary | Use in this application profile | Reference |
| --- | --- | --- |
| Schema.org | Public classes for people, events, places, documents, products, courses and software | https://schema.org/docs/schemas.html |
| PROV-O | Sources and claim versions as provenance entities; `prov:wasDerivedFrom` links | https://www.w3.org/TR/prov-o/ |
| SKOS | Bilingual preferred labels, aliases and conceptual relationship mappings | https://www.w3.org/TR/skos-reference/ |
| OWL-Time | Valid-time and knowledge-time intervals with explicit beginnings and optional ends | https://www.w3.org/TR/owl-time/ |
| ORG | Organizational units and roles; references for membership properties | https://www.w3.org/TR/vocab-org/ |

The application namespace is `urn:sbrain:vocab:`. It requires no public domain or network
service. Domain classes specialize suitable external classes with `rdfs:subClassOf`.
Related external properties are documented with `rdfs:seeAlso`: an approximate mapping
does not claim logical equivalence. For example, the subject/object direction of a
participation claim differs from Schema.org's `attendee` property.

The project distributes its own profile and original descriptions, not complete copies
of these external ontologies. There is no automatic `owl:imports`, remote JSON-LD context,
SPARQL endpoint or ontology download during memory operations. See
[vocabulary notices](../THIRD_PARTY_VOCABULARIES.md) for attribution and upstream terms.

The complete machine-readable catalog is
[`catalog.json`](../src/second_brain/knowledge/catalog.json). Generated OWL axioms include
inverse, symmetric and transitive object properties, plus class hierarchies. Predicate
signatures are enforced by SHACL rather than used to silently infer a type for a bad input.

## Consult the vocabulary

The read-only MCP tool `knowledge_schema` accepts:

```json
{"query": "participation", "kind": "properties", "limit": 20, "offset": 0}
```

Results include canonical predicate names, IRIs, English/Italian labels, accepted aliases,
expected subject/object classes and OWL rule flags. Follow `next_offset` to read more.
An empty query lists all terms. The catalog version and SHA-256 identify the exact profile.

CLI equivalents, from the repository:

```bash
mkdir -p .runtime
chmod 700 .runtime
docker compose exec -T mcp second-brain vocabulary participation --kind properties
docker compose exec -T mcp second-brain vocabulary --limit 100
docker compose exec -T mcp second-brain vocabulary --offset 100 --limit 100
docker compose exec -T mcp second-brain vocabulary --format turtle > .runtime/vocabulary.ttl
```

Reading OWL axioms gives an agent useful context for choosing tools and interpreting
relations. It does **not** execute a formal reasoner. Use `reason_memory` for reproducible
deductions with evidence.

The agent or another caller must explicitly request that tool. Neither `remember` nor
`recall` runs it automatically. See the [reasoning flow](reasoning.md) for the exact call
path and the limits of relying on agent instructions.

## Extraction and validation

1. The original source is saved before any model call.
2. Ollama receives a bounded, relevant vocabulary guide along with the extraction schema.
3. Known predicate aliases are normalized, e.g. `participates_in` and `partecipa` become
   `participation`, whose IRI is `urn:sbrain:vocab:participatesIn`.
4. Existing Python checks validate quotes, entity identities, scope, classifications and
   time intervals. SHACL checks the candidate graph's structure and registered relation
   signatures. SHACL also checks that the referenced entities and sources share its scope.
5. A rejected local-model response gets one repair attempt, including SHACL feedback
   and bounded source-contract diagnostics. These are repair inputs, not authorization
   to adopt: the repaired response must pass the same checks again.
   A failed batch leaves its original pending. A successful batch is adopted in the ledger.

Compose runs an independent worker that retries pending extraction, missing/incompatible
embeddings and stale projections through persistent SQLite jobs. Repeated failures require
review. See [reliability and recovery](reliability-and-recovery.md) for configuration and
restart semantics. The worker does not automatically execute or save OWL deductions.

Both local-model and manually supplied extractions undergo SHACL validation. Validation
does not prove a statement true or ensure that a model understood the source correctly.
Exact source quote checks and immutable file hashes remain application responsibilities.

The base shapes require subjects, predicates, values, sources, evidence, scope, context,
epistemic status, polarity and explicit temporal metadata. An end must follow its start.
Registered entity-to-entity relations check their expected classes. Subclasses are accepted.

The profile is open: an unknown precise predicate or entity kind remains usable through a
stable extension IRI. It receives structural validation, but no invented ontology rules.
This describes storage, manual submissions and historical data. Automatic local-model
adoption additionally requires registered relation/type signatures and a deterministic
source contract; unknown terms or unsupported wording remain pending. Registered
extension keys support explicit controlled-language triples; new natural-language
paraphrases need tested contracts. See [extraction quality](extraction-quality.md).
Literal-only legacy relationship values remain qualified claims. For example, an old
`destination="Torino"` claim is preserved without inventing a `Place` entity. It is not
silently promoted to an entity-valued OWL relationship.

`validate_knowledge` audits a selected current or historical snapshot without changing it:

```json
{"scope": "personal", "valid_at": "2026-10-15T10:00:00+02:00"}
```

```bash
docker compose exec -T mcp second-brain validate-knowledge --scope personal
```

An audit can report nonconforming old representations without losing their original data.
The extraction pipeline applies to `remember` and `extract_source`; explicit corrections
and revisions have a separate Python validation path. See
[temporal memory and corrections](temporal-memory-and-corrections.md).

## RDF export

`export_rdf` accepts `scope`, `valid_at`, `known_at` and `format`. Supported formats are
TriG, Turtle, JSON-LD and N-Quads. TriG/N-Quads/JSON-LD preserve named graphs; Turtle flattens
them and should not be used when graph partitions must survive an interchange.

```bash
docker compose exec -T mcp second-brain export-rdf --scope personal --format trig > .runtime/personal.trig
```

The CLI writes RDF directly to stdout; diagnostics go to stderr. The MCP tool returns a JSON
envelope containing the RDF string and snapshot metadata. As with other commands, the CLI
returns exit code 2 when there are warnings, even if the RDF output was produced correctly.
An audit that reports `conforms=false` also returns exit code 2. Keep personal exports in
the ignored `.runtime/` directory; they are private memory data, not repository source.

The dataset includes the vocabulary, snapshot metadata, entities, versioned claim records,
evidence, source revisions, provenance and both temporal intervals. Original subject/object
IDs remain recorded when an explicit entity merge gives them a current canonical identity.
Embeddings are excluded from the RDF export.

Claims use identified resources and RDF statement metadata. Describing a negative or
tentative claim does not assert its positive subject/predicate/object relationship.
Additional asserted-fact graphs contain only eligible explicit positive claims, partitioned
by scope and context. Known conflicting or incompatible records are excluded from those
graphs with warnings; their qualified claim records remain available.

RDF itself has no built-in bitemporal query semantics. This engine selects the requested
snapshot first. The snapshot metadata records valid time, knowledge time and ledger head.
It does not turn all historical versions into simultaneously asserted current facts.

## Local OWL reasoning

`reason_memory` runs the Python `owlrl` OWL 2 RL reasoner **inside the MCP container**.
There is no separate reasoner service and no paid API. SHACL uses `pyshacl`; RDF uses
`rdflib`. The runtime dependencies are pinned in the lock files.

```json
{"scope": "personal", "context": "attendance", "limit": 30}
```

```bash
docker compose exec -T mcp second-brain reason --scope personal --context attendance
```

Supported evidence-tracked results in this profile include:

- An inverse: `Luca participatesIn Trip` implies `Trip hasParticipant Luca`.
- Symmetry: `A knows B` implies `B knows A`.
- Transitivity: `A partOf B` and `B partOf C` imply `A partOf C`.
- Class inheritance: an instance of `Meeting` is also an `Event`.

The reasoner computes the OWL RL closure. The application returns supported conclusions
whose inverse/symmetric/transitive/class rules it can reconstruct with explicit premises.
It filters OWL bookkeeping triples and does not expose unsupported or untraceable
conclusions. It is not a complete OWL DL implementation or an unrestricted user-rule server.

A reasoning request requires a scope. Contexts are evaluated independently, using the
requested valid/knowledge snapshot and the currently installed vocabulary, identified by its
hash. `known_at` selects historical memory knowledge; it does not replay historical ontology
versions. Proposals, inferred source claims, explicit negatives,
unregistered predicates and incompatible legacy relationships are not positive premises.
Conflicting positive/negative facts and conflicting single-value predicates are excluded.
If the closure itself contradicts an explicit denial or a single-value constraint,
relationship deductions for that context are suppressed with a warning.

Every result is marked `inferred` and includes premise claim IDs and/or entity classification
IDs, source references, rule names, snapshot times and vocabulary hash. A deduction is a
consequence of its declared inputs, not independent verification of the real world.

Results are computed on demand, not appended to the memory ledger. A correction,
retraction, merge or changed query time affects the next computation. The engine does not
merge identities through `owl:sameAs`, assume missing facts false, or materialize inferred
statements as user declarations. Vocabulary, validation and reasoning tools are read-only
and available to read-only MCP credentials.

Snapshot work is bounded to 250 visible claims by default; oversized requests fail explicitly
and require a smaller scope or an owner-configured `SB_SEMANTIC_MAX_CLAIMS` limit. Reasoning
also bounds proof/closure triples and the number of contexts. Use `context` to narrow a
reasoning request. No canonical writer lock is held during validation or reasoning.

## Extend the profile

The bundled JSON catalog can be extended and rebuilt with the application. It is deliberately
broader than a minimal demo ontology, while avoiding one class per attribute or per person.

For a deployment-specific addition, set `SB_VOCABULARY_EXTENSION_PATH` to a **local JSON
file visible to the MCP process**. In Docker it must be mounted read-only or included in
your image and passed as a container environment variable. This is owner configuration,
not an MCP file-upload capability. Restart the process after editing it.

For example, save your private extension as `.runtime/vocabulary-extension.json` and use
this additional Compose file at `.runtime/compose.ontology.yaml`:

```yaml
services:
  mcp:
    environment:
      SB_VOCABULARY_EXTENSION_PATH: /etc/sbrain/vocabulary-extension.json
    volumes:
      - ./.runtime/vocabulary-extension.json:/etc/sbrain/vocabulary-extension.json:ro
  worker:
    environment:
      SB_VOCABULARY_EXTENSION_PATH: /etc/sbrain/vocabulary-extension.json
    volumes:
      - ./.runtime/vocabulary-extension.json:/etc/sbrain/vocabulary-extension.json:ro
```

Apply it with `docker compose -f compose.yaml -f .runtime/compose.ontology.yaml up -d mcp worker`.
The original service's vault mount, models and credentials remain configured by the first
Compose file. Definitions stay on the local machine and outside Git.

An extension can add `classes`, `properties` and attribution `sources`. For example:

```json
{
  "classes": [
    {
      "key": "research_project",
      "term": "ResearchProject",
      "parents": ["project"],
      "label": {"en": "Research project", "it": "Progetto di ricerca"},
      "aliases": [],
      "external_class": null
    }
  ]
}
```

New properties follow the records in `catalog.json`: a stable key and term, labels, expected
domain, optional entity range, aliases and optional reciprocal inverse, symmetry or
transitivity. A null range denotes a text-valued property. Unknown term strings are not
equated with existing concepts just because their labels look similar.

Extensions cannot overwrite registered keys/IRIs, introduce ambiguous aliases or class
cycles, or declare inconsistent inverse signatures. They are bounded local data files;
neither definitions nor external class IRIs are executed or dereferenced. Profile contents
determine the vocabulary hash stored with new extractions and returned by semantic tools.

Neo4j stores `kind_iri` and `predicate_iri` alongside existing properties. Its projection
checkpoint includes the vocabulary hash, so a changed profile refreshes the derived graph
without rewriting the canonical ledger.
