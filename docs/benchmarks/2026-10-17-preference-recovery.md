# Recovery of a saved entity-valued preference

October 17, 2026. Follow-up to the [initial LFM deployment](2026-10-17-lfm-deployment.md).
This fixes a source-contract coverage gap; it is not a new matched model benchmark.
All source content in this verification is fictional.

## Change and acceptance boundaries

`source-contracts-v5` recognizes that an explicit preference clause can be represented
as a literal `preference` or an entity-valued `prefers` relation. Both remain distinct
vocabulary properties. The latter requires an agent and a typed, exact source target;
its inverse `preferred_by` preserves direction and role constraints. There is no
global alias, literal-to-object conversion, or OWL property equivalence.

Only explicit `preferisce`, `preferiscono`, `prefer` and `prefers` verbs license the
alternate object form. Wanting/desiring and tentative visits do not. Both forms still
require complete names/values, a covering quote, correct polarity and certainty,
offered identities, temporal support and SHACL conformity. A single source witness
cannot be adopted with competing predicates. The model's chosen property is retained.
Exact-name declaration completion and repair hints use the same source contract.

The new extractor version is **9**. The general prompt, schema and vocabulary guide
remain unchanged; the alternate form is licensed by clause-level checks and repair
hints. No existing property keys, IRIs, aliases, domains or ranges were changed by this fix.
Existing claims retain their prior provenance and are not reinterpreted.

The repair request also starts from the original source and bounded diagnostics,
without echoing the rejected assistant JSON. It avoids explicitly feeding a wrong
subject or malformed JSON keys back to the model; it grants no exception to
identity, evidence or source-contract checks and adds no extra model attempt.

## Retry through the deployed worker and MCP

The saved revision was:

> Marta Bellini preferisce il tè verde. Forse Marta Bellini partecipa alla Riunione Faro.

- Scope: `test-lfm-20261017-7ac3`.
- Source: `343d2c80-2e80-4e2b-acd2-9b32dc80f2cc`, **revision 1**.
- Before recovery: no adopted claims for that revision, with the original extraction
  job in `needs_review` after five v4 worker attempts.

After rebuilding/recreating both application containers with v5, the same persisted
job was explicitly retried. Once the worker held the job, a real MCP `extract_source`
request concurrently targeted the same revision. No structured extraction override
was supplied. LFM completed the extraction, and the waiting foreground request returned
the adopted result after **49.66 seconds**. The recovery job reached `done`.

| Subject | Property | Target | Classification | Polarity |
| --- | --- | --- | --- | --- |
| Marta Bellini | `prefers` | tè verde | `explicit` | positive |
| Marta Bellini | `participation` | Riunione Faro | `proposed` | positive |

Both claims retain exact source evidence, LFM provenance, the current model digest and
`quality_checks.version=source-contracts-v5`. Both reuse the original Marta Bellini
identity from the trip note, without duplicating or retyping her entity.

## Duplicate and integrity checks

After worker recovery, two further `extract_source` calls and a repeated `remember`
using the original `request_id` returned the same source revision and claim IDs.
The ledger head did not change across those repeated requests.

| Check | Before | After |
| --- | ---: | ---: |
| Total source revisions | 48 | 48 |
| Total claims | 89 | 91 |
| Revisions for the recovered source | 1 | 1 |
| Adoption events for that source revision | 0 | 1 |
| Claims for that source revision | 0 | 2 |

The original Markdown readback was byte-for-content identical at the MCP boundary.
All 48 originals passed hash verification, and all 89 prior claims and existing entity
and source records remained unchanged. Neo4j was current and contained exactly the
two new source-backed assertions with typed object edges. Both new embeddings were
compatible with the existing EmbeddingGemma profile.

The successful recovery initially used clarified preference descriptions. Those
wording changes were subsequently removed to keep the general vocabulary guide
unchanged. The observed vocabulary snapshot is preserved in local diagnostics for
the claims' provenance; the property signatures and v5 acceptance rules are identical.

Default `recall` returned the explicit preference and excluded the proposed
participation. `include_uncertain=true` returned both; an exact Marta entity filter
kept her established identity. SHACL conformed for all **10** claims in the test scope
at the selected future view, including the previously tested bounded trip interval.

The original seven-note deployment sample is now fully indexed, with ten adopted
facts. Six notes retain their v4 provenance and only the recovered note used v5. These
results do not claim that the entire sample or the earlier Qwen/LFM benchmark was
rerun under v5.

## Regression coverage and local artifacts

[`test_preference_contracts.py`](../../tests/test_preference_contracts.py) adds automatic
extraction tests for literal/object/inverse forms, affirmative/negative/tentative
clauses, full targets, omitted declarations, homonyms, type/value/date mismatches,
wrong directions and competing representations. Its mixed-note concurrency test
proves one model extraction/adoption for foreground and recovery calls on one saved
revision, then repeats the original operation without adding sources or claims.

Local evidence is in `.runtime/preference-recovery-20261017/`: `before.json`, real MCP
`recovery.json`, the canonical/Neo4j `audit.json`, build logs and full-suite reports.
The directory is Git-ignored. Deployment vaults, queues, credentials and model files
are excluded from the repository publication.

The first full suite recorded **281 passes and one rejected extraction**, retained in
`first-full-suite.xml/log`. A diagnostic replay of that exact source and its original
metadata reproduced a wrong subject (`Palermo` as the trip) followed by malformed JSON.
Both were correctly rejected. Restoring the unchanged vocabulary guide alone did not
resolve it. The final change removes the rejected assistant JSON from the single
repair request, while preserving all validators and the two-request budget.

With the **same initial request hash**, the model again proposed the wrong subject,
but the clean repair returned `Trip` → `Palermo`, which passed every gate. The repair
request took 18.50 seconds in that diagnostic. No replay claims were adopted. Local
artifacts retain both rejected replays, the accepted clean replay and their request
hash comparison. The live test now includes the complete returned warning in its
assertion message rather than a truncated dictionary, so future failures are visible.

The final rebuilt Docker suite passed **283 tests, zero failures/errors/skips**, in
222.17 seconds with real LFM, EmbeddingGemma and Neo4j. Both worker end-to-end variants
completed every outage/restart/review/recovery stage. The CI JUnit coverage gate,
Ruff, Compose configuration validation and documentation links passed locally.
Both application containers contain the final source files. The final deployment
has 48 sources, 91 claims, no pending extraction, no missing/incompatible embeddings
and no jobs waiting for retry or review.
