# Production Data and Code-Evolution Closure Design

**Date:** 2026-08-27

**Target:** the first exact-commit immutable release after `1.11.24`

**Scope:** repair production-query channel authority, prevent recurrence during
Hongtu identity normalization, restore the production recall gate, classify the
release lineage correctly, and complete the bounded code-evolution transaction
mechanism without manufacturing production maturity evidence

## 1. Decision

The implementation will repair the authority invariant at its owner and then
complete the existing protected transaction architecture. It will not relabel
records, lower the three-channel gate, treat simulation as production evidence,
or give model output execution authority.

Three approaches were considered:

1. Read flattened records as a permanent compatibility exception. This would
   make the current report green quickly, but it would retain contradictory
   outer and embedded scopes and weaken every later evidence check.
2. Remove channel coverage or duplicate OpenClaw cases under other channel
   names. This would fabricate coverage and is rejected.
3. Preserve valid channel sub-scopes in identity normalization, repair the
   affected evidence records with bounded cross-checks, use indexed exact-scope
   reads, and add the missing protected transaction effect owner. This is the
   selected approach.

## 2. Confirmed production failure

The production database contains release-bound proactive decisions for all
three channels and already contains five human-accepted Codex cases and five
human-accepted Hermes cases. Each accepted case retains the correct embedded
scope, such as `embodied::channel::codex` or
`embodied::channel::hermes`.

The nightly Hongtu identity repair treats every `eimemory.*` record outside the
base `hongtu/embodied` scope as an orphan. It therefore rewrites production
recall pending cases, label evidence, and accepted cases to the base outer
scope. The production-query builder correctly asks for the exact per-channel
outer scope and consequently reports Codex and Hermes as zero. The data exists;
its evidence envelope was moved across its authority boundary.

The code-evolution ledger is empty for a different reason. The current
transaction submission path intentionally terminates with
`code_evolution_effect_executor_unavailable`; the deployment machine policy is
absent and the emergency kill-switch file is present. Provider availability,
catalog incubation, and a healthy release are therefore insufficient to create
a production transaction.

The current release lineage also treats
`deploy/bootstrap_production_recall.py` as an unknown production path, making
the current lineage incompatible even when the deployment receipt is valid.

## 3. Authority and safety invariants

1. `hongtu/embodied` is the canonical Hongtu identity scope. A workspace of
   `embodied::channel::<supported-channel>` is a valid authority sub-scope, not
   an orphan identity.
2. Identity repair may normalize identity metadata without erasing a valid
   runtime channel sub-scope.
3. A repaired evaluation record is accepted only when its outer source,
   embedded channel, embedded scope, `source_id`, pending-case relationship,
   label-evidence relationship, and referenced corpus record all agree.
4. Raw queries are never added to the production dataset. Existing digest-only
   and bounded-feature contracts remain unchanged.
5. OpenClaw evidence cannot satisfy Codex or Hermes coverage. Synthetic records
   cannot satisfy any production gate.
6. The code provider proposes file contents only. It cannot choose commands,
   environment, repository, branch, deployment target, policy, or credentials.
7. Every Git, push, deploy, rollback, and sedimentation effect is intent-first,
   lease-owned, idempotent, and reconciled against external state before retry.
8. The kill switch remains the highest-priority emergency control. A missing,
   expired, mismatched, already-consumed, or disabled one-shot policy grants no
   effect.
9. A user-reported or manually bootstrapped repair never qualifies as
   system-originated code evolution, even if its tests and deployment pass.
10. A qualifying production result requires a previously unknown
    system-detected incident, exact candidate deployment evidence, current
    compatible lineage, and the full 48-hour observation contract.

## 4. Production data repair

### 4.1 Preserve valid channel scopes

The identity module will recognize a canonical Hongtu base scope plus one
supported `::channel::` suffix. For such records:

- identity metadata may be backfilled;
- user aliases may be canonicalized where existing policy allows it;
- the channel suffix must be retained;
- an invalid, nested, unsupported, or metadata-conflicting suffix remains a
  repair candidate and fails closed.

Tests will cover base Hongtu records, valid Codex/Hermes channel records,
legacy Hongtu aliases, unsupported suffixes, and metadata/scope disagreement.

### 4.2 Repair already-flattened evaluation evidence

A bounded, idempotent repair operation will scan only these record contracts:

