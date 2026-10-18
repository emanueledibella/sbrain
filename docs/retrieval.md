# Memory retrieval

[Documentation index](README.md)

The `recall` tool retrieves recorded claims using lexical matching, vector similarity and
graph proximity. It does not automatically execute OWL reasoning. The ordinary retrieval
path uses the embedding model, separately from LFM's chat extraction model.

```mermaid
flowchart TD
    A["Question to the agent"] --> B["MCP: recall"]
    B --> C["Read canonical state from the ledger"]
    C --> D["Filter by scope, identity and both time axes"]
    B --> E["Embed the question"]
    D --> F["Neo4j full-text and vector search"]
    E --> F
    F --> G["Expand to assertions sharing entities"]
    D --> H["Rank visible canonical candidates"]
    E --> H
    G --> H
    H --> I["Conflicts, result limit and response budget"]
    I --> J["Verify original sources and quotes"]
    J --> K["Return structured memories to the agent"]
    K --> L["Agent formulates an answer"]
```

## 1. Choose the query and any exact identity

The agent supplies the question and optional `scope`, `entity_id`, `valid_at`, `known_at`,
`include_uncertain`, `limit` and `token_budget` parameters.

When a question concerns one precise person or event, the agent can use `find_entities`
first and pass the resulting ID to `recall`. A semantically similar search hit is not
proof of identity. Without an exact filter, broad searches can return related people,
events or similarly named tests in the selected scope.

## 2. Construct the visible candidate set

