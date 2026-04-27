# Autonomous Source Expansion Design

## Purpose

EIMemory should not wait for an operator to manually add every future knowledge source. It should observe recall gaps, source performance, and knowledge coverage, then safely propose and apply small source expansions during nightly memory maintenance.

This remains a memory-system capability, not an execution-system capability. The feature only updates source registry strategy and writes audit records. Fetching, extraction, promotion, and synthesis continue to use the existing intake and nightly pipeline.

## Safety Model

Autonomous expansion uses a guarded loop:

1. Build topic gaps from `collection_policy.gap_queries`.
2. Generate source expansion proposals from known safe source families.
3. Evaluate proposals with an optional LLM evaluator plus deterministic fallback scoring.
4. Apply only proposals that pass score threshold, daily budget, duplicate checks, and source-family constraints.
5. Persist an audit record for every approved or rejected proposal.

The first implementation supports ChatPaper arXiv category expansion because the connector already exists, URLs are deterministic, and `metadata.categories` can broaden coverage without creating many duplicate registry entries.

## LLM Evaluation Contract

The evaluator is injectable:

- Input: proposal payload plus compact context.
- Output: `{score, decision, reason, labels}`.
- Accepted decisions: `approve`, `reject`, `needs_review`.

If no evaluator is provided, EIMemory uses a deterministic evaluator. If an evaluator returns malformed output or low confidence, deterministic scoring remains the safety floor.

## Nightly Integration

Nightly runs autonomous expansion before external collection so newly approved categories can be collected in the same run. The report includes:

- proposal count
- approved count
- applied count
- rejected count
- audit record ids
- updated source ids

## Acceptance Criteria

- No duplicate ChatPaper categories are added.
- Expansion is bounded by `max_apply`.
- Low-score or unsafe proposals are rejected and audited.
- Source registry records `autonomous_expansion` metadata.
- Governance snapshot exposes the latest expansion status.
- Tests prove the expansion loop can run without a real LLM and can consume an injected LLM evaluator.
