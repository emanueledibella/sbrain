# Semantic safety verification — October 19, 2026

[Documentation index](../README.md) · [Safety policy](../semantic-safety.md)

This verification addresses the six counterexample families from the October 18 audit.
It is a targeted safety regression, not a representative natural-language accuracy
benchmark or a Qwen/LFM performance comparison.

## Regression results

| Check | Result |
| --- | --- |
| Original adversarial audit specifications | All 38 pass; the audited version had 27 failing requirements and 11 passing controls |
| Expanded semantic-safety module | All 65 pass |
| Complete Docker suite, including real Ollama/Neo4j and both worker recovery scenarios | 413 passed; 0 failed, errors or skips; 392.51 seconds |
| Final Docker semantic-safety and MCP protocol checks | 72 passed; 0 failed, errors or skips; 18.50 seconds |
| Distinct test cases across the complete run and final expanded checks | 418 passed |
| Ruff and whitespace validation | Passed |

The final focused run includes five additional cases added after the complete run:
three nonassertive punctuation variants, explicit CLI review/idempotence, and surviving
independent classification support. The production extraction/reasoning implementation
is unchanged between these runs; the final MCP policy additionally explains the trust
boundary. Existing tests for deliberately reviewed arbitrary semantic triples now use
an explicitly attested library fixture rather than silently treating model JSON as review.

The full-suite JUnit report also passes `scripts/ci_results.py`, including its checks
that both deterministic and real-service worker recovery scenarios actually ran.

## Actual local-model counterexamples

All cases ran in disposable vaults against the configured
`LiquidAI/lfm2.5-2.6b:q4_k_m`, with embeddinggemma, 16,384 context tokens, 8,192 output
tokens, four CPU threads and a 300-second per-request timeout.

| Synthetic Italian source | Adopted factual claims | Recall and OWL outcome |
| --- | --- | --- |
| `Marta Riva lavora per Officina Cedro?` | 0 | No employment memory or employment deduction |
| `Marta Riva partecipa alla Riunione Levante?` | 0 | No participation memory or participant deduction |
| `Marta Riva lavora per Officina Cedro.` | 1 | Employment memory and supported inverse `employs` deduction |

Both question originals were durably retained and marked extracted without factual
claims. The declarative original produced one employment claim. Repeating each operation
with the same request ID retained one original and the same claim IDs. These checks
exercise actual inference, deterministic adoption, recall, reasoning and idempotence;
they do not rely on an AI reviewer.

## Corrected boundaries

- Speech acts precede punctuation stripping. Correct empty extraction is accepted,
  and repair diagnostics do not demand declarative facts from questions.
- Both structured MCP write tools and every model adapter pass independent source
  contracts at the adoption boundary. Owner-attested imports are separate CLI/library
  operations with recorded provenance and are absent from the MCP tool schema.
- A model-selected UUID cannot resolve canonical homonyms. Approved merges collapse
  candidates to one canonical identity with aliases.
- Effective/decision prefixes date facts. Event-name dates and literal deadline values
  do not. Explicit starts and interval bounds must be preserved in covering evidence.
- New classifications use relation-licensed minimum roles. OWL type proofs require
  active supporting claims, respecting context, retractions and both time axes.
- Unsupported conditions and alternatives remain durable `needs_review`, including
  after worker restart, without invented entities or duplicate memories.

The local MCP and worker containers were rebuilt and restarted after backing up the
vault and persistent SQLite queue. Ledger bytes, originals, source metadata, claims,
entities, versions and identity history were unchanged. The running MCP reports
`source-contracts-v8`, healthy local services, a live worker and no pending/review jobs
or missing/incompatible embeddings.

Existing records are not retroactively certified under the new policy. The read-only
legacy-data audit and detailed JUnit/backup/integrity artifacts remain private under
`.runtime/semantic-safety-20261019/`. Passing these finite tests establishes the specified
boundaries, not universal semantic accuracy or the real-world truth of source statements.