The engine reads the ledger and obtains its canonical state. It reuses a cached state
when the ledger head has not changed and incrementally applies new events where possible.
See [state reconstruction](storage-and-data-model.md#reconstructing-state-from-events)
for the concrete structures and correction example.

It selects active assertion versions matching the requested scope and time snapshot:

| Parameter | Question answered |
| --- | --- |
| `valid_at` | What information applied at that time? |
| `known_at` | What versions did the engine know at that time? |

Both default to now. Interval starts are inclusive and ends exclusive. Retracted versions
do not appear in the corresponding current view, but earlier knowledge views remain
queryable. Explicit aliases and merges are resolved for the same requested times.

By default, `recall` includes only `epistemic=explicit` claims. Set
`include_uncertain=true` to include proposed and inferred source claims. An explicit
negative assertion remains eligible; negation is not uncertainty.

An `entity_id` restricts candidates to that canonical identity as subject or object,
including explicitly approved merges applicable to the snapshot.

## 3. Embed the question

The engine asks Ollama's embedding endpoint to represent the question with the configured
embedding model. A claim vector is compared only when its model identifier and dimensions
match the query configuration.

The model places semantically related texts near each other in vector space. Cosine
similarity measures their alignment. This comparison helps find memories that do not use
the exact words in the question, but it is not an identity check or a truth score.

If query embedding fails, the engine reports a warning and continues with textual search.

### How vectors are compared

An embedding is a list of numbers produced by a trained model. Its coordinates are learned
jointly; they are not manually named fields such as "person" or "travel". In this project,
each adopted claim's `statement` is embedded during ingestion, and the question is embedded
at search time. Both use the configured model and vector dimensions, with 768 dimensions
by default. The Python adapter sends text to Ollama's `/api/embed` endpoint.

Cosine similarity compares the vectors' directions:

```text
cosine(a, b) = sum(a[i] * b[i]) / (sqrt(sum(a[i]^2)) * sqrt(sum(b[i]^2)))
```

This can connect "Who is joining the trip?" with "Luca is coming with us" despite different
wording. It does not establish logical agreement: "Luca is joining" and "Luca is not joining"
can concern the same topic and have similar vectors. Polarity, evidence and conflicts remain
separate structured data.

For a learning path and executable examples, use these primary sources:

1. [Sentence Transformers: semantic search](https://sbert.net/examples/sentence_transformer/applications/semantic-search/README.html)
   explains embedding a corpus and query into a shared space and retrieving nearby entries.
2. [Sentence Transformers: semantic textual similarity](https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html)
   demonstrates similarity scores and comparison functions.
3. [Google: original EmbeddingGemma model card](https://ai.google.dev/gemma/docs/embeddinggemma/model_card)
   documents the text model's inputs, 768-dimensional output and training information.
4. [Ollama: embeddings](https://docs.ollama.com/capabilities/embeddings)
   shows calls to the local embedding API.

Sentence Transformers is a learning reference here, not a runtime dependency of this
repository. The implementation uses [`Ollama.embed()`](../src/second_brain/ollama.py)
and the scoring code in [`MemoryEngine.recall()`](../src/second_brain/engine.py).

## 4. Search Neo4j and expand through shared entities

Before search, the engine attempts to bring Neo4j's derived projection up to date. The
search combines a full-text index and a vector index, with vault, scope, time, uncertainty
and optional exact-identity constraints. The default search candidate limit is 200.

[`Neo4jGraph.search()`](../src/second_brain/graph.py) extracts word tokens from the question
and constructs a Lucene query from at most the first 30, quoting them and joining them
with `OR` rather than accepting arbitrary Lucene syntax from the caller. The full-text
index covers `statement`, `value`, `subject_name`, `object_name`, `context` and `predicate`.
When a query vector is available, the vector branch also searches the configured cosine
index; it currently requires a returned Neo4j score of at least `0.675`.

Neo4j's vector procedure selects its initial candidates before namespace and temporal
filters are applied. The canonical search described below therefore remains important;
unrelated or historical index entries must not hide every eligible memory.

Graph expansion starts from up to 20 assertion IDs with the highest search contributions.
It finds other visible assertions linked to the same subject or object entities:

```text
Starting assertion
   ─ ABOUT or OBJECT → Entity ← ABOUT or OBJECT ─ Related assertion
```

The starting points are assertions found through text and vectors. Expansion follows
shared entities; it is not an unrestricted traversal along every possible relationship
or a formal OWL deduction.

Neo4j contributes rank-based scores from full-text and vector results. Each branch uses
`1 / (60 + rank)`, with zero-based rank, and the vector contribution is added to an existing
text contribution. Related assertions receive `0.005` if they have no search score yet.
These contributions are separate from the exact cosine calculation below.

## 5. Rank canonical candidates

The engine also scores every eligible canonical claim using word overlap and exact
cosine similarity over its stored vector. This preserves historical retrieval when the
approximate vector index's candidate limit contains many inactive or foreign versions.

Lexical matching covers the statement, value, subject/object names and approved aliases,
context and predicate. The current scoring formula is:

```text
score = lexical coverage
      + 0.6 × max(0, cosine similarity)
      + Neo4j search contribution
```

Lexical coverage is the fraction of distinct question terms found in the claim's search
text. A candidate is included when it has a lexical match, cosine similarity of at least
0.35, or a Neo4j contribution. Scores order relevance, not reliability or factual truth.

The canonical lexical pass case-folds text, extracts words with Python's `\w+` regular
expression and compares sets of distinct words. This step does not perform stemming,
stop-word removal or synonym expansion. Exact cosine is calculated only for matching
model IDs and vector dimensions. Candidates are sorted by descending combined score,
then by claim ID for ties.

The exact canonical pass scans the visible candidate set. Its work grows with the number
of visible claims, even though Neo4j supplies indexes and graph contributions.

## 6. Attach conflicts and enforce response bounds

The engine compares visible explicit claims through the shared
[`ConflictIndex`](../src/second_brain/consistency.py). An affirmation and a denial of the
same target conflict; different positive targets conflict only where the vocabulary
declares `single_in_context`. Multi-valued relations can retain several compatible
targets. Comparisons respect scope, approved identities, inverse/symmetric direction,
contexts and overlapping validity intervals. A narrowly source-licensed bridge can
compare literal and entity-valued preferences without changing their stored predicates.
Identity filtering limits returned candidates; conflicting claim IDs are determined
from the full temporal scope, so selecting one object cannot hide a competing target.
See [memory consistency](memory-consistency.md) for contexts, homonyms and legacy records.
This is a bounded comparison policy, not a universal semantic contradiction detector.

The default result limit is 10. The default response budget is approximately 4,000 tokens,
estimated as four characters per token over serialized items. Whole items that do not
fit are omitted rather than cutting their evidence in the middle. The result indicates
whether it was truncated.

## 7. Verify evidence and return structured data

For the memories selected for return, the engine reads the necessary immutable originals,
checks their hashes and verifies the exact quoted character slices. A request-local cache
avoids rereading the same source repeatedly.

Results contain claim data, subject/object entities, canonical identity IDs, dates,
classification, predicate IRI, score, source IDs, revisions, quotes, conflicts and warnings.
Raw embeddings are excluded from the ordinary result.

The agent then writes its answer using this evidence. It can request full originals with
`read_source`, inspect one claim and its history with `explain`, or explore broader history
with `history`. Retrieved text remains untrusted source material, not new instructions.

If Neo4j is unavailable, canonical lexical matching and any available exact vector
comparisons still operate. If models are also unavailable, lexical retrieval remains.
An original saved without a successful extraction has no adopted claims to rank yet; see
[reliability and recovery](reliability-and-recovery.md).

Formal deductions require a separate [reasoning request](reasoning.md).

The independent worker discovers pending extraction and repairs embeddings/projections in
the background. It does not block every `recall` until the backlog is empty: an unfinished
source still becomes searchable only after successful adoption. Use `memory_status.recovery`
or `recovery_jobs` to inspect backlog and `needs_review` rather than assuming a successful
text save means complete indexing.
