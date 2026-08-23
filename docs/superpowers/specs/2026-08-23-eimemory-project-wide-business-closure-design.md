# EIMemory Project-Wide Business Closure Audit Design

**Date:** 2026-08-23  
**Target:** the first release after `1.11.3` whose source, CI result, immutable
deployment identity, and production closure evidence all refer to the same full
Git commit  
**Scope:** every maintained production entry point and dynamic launcher,
project-wide business-flow correctness, L5 v3 and bounded code evolution,
isolated data simulation, removal of genuinely dead tests/deployment surfaces,
and exact-commit production release

## 1. Decision and evidence boundary

The work will use a staged, evidence-led audit on the existing architecture.
It will not rewrite the runtime wholesale and it will not narrow the request to
L5 alone.

Three approaches were considered:

1. A one-shot rewrite could make the package look smaller, but would combine
   business repair, storage migration, governance changes, and deployment in
   one unreviewable risk boundary.
2. A L5-only patch would leave the data, knowledge, adapter, storage, and
   delivery loops outside the requested full-project closure.
3. A business-flow audit with small TDD repairs preserves the existing
   authority model while producing explicit evidence for every maintained
   entry-to-terminal path. This is the selected approach.

Simulation has one limited purpose: prove that a business path is executable
and that its state transitions, terminal effects, rollback, and cleanup are
correct. Synthetic events, outcomes, tasks, advertisements, evaluations, and
code mutations must use an isolated temporary root and must never contribute to
production L5 projections or real-business maturity. After deployment, only
real business events and outcomes may advance the live system.

## 2. Global invariants

1. There is one production state owner for each durable fact. General records
   remain authoritative in their durable record stream; typed capability v3
   entities remain authoritative in the normalized capability store; learning
   and promotion remain owned by governance.
2. Every maintained business operation has an identifiable input, validation
   boundary, durable state transition, observable terminal result, and failure
   or rollback result.
3. A healthy service, passing unit test, synthetic event, or operator-set label
   is never accepted as proof of a real business outcome or L5 maturity.
4. Release-dependent evidence must match package version, full commit, package
   tree, deployment receipt, release session, and active immutable release.
5. Trust, authority, maturity, success, and side-effect permission are derived
   by server-owned policy. Caller payloads and generated proposals cannot grant
   them.
6. Failed, stale, contradictory, cross-scope, or incomplete evidence is
   restrictive. It cannot silently disappear or promote a capability.
7. High-risk effects remain fail-closed. Recall and host continuity may remain
   fail-open only where their public contracts explicitly require it.
8. Cleanup removes a surface only after proving it has no production import,
   dynamic launcher, package entry point, systemd/subprocess reference, public
   adapter contract, persisted compatibility obligation, or external
   integration reference.
9. Tests are retained by business invariant, not by implementation nostalgia.
   Duplicate tests may be consolidated only when the surviving test preserves
   the same failure evidence and production boundary.
10. Deployment remains an immutable, rollback-capable transaction. Simplifying
    it may reduce duplication and optional helpers, but cannot remove identity,
    storage, secret, health, integration, or rollback gates.

## 3. Audit authority and coverage model

The audit unit is a maintained production source file plus its reachable public
or dynamic behavior. A generated audit ledger will enumerate every tracked
source under `eimemory/`, maintained integration code, deployment helpers, and
workflow files. Each item receives one of these dispositions:

- `business_owner`: owns a durable business state or terminal effect;
- `entry_or_adapter`: translates a public, host, CLI, RPC, timer, or subprocess
  request into a business owner;
- `shared_contract`: model, policy, validation, or storage primitive used by a
  maintained owner;
- `operational_gate`: release, health, recovery, safety, or monitoring control;
- `compatibility_surface`: intentionally retained external compatibility;
- `dead_candidate`: no demonstrated maintained reachability or obligation.

For every `dead_candidate`, the audit must record the searches and reference
classes checked before deletion. A test-only import is not enough to retain dead
production code, but absence of an ordinary Python import is not enough to
delete a dynamic or operational entry point.

Business closure is recorded by flow, not merely by file. Each flow record must
name:

