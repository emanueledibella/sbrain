# Vector profiles and migration

[Documentation index](README.md)

An embedding belongs to a vector space determined by the weights that produced it.
The model tag and vector length alone cannot identify that space. The engine stores
`embedding_profile` alongside each vector, independently of extraction provenance.

The profile is a SHA-256 digest of the embedding pipeline version, configured model,
dimensions and actual weight digest returned by Ollama's local `/api/tags` endpoint.
The raw-statement embedding pipeline currently has version `ollama-embed-raw-statement-v1`.
Injected model adapters should implement `embedding_identity()` to return a stable
profile that changes with their weights or preprocessing; simple unversioned adapters
are intended for deterministic tests, not mutable production models.

## Generation and retrieval

1. Resolve the model's actual identity before generating a batch.
2. Generate and validate all vectors, then resolve identity again.
3. Reject the entire vector batch if the digest changed or cannot be established.
4. Commit successful vectors and their profile together in the canonical ledger.
5. Compare queries only with claim vectors bearing the same profile.

Neo4j vector index names and labels include the profile. Vector queries also filter the
stored profile explicitly. Lexical and graph retrieval remain available while some vectors
are incompatible or missing. A tag change during a batch therefore cannot silently label
those vectors as belonging to the new model. Keep tags immutable during active inference;
the before/after check cannot observe a tag changed away and restored between both checks.

`memory_status.embedding_profile` reports the currently resolved profile, or null when
identity cannot be verified. `embeddings_incompatible` counts vectors requiring repair.
An Ollama outage does not invalidate source evidence or change a claim's validity.

## Existing data and model changes

Old ledger entries without a profile remain readable and pass the existing hash-chain
verification. Their vectors are treated as incompatible because their actual weights
cannot be proven from a mutable tag. The worker schedules ordinary embedding repairs;
it never guesses a profile for an old vector.

Repair appends an `embeddings` event containing the vector profile and affected claim
IDs. All assertion versions receive that vector/profile, including historical versions,
without changing valid time, knowledge time, claim identity, evidence or retraction status.
Replaying the ledger restores the same result. The queue is reconciled after a restart.

A change of digest under the same tag creates a different profile and repair generation.
During migration, queries skip incompatible vectors rather than mixing spaces. There
is no all-or-nothing search outage: lexical retrieval continues, and repaired claims
gradually regain semantic retrieval. Neo4j remains a rebuildable projection.

For an explicit full rebuild, `docker compose exec -T mcp second-brain reembed` recomputes
all claim vectors. With the worker enabled this is optional. The first upgrade to versioned
profiles may schedule repairs for every existing vector and temporarily increase CPU use.