- `production_recall_pending_case.v1`;
- `production_recall_accepted_case.v1`;
- `eimemory.production_recall.label_evidence`.

It will not infer a channel from a title or arbitrary metadata. The target
scope must be reconstructed from the protected pending/accepted payload and
validated with `resolve_channel_scope`. Label evidence must resolve through its
exact pending record, labeler, source partition, and referenced corpus record.
The referenced corpus record must already exist in the target channel scope.

Records are rewritten from the canonical base scope to the validated channel
scope in dependency order: pending case, label evidence, accepted case. Each
rewrite is deterministic and restart-safe. Conflicts remain at the original
scope, are counted by reason, and block readiness rather than being silently
skipped.

The operation emits one bounded audit receipt containing record IDs, before and
after scope digests, counts, and conflict reason counts. It contains no raw
query, record body, label packet, token, or secret. The pre-switch production
recall bootstrap invokes the repair before collecting or building a dataset.

### 4.3 Indexed dataset construction

Dataset construction will query the existing `report_type` metadata index for
accepted production recall cases under each exact channel scope instead of
requesting the newest generic 500 records and filtering them in Python. It will
still validate the record source, schema, embedded scope, source partition,
feature quality, stable case identity, and unique channel ownership.

The resulting gate must report all three active channels with at least five
accepted cases per channel. More OpenClaw history must not hide the bounded
Codex or Hermes sample.

## 5. Release lineage repair

`deploy/bootstrap_production_recall.py` and the new bounded channel-evidence
repair surface will be assigned to the existing memory-governance and
deployment-runtime lineage domains as appropriate. They are release-critical
production paths, not ignored documentation or test files.

Lineage tests will prove that these paths are classified, that unrelated
production paths remain unknown, and that compatibility still requires replay
evidence for every changed domain.

## 6. Bounded code-evolution effect owner

### 6.1 Detection and proposal

A system detector may open a transaction only from a typed persisted incident
whose detector identity, detection timestamp, diagnostic codes, affected
contract, and pre-detection state are present. The detector records whether the
incident was known or user-reported before detection; callers cannot override
those facts later.

Only incident classes registered in trusted release code may select a protected
test plan and allowed-file set. The live Hermes provider receives the exact
base files and incident contract and returns bounded full-file updates plus its
attestation. Unregistered incidents remain diagnostic and create no effect.

This release's production-data fix is submitted through the ordinary reviewed
development and deployment path. It is recorded as user-reported and is not a
qualifying self-evolution transaction.

### 6.2 Candidate materialization and verification

The effect owner runs outside the provider and owns a detached candidate
worktree. It will:

1. acquire the transaction lease and verify the clean exact base commit,
   remote URL digest, branch, provider advertisement, catalog snapshot, and
   proposal digest;
2. append a `candidate_materialization` intent event while the durable state
   remains `PATCH_VALIDATED`, before creating the detached candidate;
3. apply only the normalized provider updates to plan-owned files using
   no-follow regular-file checks;
4. verify the complete candidate tree digest and bounded diff contract, then
   record the `CANDIDATE_MATERIALIZED` result state;
5. construct focused, regression, and full-suite argv exclusively from the
   protected test plan;
6. append immutable verification receipts and stop on the first failure;
7. restore/remove the isolated candidate and terminalize without external
   effects when validation fails.

No shell string, environment map, credential, arbitrary pytest argument, or
path supplied by the incident/provider is executed.

### 6.3 One-shot authorization and external effects

After all verification receipts exist, the owner loads the root-owned v2
machine policy, checks the kill switch, and binds the policy to the exact
incident, detector, provider, repository, base tree, candidate tree, test plan,
installer digest, and effect set. Policy consumption is atomic and limited to
one transaction.

For each enabled effect the owner appends an intent before acting:

```text
COMMIT_INTENT -> COMMITTED
PUSH_INTENT -> PUSHED
DEPLOY_INTENT -> DEPLOYED_VERIFIED -> HEALTHY -> OBSERVING
```

The commit uses a transaction trailer and the recorded base as its sole parent.
Push is compare-and-swap against the recorded remote base. Deployment calls the
existing immutable installer for the exact candidate commit and accepts only a
matching deployment receipt, committed storage marker, release identity, and
health result. Unknown external state is quarantined; it is never blindly
replayed.

### 6.4 Observation, success, and rollback

The existing 48-hour observation offsets remain authoritative. Every sample is
bound to the deployed commit, release identity, provider advertisement,
deployment receipt, service health, and incident-specific measure.