- ingress and caller-controlled fields;
- server-derived scope, trust, and authority;
- authoritative durable writes and idempotency key;
- projections or caches and their rebuild/repair path;
- downstream consumer and terminal state;
- negative, retry, rollback, quarantine, or escalation state;
- observable evidence and the tests or production checks that validate it.

The minimum maintained flow families are:

1. record ingest, import/export, backup/restore, rebuild, and maintenance;
2. recall, ranking, proactive recall, vector/graph projections, and response
   evidence;
3. source discovery/intake, paper provenance, review, knowledge compilation,
   contradiction handling, and refresh;
4. experience events, terminal outcomes, corrections, replay datasets, and
   metrics;
5. capability definition/revision/binding, provider advertisement, trusted
   evaluation catalog, observation, projection, lifecycle, and regression;
6. autonomous learning, candidate selection, safety/evaluation gates,
   promotion, reward, rollback, and continuity;
7. local code proposal, preflight, bounded apply, verification, recovery,
   commit/deployment authority, and post-deploy observation;
8. L5 assessment/readiness, live acceptance, closure rehearsal, release
   lineage, and data-accumulation semantics;
9. Codex, Hermes, OpenClaw, eibrain, RPC, and bridge lifecycle contracts;
10. operator jobs, timers, diagnostics, emergency stop, immutable install,
    health verification, and rollback.

## 4. Project-wide audit and repair sequence

### 4.1 Baseline

Before source changes, capture the clean commit, version, test collection,
package build/import, CLI help construction, workflow inventory, deployment
contract suite, and current production identity. The baseline may expose known
gaps but must distinguish inherited failure from a regression introduced by
this release.

Static review combines syntax/type-shape checks, AST/import and dynamic-entry
inventory, exception/terminal-state review, SQL transaction boundaries,
time/scope/release identity propagation, subprocess/network policy, and focused
manual review of every business owner and operational gate. Broad marker scans
are diagnostic only; a `pass`, `except`, or compatibility fallback is not a bug
without a violated contract.

### 4.2 Repair rule

Every discovered behavior defect follows the same loop:

1. reduce it to a concrete counterexample at a maintained boundary;
2. add the smallest failing regression test that expresses the business
   invariant;
3. identify the authoritative owner rather than patching a downstream symptom;
4. implement the narrow repair and keep unrelated behavior stable;
5. run focused tests, then the complete affected flow family;
6. update the audit ledger and documentation with the verified disposition.

Large-file size alone does not authorize refactoring. Consolidation is allowed
when it removes a demonstrated duplicate state owner, repeated policy, repeated
deployment action, or an unreachable compatibility surface.

## 5. L5 and code self-evolution closure

L5 v3 continues to keep loop maturity, capability readiness, adapter readiness,
and deployment assurance separate. The release must not restore fixed
capability cohorts or infer capability truth from package/module names.

The maintained dynamic path must prove:

```text
real scoped outcome or incident
  -> active capability revision + provider binding
  -> fresh provider advertisement + trusted catalog case
  -> evidence-bound hypothesis/candidate
  -> bounded proposal and subject-state digest
  -> isolated preflight + replay + safety
  -> machine-policy-authorized local apply
  -> focused verification + regression verification
  -> durable transaction completion or exact rollback/recovery
  -> optional separately authorized commit/deploy
  -> current-release observation and readiness projection
```

The existing `code.implementation:v1` capability is not considered closed just
because its source, provider, policy, or timer exists. The audit must verify the
complete coordinates, catalog selection, advertisement freshness, proposal
availability, transaction recovery, terminal effect, and observation path.

An isolated temporary repository may be used to prove both successful and
failed/rolled-back code transactions. This establishes mechanism correctness
only. A production maturity claim additionally requires a non-fabricated
incident or real business improvement, the active production policy, the exact
deployed revision/binding, and current-release evidence. If those real samples
have not accumulated, readiness must expose the precise deficit as
`data_accumulating` or another existing fail-closed state.

“Self-evolution” means bounded autonomous progression from real feedback through
existing machine gates. It does not mean self-granted permissions, arbitrary
shell execution, unreviewed network access, manual maturity assignment, or
automatic deletion of its own safeguards.

## 6. Isolated business simulation

The simulation harness will use a freshly created temporary `EIMEMORY_ROOT`,
temporary repository where code mutation is involved, deterministic local fake
providers, and explicit simulated provenance. It must not inherit production
credentials, proposer commands, automation policy, HOME-based host state, or
the live storage root.

