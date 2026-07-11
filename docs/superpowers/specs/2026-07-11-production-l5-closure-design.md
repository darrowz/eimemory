# Production L5 Closure Design

## Goal

Make the production `hongtu/embodied/darrow` scope reach L5 through real,
auditable execution evidence. The release must not manufacture task-success
records, reinterpret `not_run` as pass, or remove weak capabilities from the
readiness gate.

## Current root cause

The 1.9.16 replay and readiness gates fail closed correctly, but the evidence
producer is incomplete:

- OpenClaw terminal hooks serialize `outcome` and `verifier` as strings. The
  attribution and replay layers require a structured verifier with
  `passed=true`, so normal successful traces are rejected.
- Production outcome traces have no case-specific capability contract. The
  replay executor therefore falls back to brittle text matching and cannot
  prove any of the twelve weak-capability cases.
- Patch-promotion metrics mix invalid preflight candidates with executed
  deployment attempts and count every retry instead of the latest result per
  candidate. This prevents a repaired candidate from recovering while still
  failing to distinguish generation quality from deployment quality.
- The L5 assessment only sees rollback data nested in the current autonomous
  report. It cannot reuse an already executed policy/lifecycle rollback record.

## Considered approaches

### Structured contracts and active acceptance probes (selected)

Add a strict capability evidence contract, preserve it through OpenClaw and
outcome-trace storage, execute twelve non-destructive acceptance probes, and
replay only contract-backed evidence. This gives deterministic, repeatable,
side-effect-free evidence without inflating production task-success metrics.

### Passive collection only

Wait for normal OpenClaw traffic to eventually produce matching traces. This
does not close the loop because current traffic is mostly generic
communication, and the old hook loses the required structured verifier data.

### Relax readiness or remove weak capabilities

Lower thresholds, accept text-only evidence, or delete weak capabilities from
the L5 set. This would make the number green while preserving the broken
system, so it is rejected.

## Architecture

### 1. Capability outcome contract

Add `eimemory.experience.capability_contract` with schema
`capability_contract.v1`. A contract contains:

- `capability` and one known `case_id`;
- structured `observations` used by that case's validator;
- a list of named checks, all with `passed=true`;
- at least one source record ID that exists in the same scope;
- `probe=true` for controlled acceptance probes.

The validator owns all case-specific logic. Unknown cases, missing source
records, failed checks, incomplete observations, or mismatched capability/case
pairs fail closed.

Outcome traces remain `outcome_trace.v1` for compatibility and carry the
contract as an optional nested object. When a contract is present,
`record_outcome_trace` validates it before persistence and hoists capability,
case, and evidence-source metadata for indexed lookup.

### 2. OpenClaw producer repair

The terminal hook must preserve structured dictionaries instead of converting
them to strings:

- `outcome` is stored as `{status, success, rehearsal}`;
- `verifier` is stored as `{passed, method, evidence_refs, checks}`;
- an explicit `capability_contract` supplied by event, outcome, or task context
  is passed through unchanged and validated by the experience layer.

The hook must not infer a passing capability contract from generic assistant
text. Missing explicit contract data remains non-L5 evidence.

### 3. Non-destructive acceptance probes

Add `eimemory.governance.capability_acceptance`. It executes all twelve weak
capability contracts against deterministic validators:

- search: recency/source trust, GitHub ranking criteria, primary-source proof;
- research: citation/fact separation, conflict handling, actionable decision;
- UUMit: requirement acceptance, quality gate, post-delivery follow-up;
- device: physical channel, missing-target clarification, reversible boundary.

Each probe first persists an immutable `capability_probe_result` containing its
input, check results, observation, and digest. A successful probe then records
one linked outcome trace with structured verifier and capability contract.
Probe traces are marked `rehearsal=true`; they may prove replay capability but
must remain excluded from production task-success metrics.

Every invocation uses a fresh execution ID, so the twelve cases have distinct
source records and regressions can supersede earlier passes.

### 4. Attribution and replay

Capability attribution prefers explicit contracts. Legacy text attribution is
retained for historical dashboards but marked `contract_verified=false` and
cannot satisfy L5 replay.

The replay executor accepts only:

- source `eimemory.experience.outcome_trace`;
- `outcome.status` in a successful state;
- `verifier.passed=true`;
- a valid matching capability contract;
- a retrievable probe/source record in the same scope.

Keyword matching is removed from the L5 path. The latest execution per
capability/case continues to determine readiness.

### 5. Patch and rollback evidence

Dashboard patch metrics are split into:

- candidate validity: whether a code-patch candidate reached an executable
  contract;
- deployment success: latest executed result per candidate, excluding records
  that never reached a deployment attempt.

L5 requires at least one successful, fully verified autonomous deployment in
the current schema. The final release will exercise the real promotion manager
on a small versioned audit artifact through verification, commit, immutable
deployment, post-deploy health, commit identity, and rollback evidence. Invalid
historical candidates remain visible in candidate-validity reporting but do
not masquerade as deployment failures.

The L5 loop imports executed rollback/quarantine ledger references into the
closed-loop report. Observation mode may use an explicit stop condition;
apply mode must carry a real rollback reference.

### 6. Closure orchestration

Extend closure rehearsal into this sequence:

1. execute and persist all acceptance probes;
2. run the twelve replay cases;
3. require distinct sources and pass rate `1.0` for each weak capability;
4. run skill reuse and non-destructive rollback rehearsal;
5. run an L5 observation cycle and persist its complete assessment;
6. recompute dashboard and readiness.

The command returns success only when readiness reports `current_stage=L5` and
`readiness_score=1.0`. Any failed stage prevents later success evidence from
being written.

## Safety and integrity

- Probes perform no network send, spend, deletion, credential use, deployment,
  or physical device action.
- Probe evidence is separately labelled and never counts as a user task
  success.
- Source records are scope-bound and must be retrievable.
- A boolean `passed=true` without case checks and source evidence is rejected.
- Deployment evidence is produced only after the immutable release, service
  restart, health check, and commit/version identity checks succeed.

## Verification

Completion requires all of the following fresh evidence:

- red/green regression tests for contract validation, hook structure,
  acceptance probes, replay trust, latest metrics, and assessment rollback;
- associated local and remote layered suites pass;
- compileall, AST parse, version consistency, and `git diff --check` pass;
- GitHub, local branch, and `/dev-project/eimemory` share one commit;
- `/opt/eimemory/current`, RPC health, gateway health, and service states match
  the release;
- production closure rehearsal succeeds;
- production readiness reports L5 with twelve latest passing weak-capability
  replays, complete trusted assessment, verified patch deployment, and rollback
  evidence.