- A hard failure or incident regression creates `ROLLBACK_INTENT`; successful
  immutable rollback and prior-release health create
  `ROLLED_BACK_HEALTHY`.
- Two consecutive noncritical degraded samples also require rollback.
- Only all required phases through 48 hours with no regression can append the
  terminal outcome and reach `SUCCEEDED_SEDIMENTED`.
- Missing time, a restarted timer, or insufficient samples remains
  `OBSERVING`; elapsed wall time alone is not evidence.

The learn-watch and nightly owners resume leases and reconcile pending intents,
but they do not synthesize missing external state. The product-completion reader
continues to require system origin, unknown-before-detection, not user-reported,
not manual, a qualifying terminal receipt, valid observation, and current
compatible lineage.

## 7. Failure handling and recovery

- Invalid channel repair candidates remain unchanged and produce bounded
  conflict diagnostics.
- A partial evidence migration is restart-safe; already-correct records are
  idempotent and conflicting destinations block.
- Dataset construction fails closed on cross-channel, source, label, or scope
  leakage.
- Provider, catalog, plan, tree, diff, test, policy, Git, push, deployment, or
  health mismatch stops at the nearest protected state.
- Failures before commit remove the detached candidate and have no production
  effect.
- Failures after deployment enter the existing rollback/recovery states and
  preserve all receipts.
- Credentials are inherited only by the separately authorized Git/deployment
  owner. They are never copied into transactions, logs, specs, commits, or
  provider requests.

## 8. Verification strategy

Implementation follows red-green-refactor at each boundary:

1. identity tests reproduce nightly flattening and prove valid channel scope is
   retained;
2. production-query tests reproduce the five-plus-five records becoming
   invisible, then prove exact repair and indexed discovery;
3. adversarial migration tests reject forged embedded scope, wrong source,
   missing pending evidence, cross-channel corpus references, and collisions;
4. lineage tests classify the bootstrap and repair paths;
5. transaction tests prove every intent/result edge, lease/CAS behavior,
   detached worktree containment, protected argv construction, and policy
   consumption;
6. failure-injection tests cover verification failure, remote ref movement,
   interrupted deploy, invalid receipt, observation regression, rollback, and
   recovery quarantine;
7. isolated end-to-end simulation proves proposal through success and rollback
   without writing production state;
8. affected suites, deployment contract tests, package/CLI checks, and the
   clean full suite run before release.

Production verification then checks the exact commit, package tree, RPC health,
managed units, three-channel production-query status, production recall gate,
release lineage, provider/catalog status, transaction ledger, and absence of
synthetic IDs. The current user-reported repair must not appear as qualifying
code-evolution evidence.

## 9. Acceptance criteria

The implementation release is complete when:

1. valid Codex/Hermes channel records survive nightly identity repair;
2. all previously flattened trusted production-query evidence is either
   restored to its exact scope or reported as a blocking conflict;
3. production-query status reports `ready=true`, all three required channels,
   and at least five accepted cases per channel from genuine label evidence;
4. production recall evaluation and strict-state activation pass for the exact
   immutable release without synthetic or reclassified cases;
5. release lineage has no unknown bootstrap/repair production path and remains
   receipt- and replay-bound;
6. a registered isolated incident can traverse the complete transaction owner,
   including success and rollback paths, with no provider-owned execution
   authority;
7. production leaves the emergency kill switch and one-shot machine policy in
   the explicitly deployed operator-selected state; missing authorization does
   not become success;
8. readiness distinguishes mechanism completion from real production
   qualification and remains `data_accumulating` when no genuine qualifying
   system incident has completed its 48-hour observation;
9. focused, affected, deployment, package/CLI, isolated simulation, and full
   regression verification pass on the exact commit;
10. the commit is pushed and deployed through the immutable installer, and
    post-deploy health and rollback readiness are verified.

## 10. Delivery sequence

1. Capture the clean baseline and add failing regression tests.
2. Repair identity/channel authority and the existing evidence envelopes.
3. Restore indexed dataset construction and production recall readiness.
4. Repair release-lineage classification.
5. Implement and failure-test the protected transaction effect owner.
6. Run isolated success/rollback simulations and the full verification matrix.
7. Commit and push the exact candidate.
8. Deploy immutably, verify production data closure, and retain the real
   evolution observation path without manufacturing a qualifying incident.
