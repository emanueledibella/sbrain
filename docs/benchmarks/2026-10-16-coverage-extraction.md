# Extraction coverage: Qwen and LFM

[Documentation index](../README.md) · [Extraction quality](../extraction-quality.md) · [Previous source-contract benchmark](2026-10-16-deterministic-extraction.md)

2026-10-16. At the time of this comparison, the production extraction model remained
**qwen2.5:1.5b**. These measurements use fictional sources and do not adopt any benchmark memories.

**October 17 follow-up:** deployment now uses LFM and the new controls. The timeout
investigation found host sleep during the failed request, and its exact request body
completed on replay in 54.45 seconds. See [local model operation](../local-model-operation.md).
The historical measurements below retain the original failure and its measured cost.

## Changes

All 32 rejected extractions from the preceding full comparison were inspected. Some contained real model errors; others contained equivalent representations or complete source-supported facts with missing declarations. The revised automatic pipeline (`source-contracts-v4`, extractor version 8) adds:

- Narrow two-place decimal separator equivalence, retaining currency, sign, units and recurrence.
- Equivalent explicitly zoned deadline literals, retaining source wording and separating deadlines from claim validity.
- Full participation phrases normalized only when their complete target and polarity match the source.
- Omitted subject declarations completed only from an exact full source-name ref and a unique supported triple; unique offered identities preserve exact ID/name/kind, including bounded grammatical descriptor equivalence.
- Coalescing of entity declarations only when ref and every field are identical. Conflicting fields and distinct refs remain unmerged.
- Bounded source-derived repair diagnostics for names, canonical relations, roles, polarity, certainty and exact evidence. Every repaired output passes all checks again. No AI reviewer authorizes adoption.

An unrecognized type, wrong supplied target, truncated name, altered negation, invented date/quote or omitted fact can still keep the whole note pending. No missing claims are synthesized to pass coverage checks. See the [acceptance policy](../extraction-quality.md#recovering-representational-false-rejections).

## Matched method

Same 16 Italian notes and unchanged factual rubric; 23 expected facts per pass, twice per model: **64 measured extractions and 46 expected facts per model**. Same model weights/digests and quantizations as before: `qwen2.5:1.5b` and `LiquidAI/lfm2.5-2.6b:q4_k_m`. Initial extraction system prompt, schema and vocabulary are unchanged; deterministic acceptance/normalization and repair feedback differ.

Pinned Ollama 0.40.3, CPU-only Docker on Apple M4 Pro (24 GiB host RAM), 4-vCPU quota, four inference threads, 6 GiB memory limit with no extra swap, context 16,384 and generation cap 8,192 per request. Temperature 0, seed 42, top_k 40, top_p 0.9, min_p 0, repeat_penalty 1.1. Native model templates and LFM thinking default are retained. One loaded model and one request at a time.

Order Qwen/LFM/LFM/Qwen, the same two shuffled note orders, fresh runners and excluded warmups. All 238 tests finished before measurements. Source hashes were frozen before the full run and verified afterward. A preceding partial run was excluded in full after discovering the exact duplicate declaration false rejection; its traces and reason are retained separately. No cases or favorable outcomes are selectively combined.

Latency includes extraction, deterministic validation/SHACL and up to one extraction repair, including failures. Loading/warmups, observation probes, embeddings, MCP transport and Neo4j are excluded. RAM is Linux runner VmHWM, not minimum required memory or whole-stack RAM.

## Results

| Metric | Qwen before | Qwen after | LFM before | LFM after |
| --- | ---: | ---: | ---: | ---: |
| Completely correct notes | 11/32 | 17/32 | 21/32 | 29/32 |
| Correct accepted claims | 15/15 | 25/25 | 25/25 | 39/39 |
| Incorrect accepted claims | 0 | 0 | 0 | 0 |
| Expected facts recovered | 15/46 (32.6%) | 25/46 (54.3%) | 25/46 (54.3%) | 39/46 (84.8%) |
| Strict precision on accepted claims | 100.0% | 100.0% | 100.0% | 100.0% |
| Median extraction, all outcomes | 6.93 s | 6.59 s | 51.58 s | 43.12 s |
| Mean extraction, all outcomes | 6.92 s | 6.94 s | 63.28 s | 72.27 s |
| P95 extraction | 11.82 s | 12.53 s | 151.19 s | 132.73 s |
| Peak model-runner RSS | 2.145 GiB | 2.170 GiB | 3.196 GiB | 3.149 GiB |
| Extraction repair requests | 23 | 19 | 15 | 11 |

## Remaining rejections

- **Qwen: 15/32 notes pending.** `abstention` (2), `amount` (2), `contacts` (2), `injection` (2), `mixed-certainty` (1), `proposal` (2), `todo` (2), `validity-interval` (2)
- **LFM: 3/32 notes pending.** `contacts` (1), `daily-storage` (1), `mixed-certainty` (1)

LFM still rejects the second-pass mixed note and daily-storage note because unregistered types remain after repair. Its contacts repair times out after 600 seconds (627.96 seconds for the entire note). This timeout stays in both latency and coverage; the underlying runtime slowdown was not isolated. Although LFM median latency improves, its mean rises from 63.28 to 72.27 seconds. All 64 initial request hashes are identical to their baseline counterparts.

A rejected batch contributes zero recovered facts, even if some candidate claims are correct. Here accepted means the extractor returned a validated batch; incorrect accepted claims are scored among those returned batches. No benchmark sources or claims are persisted. In production, rejection leaves the original durable and pending; worker retries can eventually expose `needs_review`.

## Interpretation and limits

Correctness is audited against each original source and unchanged gold facts, separately from validator acceptance. Saved reviews are reused only for exactly identical semantic drafts for the same source/model (exact quote offsets are independently checked). No model evaluates its own output.

This is a small, known regression corpus already used for diagnosis, not an unbiased evaluation on unseen personal notes. Two passes are timing repetitions, not 32 independent examples. Zero accepted factual errors here is not a general guarantee. The original rubric does not comprehensively annotate entity kinds: generic entity types or an overly specific subscription-as-event type can remain compatible with broad relation signatures. Typed edges and minimum roles are not proof of real-world identity, type or truth.

The source parser supports a bounded Italian/English subset. Unsupported phrasing, complex coordination, pronouns and other languages may still need review. The existing manual structured-submission trust contract remains distinct.

## Verification and evidence

**238 passed, zero failures and zero skips**, with real Ollama, real Neo4j, both worker recovery E2E variants and 98 held-out quality regressions. Ruff and whitespace checks passed. Diagnostic replay also keeps all nine previously incorrect historical claims blocked and recovers five previously rejected complete extractions from their old first responses; these replays are excluded from measured scores.

Full local evidence is retained under `.runtime/model-comparison-coverage-v2-20261016/`: `report.md`, `comparison.json`, `comparison.csv`, `reviews.json`, complete `results.jsonl`, corpus/rubric, frozen source snapshot/hashes, model/resource manifest, integrity/cleanup results and final JUnit report. The excluded predecessor is `.runtime/model-comparison-coverage-20261016/`. Runtime evidence is ignored by Git and contains synthetic data.

Production `.env`, Compose and model selection were unchanged during the comparison.
The existing MCP/worker images were not rebuilt then. For the current deployment and
model migration procedure see [local model operation](../local-model-operation.md).
To rebuild application services with an already configured model:

```bash
docker compose up -d --build mcp worker
```