Scenario families must cover at least:

- durable ingest -> recall -> evidence response;
- source/paper -> reviewed claim -> compiled knowledge -> refresh or blocked
  contradiction;
- event -> terminal outcome -> replay/evaluation -> observation;
- capability discovery -> activation prerequisites -> acceptance -> projection
  -> downgrade on failed/stale evidence;
- learning candidate -> promotion or block -> reward -> rollback;
- code proposal -> apply -> verification -> observation, plus failed
  verification -> exact restoration;
- adapter/RPC lifecycle -> durable capture/recall/outcome/status;
- deploy staging -> identity/health/integration checks -> commit or rollback.

Every scenario captures pre-state and post-state, asserts terminal evidence,
and then destroys its isolated root. A cleanup assertion verifies that no
synthetic ID or root appears in production state, release evidence, or the
working tree.

## 7. Test and deployment simplification

### Tests

The suite will be mapped to the business-flow ledger. Cleanup candidates are:

- tests whose only subject is a deleted dead implementation;
- exact duplicate assertions already enforced at the same boundary;
- stale version/string snapshots with no external contract value;
- generated or cached artifacts accidentally present in the maintained tree.

Tests are not removed merely because they are numerous, slow, deployment
specific, or difficult to satisfy. Security, storage recovery, concurrency,
release identity, rollback, host integration, and L5 evidence tests are
preserved unless replaced by stronger equivalent coverage.

Release verification uses layers: focused TDD, affected flow families,
deployment contracts, package/CLI/static checks, isolated simulations, and one
clean full-suite candidate run. Environment-dependent suites must declare their
requirements rather than inherit uncontrolled developer state.

### Deployment

The desired operator interface remains one exact-commit command:

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
```

Internally, redundant helpers or repeated work may be consolidated. The final
transaction still must validate the commit, build the immutable release, stage
configuration without secrets in source control, migrate/verify storage,
install host integrations, switch atomically, restart managed units, verify RPC
identity and integration health, record receipt/lineage, and restore the prior
release on any post-switch failure.

## 8. Error handling and observability

- Invalid caller input returns a bounded validation error without partial
  durable effects.
- Transient work uses explicit retryable states with bounded attempts and
  idempotency keys.
- Ambiguous external effects become `delivery_uncertain`, quarantined, or the
  nearest existing fail-closed terminal state; they are not blindly retried.
- Corruption, release mismatch, stale evidence, or ambiguous code transaction
  blocks promotion/deployment and records a diagnostic that does not contain
  secrets or raw private payloads.
- Repair and rollback preserve the evidence needed to explain what changed and
  why, while avoiding a second mutable truth store.
- Runtime and readiness reports distinguish implementation availability,
  mechanism verification, real production evidence, and remaining data
  accumulation.

## 9. Acceptance and release gate

The release is eligible to merge, push, and deploy only when all of the
following are true:

1. every tracked maintained production/deployment source has an audit-ledger
   disposition and every public/dynamic entry belongs to a documented flow;
2. every confirmed defect has a failing-before/passing-after regression or an
   equivalent executable production contract;
3. all maintained flow families reach a validated terminal success and at
   least one relevant failure/rollback state in isolated execution;
4. synthetic data is absent from production and from real-maturity inputs;
5. dynamic L5 and code evolution fail closed on missing real evidence and
   accurately report live deficits;
6. deletion candidates pass reachability and obligation review, with surviving
   tests preserving the business invariants;
7. focused, affected-family, deployment, simulation, build/import/CLI, static,
   and clean full-suite checks pass;
8. the working tree contains only intended changes and passes diff review;
9. the final commit is pushed and CI succeeds for that exact commit;
10. immutable deployment succeeds and package version, commit, package tree,
    current symlink, RPC health, integrations, managed jobs, receipt, and
    lineage agree;
11. post-deploy real-business readiness is read independently. Any remaining
    sample deficit is reported honestly and allowed to advance only from later
    real outcomes.

The installer’s rollback path is the release fallback. If CI, deployment, or
post-switch verification fails, no partial-success claim is made: diagnose the
failed boundary, repair it with a regression, produce a new exact commit, and
repeat the affected verification layers.

