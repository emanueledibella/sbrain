# Deterministic extraction: Qwen and LFM

[Documentation index](../README.md) · [Acceptance rules](../extraction-quality.md)

Date: 2026-10-16. This is a regression evaluation on the same small Italian corpus
used to diagnose the implementation, not an estimate of general accuracy.
The production model and its configuration were not changed.

## Configuration and method

- 16 fictional Italian notes, 23 annotated facts, two passes per model: 64 measured extractions.
- Qwen `qwen2.5:1.5b` and `LiquidAI/lfm2.5-2.6b:q4_k_m`, existing Q4_K_M weights.
- Pinned Ollama 0.40.3; CPU-only Docker on Apple M4 Pro.
- 4-vCPU quota, 4 threads, 6 GiB container limit, no additional swap.
- Context 16,384; output limit 8,192 per request; temperature 0, seed 42,
  top_k 40, top_p 0.9, min_p 0, repeat_penalty 1.1.
- Qwen/LFM/LFM/Qwen order, same paired shuffled note orders, fresh runner/warmup per block.
- Latency includes validation and up to one extraction repair, including failed notes.
  Loading, warmup, embeddings, MCP and Neo4j are excluded.
- RAM is the runner process's observed Linux VmHWM, not whole-stack or minimum RAM.

The previous pipeline used source grounding plus a separate same-model AI reviewer.
The revised pipeline uses deterministic source contracts, complete names, source-local
relations/polarity/certainty, exact targets and typed `object_ref` links. Its prompt and
vocabulary were also updated. This comparison measures the whole pipeline change.
The annotated corpus and factual scoring rubric were unchanged.

An initial partial Qwen block overlapped the final integration tests. It was aborted
solely to remove timing contention, retained separately, and excluded from every metric.
The full measured run started after the 185-test suite passed; code stayed frozen.

## Results

| Metric | Qwen previous | Qwen deterministic | LFM previous | LFM deterministic |
| --- | ---: | ---: | ---: | ---: |
| Correct / accepted claims | 18/21 | 15/15 | 34/40 | 25/25 |
| Incorrect accepted claims | 3 | 0 | 6 | 0 |
| Strict accepted-claim precision | 85.7% | 100% | 85.0% | 100% |
| Expected facts recovered | 18/46 (39.1%) | 15/46 (32.6%) | 34/46 (73.9%) | 25/46 (54.3%) |
| Completely correct notes | 11/32 | 11/32 | 19/32 | 21/32 |
| Accepted extractions | 14/32 | 11/32 | 26/32 | 21/32 |
| Median extraction | 8.34 s | 6.93 s | 66.33 s | 51.58 s |
| P95 extraction | 14.82 s | 11.82 s | 125.28 s | 151.19 s |
| Peak runner RSS | 2.076 GiB | 2.145 GiB | 2.914 GiB | 3.196 GiB |
| AI review requests | 15 | 0 | 30 | 0 |
| Extraction repair requests | 19 | 23 | 5 | 15 |

Zero accepted errors describes these 15 and 25 returned claims. Rejected notes still
lose expected facts from the graph. Their original sources remain saved and pending
for worker retries or `needs_review`. The revised pipeline trades coverage for source
fidelity; it is not an unqualified accuracy improvement.

LFM recovers more correct facts than Qwen under these controls, with higher latency
and RAM. Neither result establishes reliable unattended ingestion of arbitrary
personal notes. The conservative language contracts and model repair behavior need
broader held-out evaluation.

## Graph structure and remaining limits

All accepted registered object properties now have actual `object_ref` links:
Qwen **11/11**, LFM **15/15**. Previously they were Qwen **0/13**, LFM **0/24**.
Denominators reflect each run's registered profile and accepted notes; this is a
separate structural metric, not a revised factual scoring rule.

A diagnostic replay blocks all nine previously accepted erroneous claims for their
intended name/relation/polarity/target failures. It is not new model inference and is
not added to the score. In the new measured run:

- Generic event name `Gita`, visit-as-destination/preference, negative pending status,
  and locations losing container details do not pass the acceptance checks.
- Qwen still rejects many notes because names/quotes are translated or fields are
  assigned incorrectly. Correct decimal reformattings can also fail strict value matching.
- LFM still fails several participation, mixed-fact, daily-storage and task notes.
  A second generation can repeat the error or omit another fact.
- Entity typing remains incomplete: the subscription in the cost/payment example is
  still typed `event`. The unchanged factual rubric does not comprehensively annotate
  types. Valid domain/range constraints do not establish correct real-world classification.
- Identical request hashes can yield different outputs between passes, despite fixed
  sampling. The date-qualified event succeeds once with LFM and is rejected once.

## Follow-up safeguards and evidence

After the frozen measurements, regression tests also fixed stale object links on
corrections/revisions and the English name `May` being mistaken for a tentative modal.
Same-target aliases, polarity-only changes and legacy descriptive `yes/no` values
preserve object identity. Replaying all recorded responses verifies identical request
hashes, repair paths, outcomes and accepted drafts for all 64 benchmark runs after
these follow-ups. Timing figures remain the original frozen-run measurements.
The final full suite passes **192 tests**, including real Ollama/Neo4j and both
worker end-to-end modes. Existing legacy relationship tests were preserved.

The raw evidence is kept outside Git in
`.runtime/model-comparison-deterministic-20261016/`: `report.md`, `comparison.json`,
`comparison.csv`, `reviews.json`, `results.jsonl`, corpus/rubric, source snapshots,
integrity/cleanup checks and a downloadable `results.zip`.
No benchmark memories were written to the personal vault or Neo4j.
