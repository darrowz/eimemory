# Dense admission vs verification call contract (2026-09-20)

Issue: dense admission vs verification call contract inconsistency.

## Contract

1. **Dense similarity alone does not admit.** Cosine / `dense_vector_score`
   ranks candidates. It is not answer evidence and must not, by itself, sustain
   admission when a configured verifier is required.
2. **Verification may be skipped only for independent non-dense evidence.**
   Callers of `needs_verification(query, chosen, *, independent_evidence=...)`
   may skip only when `chosen` is non-empty **and** they assert a kind from
   `INDEPENDENT_EVIDENCE_KINDS`:
   - `identity_lookup` — exact title / record_id match
   - `keyword_exact` — exact keyword evidence
   - `lexical_durable` — strongly lexical durable event / commitment match
   - `graph_relation` — trusted graph grounding
   - `verified_proof` — prior successful `verify_candidates` proofs
3. **Merely similar candidates are not independent evidence.** A non-empty
   `chosen` list of dense/similarity hits without an asserted kind still
   requires verification. Lightweight cosine+coverage selections therefore pass
   empty `chosen` into the helper (or omit independent evidence) so they cannot
   bypass the verifier.
4. **Exclusivity / negation queries always re-verify**, even with independent
   evidence (`只需|只要|不必|不用|不要|只看|而不|only|not|without|rather than`).

## Call sites

- `LightweightAdmission.select`: identity lookup returns before verification;
  similarity-gated selections force verification when assistance is enabled.
- `GovernedRecallEngine._select_post_fusion_items`: when assistance is enabled,
  always calls `verify_candidates` for non-early-stop fusion candidates
  (similarity-ordered, not independently evidenced).

## Obsolete draft rejected

An earlier draft required "configured verifier must always verify" with no
skip path. That conflicts with identity / keyword / durable lexical fast paths.
The restored helper skip remains, but only behind an explicit independent-
evidence assertion—not behind a bare non-empty candidate list.
