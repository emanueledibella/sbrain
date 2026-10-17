# LFM deployment verification

October 17, 2026. This is a deployment smoke test, not another matched Qwen/LFM
benchmark. All new source content is fictional and isolated in scope
`test-lfm-20261017-7ac3`.

This records the initial **v4** deployment. The subsequent v5 preference fix and
retry of the same saved revision are documented in the
[preference recovery report](2026-10-17-preference-recovery.md).

## Active configuration

Both MCP and worker were rebuilt from the checkout and recreated using the same image.
Their installed `quality.py` hashes match the checkout. Both report
`source-contracts-v4`, `LiquidAI/lfm2.5-2.6b:q4_k_m`, a 16,384-token context,
8,192 generated tokens, four inference threads and a 300-second Ollama request timeout.
Ollama and Neo4j containers, model files, credentials and persistent volumes were reused.
EmbeddingGemma remains the embedding model; existing vectors were not regenerated.

The observed LFM model digest is
`83f596831da1869c694b67b8d1c0978dddebf07599b663453befbdd953bfdcea`.

## Timeout investigation

The previous 600-second timeout occurred during host sleep. macOS records lid-close
sleep at **October 16, 15:24:12 Europe/Rome** and full wake at **23:22:48**. The Ollama
failure log resumes at that same wake instant (**21:22:48 UTC**).

The exact failed repair request body, including the same model, schema, prompt, seed
and generation settings, was replayed on the awake deployment. The request checksum
matches the historical trace. It completed normally in **54.45 seconds**, generated
2,392 tokens, and its two contact claims passed the current deterministic gates on
offline response replay. No replay claims were adopted into the vault. The deployed
Ollama container has different resource limits from the old benchmark container, so
this is a diagnostic replay rather than a replacement benchmark measurement.

The historical benchmark retains its timeout and original coverage/latency results.
See [model operation](../local-model-operation.md) for host sleep, client timeouts,
request budgets and durable retry behavior.

## New Italian sources

The corpus and expected facts were written before insertion. Requests used the actual
authenticated MCP HTTP server with **no structured extraction override**. Each successful
source was read back and each adopted claim inspected through `explain` and `recall`.

| New note | Expected facts | Adopted immediately | `remember` time | Result |
| --- | ---: | ---: | ---: | --- |
| Gita Bosco Azzurro → Trento; Marta Bellini participates | 2 | 2 | 45.12 s | Indexed |
| Davide Rinaldi does not participate in that trip | 1 | 1 | 59.64 s | Indexed, negative polarity preserved |
| Archivio Nuvola subscription costs 18,90 euro per month | 1 | 1 | 28.88 s | Indexed, amount/currency/recurrence preserved |
| Rinnovo PassMuseo deadline: November 23 at 18:15 UTC+01:00 | 1 | 1 | 35.00 s | Indexed; deadline is not claim validity |
| Studio Levante email and website under `.example` | 2 | 2 | 104.93 s | Indexed |
| Marta Bellini prefers green tea; might attend Riunione Faro | 2 | 0 | 103.02 s | Source saved, extraction pending |
| Gita Vento del Nord → Aosta during an explicitly zoned interval | 1 | 1 | 178.31 s | Indexed, both interval bounds preserved |

Initial outcome: **6/7 complete notes, 8/10 expected facts, no incorrect accepted
claims in this sample**. All seven original Markdown revisions are preserved. Times
include MCP, extraction/repair, validation, embeddings and projection work; the deployed
recovery worker was active and could contend for Ollama. They must not be compared
directly with the isolated extraction-only benchmark timings.

The rejected mixed note exposes a coverage limitation: the model chose the registered
entity-valued `prefers` relation, while the recognized natural-language source contract
supports the literal `preference` property. The gates did not accept that alternative
representation. Because the batch is atomic, the tentative participation in the same
note was also withheld. This is a rejection, not an accepted factual error. No manual
extraction, weakened gate or new source revision was used to make the result succeed.
The persistent worker retries that same revision and exposes `needs_review` if its
retry budget is exhausted. Inspect current job status rather than assuming a retry
has succeeded.

## Storage, graph and retrieval checks

- The original **41 sources** were hash-verified; their records, the original 81 claims
  and existing entities were unchanged.
- The run added exactly seven sources and initially eight adopted claims. Repeating
  successful operations with the same `request_id` reused the source and claim IDs.
  There was at most one adoption event per tested source revision, including worker races.
- All adopted claims have compatible EmbeddingGemma vectors and LFM provenance with
  `quality_checks.version=source-contracts-v4`.
- Neo4j was current, with eight assertions, four typed object links and source support
  edges for this scope. Participation and destination refer to the same trip identity.
- Scoped SHACL validation conformed for all eight adopted claims.
- The Aosta interval is absent just before October 18 at 09:00 UTC+02:00, present
  exactly at that instant, and absent exactly at October 20 at 18:00 UTC+02:00.
- The test also checked that default retrieval excludes proposals, but its mixed note
  was withheld entirely: that empty result alone is **not** positive evidence that LFM
  successfully extracted uncertain facts. Deterministic suite tests cover uncertainty
  handling separately.

To explore the fictional adopted records in Neo4j Browser:

```cypher
MATCH (a:Assertion {scope: 'test-lfm-20261017-7ac3'})-[:ABOUT]->(subject:Entity)
OPTIONAL MATCH (a)-[:OBJECT]->(object:Entity)
OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(source:SourceRevision)
RETURN subject, a, object, source;
```

Local diagnostic artifacts are in `.runtime/lfm-deployment-20261017/`: corpus and MCP
results, deployment/quality hashes, original/replayed timeout traces, selected power
events, storage/graph audit and test reports. This directory is ignored by Git.
Its private environment rollback copy must never be included in a public archive.

## Full suite and final state

The rebuilt Docker test image passed **239 tests, zero failures/errors/skips**, in
294.52 seconds with real LFM, EmbeddingGemma and Neo4j enabled. Both worker end-to-end
variants completed outage preservation, automatic retry, persisted `needs_review`,
restart after commit-before-acknowledgment and recovery without duplicate records.
The same `ci_results.py` coverage gate used by GitHub Actions accepted the JUnit report.
Ruff, Compose configuration validation and documentation link checks also passed.

At the final deployment audit, MCP, worker, Ollama and Neo4j were healthy. The vault
contained **48 sources and 89 claims**, including exactly seven new sources and eight
new claims. There were no missing or incompatible embeddings. The mixed note remained
pending, with one extraction job in `retry_wait`; the autonomous worker was alive and
no deployment job had reached `needs_review` yet. This is distinct from the disposable
suite's deliberate `needs_review` scenarios. Queue state can change after this snapshot.

Runtime defaults and local `.env` now select LFM. All non-model `.env` settings,
including credentials, were preserved. Existing records retain their original model
provenance. This verification ran locally; it does not represent a GitHub Actions run
or a repository push.
