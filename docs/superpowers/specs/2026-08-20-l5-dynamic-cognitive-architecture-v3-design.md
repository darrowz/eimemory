# L5 Dynamic Cognitive Architecture v3

Status: accepted implementation specification, 2026-08-20.

This specification is the target architecture for the next eimemory refactor.
It supersedes the fixed capability taxonomy and single-axis readiness semantics
in `docs/l5-roadmap-spec.md`. Historical L5 specifications remain evidence of
earlier decisions, but they are not implementation authority where they conflict
with this document.

## 1. Purpose

L5 is the cognitive control plane of eimemory. It must answer four different
questions without collapsing them into one hard-coded score:

1. Is the learning loop itself complete and improving from outcomes?
2. Which capabilities exist, who provides them, and how reliable are they?
3. Is a particular deployment safe and sufficiently evidenced to operate?
4. Can accumulated knowledge produce testable improvements rather than merely
   increase stored text?

The redesign removes fixed capability lists, package-version equality, machine
identity, and host-specific assumptions from L5 semantics. New capabilities and
new adapters must be registered through contracts and evidence, not by editing
the L5 engine.

## 2. Non-negotiable decisions

1. **Capabilities are data, not constants.** No `STRONG_CAPABILITIES`,
   `WEAK_CAPABILITIES`, fixed readiness list, or fixed case switch may define the
   universe of capabilities.
2. **Capability identity is semantic.** A capability ID such as
   `memory.recall` identifies a stable job. Package version, commit, hostname,
   operating system, model name, and adapter instance are observations or
   execution context, never part of capability identity.
3. **Providers are independent.** Codex, Hermes, OpenClaw, eibrain, a local
   module, or a future host may provide different subsets and revisions. L5
   consumes advertisements and results; it does not require identical tool
   surfaces.
4. **Knowledge is a hypothesis source, not proof.** Ingested knowledge may
   propose or support an improvement, but it cannot directly increase capability
   maturity, activate a policy, or apply code.
5. **Outcomes close the loop.** Maturity changes require validated observations,
   eval results, or real outcomes linked to the same capability revision and
   provider binding.
6. **L5 is not deployment health.** Service health, release identity, and
   deployment evidence are represented under deployment assurance and cannot
   manufacture cognitive maturity.
7. **Release identity is evidence context.** Commit, receipt, and release session
   bind deploy-dependent evidence. Package version remains descriptive metadata.
   Capability evidence that is proven environment-independent may survive a
   release through an explicit compatibility rule, not version equality.
8. **Machine identity is diagnostic context.** Environment fingerprints may be
   used for reproducibility, drift detection, and applicability, but a hostname
   or machine ID cannot grant or deny L5.
9. **No human approval gate in automatic evolution.** A machine policy either
   authorizes, blocks, quarantines, rolls back, or defers an action. There is no
   `pending_human_approval` state and no review queue in the direct-write path.
10. **Safety remains machine-enforced.** Removing human approval does not remove
    bounded file allowlists, command allowlists, isolated verification, atomic
    apply, rollback, kill switch, secret isolation, or external-side-effect
    policies.
11. **One production owner per state.** Capability, evaluation, knowledge,
    evolution, deployment, and adapter states each have a named owner. No
    compatibility wrapper may become a second authority.
12. **Migration is expand-contract.** Old and new schemas coexist through
    backfill and shadow comparison. Destructive cleanup happens only after
    measured parity and recovery evidence.

## 3. Current structural problem

The current implementation distributes a fixed taxonomy across multiple owners:

- `experience/capability_contract.py` maps fixed case IDs to fixed capability
  validators.
- `governance/capability_acceptance.py` embeds fixed acceptance fixtures and
  separately labels weak cases.
- `capability_replay_packs.py`, `l5_readiness.py`, and `release_lineage.py`
  contain fixed core/strong/weak capability collections.
- `autonomy_goal_queue.py` embeds capability priorities, user value, and risk.
- `self_model.py`, `replay_dataset.py`, and `world_watchers.py` infer a fixed
  taxonomy from keywords.
- release and deployment evidence are mixed into the L5 completion decision,
  which makes a cognitive claim depend on one release or machine observation.

Changing one list would leave the same architectural error elsewhere. The
refactor must replace the source of truth and migrate every consumer.

## 4. Target architecture

```text
knowledge sources + task outcomes + adapter advertisements + runtime signals
                                |
                                v
                     Capability Registry
              definitions / revisions / relations
                                |
             +------------------+------------------+
             |                  |                  |
             v                  v                  v
      Provider Bindings    Evaluation Catalog   Knowledge Links
      adapter/module       specs + graders      support/refute/apply
             |                  |                  |
             +------------------+------------------+
                                |
                                v
                    Observation and Eval Runs
                                |
                                v
                    Capability State Projector
                maturity / confidence / applicability
                                |
              +-----------------+------------------+
              |                 |                  |
              v                 v                  v
       Learning Loop      Adapter Readiness   Deployment Assurance
              |                 |                  |
              +-----------------+------------------+
                                |
                                v
                         L5 Assessment v3
                                |
                                v
             hypothesis -> experiment -> evolution -> outcome
                                ^                       |
                                +-----------------------+
```

The registry, observations, eval catalog, and projections are internal runtime
contracts. They do not require adding model-visible tools to every adapter.

## 5. Core domain model

### 5.1 Capability definition

`CapabilityDefinition` is a stable semantic identity:

```json
{
  "capability_id": "memory.recall",
  "display_name": "Memory recall",
  "description": "Recover relevant governed memory for a scoped query.",
  "owner": "retrieval",
  "status": "active",
  "risk_tier": "bounded_read",
  "tags": ["memory", "retrieval"],
  "created_at": "...",
  "supersedes": []
}
```

Rules:

- IDs use lowercase dot-separated semantic names and are immutable.
- Renaming creates an alias or a new definition with `supersedes`; stored
  evidence is not rewritten silently.
- `status` is `discovered`, `active`, `deprecated`, `retired`, or `quarantined`.
- A definition does not contain a score, provider, machine, version, fixture,
  or latest outcome.

### 5.2 Capability revision

`CapabilityRevision` contains the contract that can evolve independently from
the semantic ID:

- input/output schema or observable invariants;
- success and failure semantics;
- evidence requirements;
- dependency and composition rules;
- risk tier and side-effect class;
- compatibility declaration;
- deterministic schema digest.

Evidence always references a revision. An incompatible revision starts with no
inherited maturity. Compatible revisions may inherit only evidence explicitly
accepted by a deterministic compatibility policy.

### 5.3 Capability relations

The registry supports relations rather than a fixed hierarchy:

- `parent_of` for reporting;
- `depends_on` for readiness blocking;
- `composes` for composite capabilities;
- `conflicts_with` for mutually unsafe combinations;
- `supersedes` for lifecycle migration;
- `related_to` for discovery only.

Composite readiness is computed from the declared relation policy. It is never
inferred merely from a shared name prefix.

### 5.4 Provider binding

`CapabilityBinding` connects a capability revision to an implementation:

- provider kind: module, Codex, Hermes, OpenClaw, eibrain, or future adapter;
- stable provider instance ID assigned by eimemory;
- implementation contract digest;
- supported operations and limits;
- environment constraints;
- status and last advertisement timestamp;
- evidence and eval applicability rules.

Hostnames and executable paths are metadata. Replacing a machine does not create
a new capability. Changing an implementation contract creates a new binding
revision or invalidates only evidence whose applicability depends on that
implementation.

### 5.5 Evaluation specification

`EvaluationSpec` replaces fixed Python case collections. It declares:

- `eval_spec_id` and revision;
- target capability revision;
- optional provider binding selector;
- fixture/artifact references;
- executor ID and executor contract digest;
- grader type: deterministic code, schema/rule, or bounded model grader;
- required checks and metrics;
- retry and stability policy;
- applicability constraints;
- timeout and resource budget;
- status and provenance.

Human graders are not release or evolution gates. Ambiguous evaluations use a
bounded model grader, deterministic tie-breaker, or fail closed as inconclusive.

### 5.6 Observation and eval run

Every `CapabilityObservation` or `EvaluationRun` records:

- capability revision and provider binding;
- scope and source;
- outcome/eval identity and idempotency key;
- executor and grader revisions;
- input, output, and evidence digests;
- environment fingerprint;
- deployment authority when deployment-dependent;
- verdict: pass, fail, blocked, inconclusive, stale, or invalid;
- measured metrics and error taxonomy;
- timestamps and provenance references.

An observation never mutates accumulated maturity directly. It is appended and
then consumed by the capability state projector.

### 5.7 Capability state

`CapabilityStateSnapshot` is a reproducible projection over a bounded evidence
window:

- maturity: `unknown`, `observed`, `evaluated`, `reliable`, `regressed`,
  `quarantined`, or `retired`;
- confidence and sample sufficiency;
- reliability metrics including pass@1 and consecutive stability where useful;
- latest success/failure and regression streak;
- dependency state;
- knowledge applicability state;
- provider and environment applicability;
- input watermark and projection algorithm revision;
- exact evidence references.

Thresholds come from a versioned `CapabilityProfile`, not global source
constants. Profiles allow different operational requirements without changing
the capability universe.

## 6. L5 assessment model

`L5AssessmentV3` has independent axes.

### 6.1 Loop maturity

`loop_maturity` evaluates whether the system closes the learning cycle:

1. `observing`: outcomes and corrections are captured.
2. `diagnosing`: weaknesses are linked to capabilities and evidence.
3. `experimenting`: hypotheses produce bounded evals or replay.
4. `evolving`: verified candidates can change behavior or code.
5. `compounding`: repeated real outcomes show sustained improvement without
   unacceptable regressions.

A later failed cycle does not erase historical completion, but current maturity
is downgraded or marked regressed when required recent evidence expires.

### 6.2 Capability readiness

`capability_readiness` is a map keyed by capability revision and, where needed,
provider binding. L5 may report coverage and weighted readiness for a selected
profile, but must always retain the individual states and uncovered capabilities.

The v3 contract uses `revision_id -> (_revision | binding_id) -> state`.
`_revision` is a revision-wide state; each binding ID preserves an independent
provider state. Every state names the immutable capability-state snapshot that
supports it, and that snapshot must be listed by the assessment. This prevents a
multi-adapter assessment from collapsing Codex, Hermes, OpenClaw, or a future
provider into one mutable score.

There is no built-in strong/weak taxonomy. A weak capability is simply an active
capability whose projected state is below the selected profile's requirement.

### 6.3 Adapter readiness

`adapter_readiness` reports whether each adapter:

- advertises a valid contract;
- can emit normalized outcomes;
- satisfies its declared lifecycle obligations;
- has fresh evidence for the operations it claims;
- degrades honestly when an operation is unavailable.

Different adapters may be ready with different surfaces. OpenClaw lifecycle
hooks, Codex/Hermes model-facing tools, and operator-only E2E probes remain
separate contracts.

### 6.4 Deployment assurance

`deployment_assurance` reports:

- release commit, receipt, session, import root, and artifact integrity;
- schema migration and backfill state;
- service and RPC health;
- adapter deployment probes;
- rollback/recovery readiness;
- deployment-dependent eval coverage.

It is a deployment claim, not the identity of a capability and not proof of loop
maturity. L5 APIs expose all axes; a UI may render a summary but cannot discard
the underlying states.

## 7. Knowledge-to-capability bridge

### 7.1 Link types

`CapabilityKnowledgeLink` associates existing claims, relations, pages, source
artifacts, and synthesized knowledge with a capability revision:

- `supports`: evidence supports a capability hypothesis;
- `refutes`: evidence contradicts it;
- `informs_eval`: knowledge helped define an eval or fixture;
- `informs_change`: knowledge motivated a rule, prompt, code, or policy change;
- `explains_outcome`: knowledge explains a result after the fact;
- `limits_applicability`: source conditions narrow where an improvement applies.

### 7.2 Applicability

Each link carries source trust, review state, temporal validity, environment
constraints, contradiction state, and an applicability score with evidence.
Contradicted, stale, rejected, or unverified knowledge cannot authorize an
active change.

### 7.3 Closed-loop rule

The only path by which knowledge improves capability maturity is:

```text
reviewed knowledge
  -> explicit capability hypothesis
  -> eval or replay specification
  -> bounded candidate
  -> verified result
  -> real outcome where required
  -> capability state projection
  -> knowledge applicability feedback
```

Failed experiments reduce or qualify applicability; they are retained as useful
negative knowledge. Successful results do not rewrite the original source.

## 8. Automatic evolution contract

The evolution pipeline is fully machine-gated:

```text
capability gap
  -> evidence-bound hypothesis
  -> proposal generation
  -> bounded patch/policy candidate
  -> isolated preflight
  -> targeted tests and evals
  -> performance and safety checks
  -> atomic local apply
  -> policy-authorized commit/deploy when enabled
  -> observation
  -> accept, rollback, or quarantine
```

Required invariants:

- no human-approval state;
- generated verification commands must use a positive argv policy;
- no Git, shell, network, credential, or external-message side effect inside the
  patch verification sandbox;
- repository apply is transactional and subject-state-bound;
- an interrupted transaction is restored from recorded old content or
  quarantined; it is never blindly replayed;
- commit and deployment are explicit machine policy capabilities, not implicit
  consequences of a file write;
- an enabled autonomous deployment policy requires a verified rollback path and
  post-deploy observation;
- capability maturity changes only after persisted results are projected.

## 9. Adapter contract

The existing public recall/remember/outcome/status surfaces remain compatible.
The new capability protocol is internal and additive:

1. `advertise_capabilities(adapter_context) -> advertisement`
2. `normalize_capability_outcome(host_event) -> observation | unsupported`
3. `capability_health(binding_id) -> binding health`

An adapter advertisement contains supported capability revisions, operations,
limits, side-effect class, evidence sources, and contract digest. It must not
claim capabilities inferred only from the host name.

Adapter-specific decisions:

- **Codex:** keep the common model-facing tools; hooks and tool receipts may emit
  additional internal observations.
- **Hermes:** keep provider registry/core boundaries; advertise only capabilities
  backed by its provider and host context.
- **OpenClaw:** keep lifecycle functionality in hooks and bridge status on the
  model surface; do not expose every Python wrapper as a model tool. Hook and E2E
  outcomes feed the internal observation contract.
- **eibrain:** continue through bounded RPC/SDK contracts and advertise RPC
  capabilities independently from local module capabilities.

## 10. Storage v2 decision

The redesign keeps local-first operation but updates the database architecture.
It does not replace everything with PostgreSQL.

### 10.1 Authority by data class

There is one authority per data class:

| Data | Authority | Derived/read model |
| --- | --- | --- |
| immutable source and eval artifacts | content-addressed artifact store | metadata/index rows |
| historical observations and evidence | append-only durable record/event ledger | SQLite indexes and aggregates |
| current registry definitions and bindings | SQLite v2 domain tables with operation journal | JSON exports and dashboards |
| eval specifications and runs | SQLite v2 domain tables plus immutable evidence refs | aggregate snapshots |
| capability state and L5 assessments | reproducible versioned projection | dashboards/API views |
| cross-machine/vector search | optional PostgreSQL/pgvector projection | never local write authority |

The existing operation journal/outbox must make a SQLite transaction recoverable
until its audit record is durably exported. JSON payloads remain available for
forward-compatible evidence, but query-critical fields move into typed columns.

### 10.2 New domain tables

The implementation plan may adjust names after an ADR, but it must preserve the
following normalized entities:

- `capability_definitions`
- `capability_revisions`
- `capability_relations`
- `capability_bindings`
- `adapter_capability_advertisements`
- `capability_profiles`
- `evaluation_specs`
- `evaluation_runs`
- `capability_observations`
- `capability_knowledge_links`
- `capability_state_snapshots`
- `l5_assessments_v3`

All tables include scope, timestamps, schema revision, provenance, and stable
digests. Large bodies and binary fixtures remain artifact references rather than
database blobs.

### 10.3 Indexing requirements

Indexes must support:

- active definitions by scope and status;
- revisions by capability and effective time;
- bindings by provider and capability;
- pending/stale evals by capability, binding, and profile;
- observations by capability, binding, outcome time, and verdict;
- knowledge links by capability and knowledge record;
- latest snapshots by profile and evidence watermark;
- idempotency and semantic-key uniqueness where appropriate.

JSON expression indexes are compatibility aids, not the permanent query plan for
high-volume capability and eval data.

### 10.4 Migration protocol

1. Inventory and measure the old data.
2. Add new tables without changing reads.
3. Add idempotent dual writes at the owning boundary.
4. Backfill in bounded batches with cursor, counts, and digests.
5. Rebuild projections and compare old/new semantics in shadow mode.
6. Cut over reads behind a feature flag.
7. Keep rollback to the previous reader while dual writes remain active.
8. Stop old writes only after stable observation.
9. Remove old fields/constants and compatibility code in a later cleanup task.

Schema and data migrations are separate. Deployed migrations are immutable;
corrections use new forward migrations.

## 11. Performance contract

No new architecture is accepted without a pre-change baseline. Measure at least:

- recall p50/p95/p99 and result parity;
- record append and atomic mutation latency;
- capability registration and advertisement latency;
- observation ingestion throughput and idempotency cost;
- eval scheduling and result projection throughput;
- L5 assessment latency by capability count;
- SQLite size, WAL growth, index size, and compaction time;
- backfill throughput, lock time, restart behavior, and memory use;
- optional PostgreSQL projection lag;
- adapter outcome normalization latency and error rate.

Budgets are stored in a versioned benchmark profile after the baseline run. They
must include small, medium, and large synthetic datasets. A stage cannot promote
if it exceeds its budget unless a new ADR explains and measures the trade-off.

The L5 projector must be incremental by evidence watermark. It must not scan all
records for every readiness request. Expensive aggregates are materialized and
invalidated by new observations, definition revisions, or profile changes.

## 12. Security and privacy

- Capability advertisements are untrusted input and validated against size,
  schema, ID, and provenance limits.
- Knowledge, fixtures, model graders, and imported definitions cannot inject
  executable commands.
- Environment fingerprints exclude secrets, raw paths where unnecessary, and
  stable personal identifiers.
- Cross-scope evidence is rejected unless an explicit visibility policy permits
  it.
- Model graders never receive credentials or unrestricted raw private payloads.
- External side effects require a declared capability risk class and machine
  policy; absence of policy means blocked, not approved-by-default.

## 13. Documentation and evidence discipline

For every implementation work package, preserve a machine-checkable mapping:

```text
requirement
  -> contract or invariant
  -> characterization/RED test
  -> implementation change
  -> focused GREEN test
  -> benchmark or migration evidence
  -> automated fresh-context review
  -> remaining limits
```

The review is automated and evidence-driven; it is not a human approval gate.
Facts have one canonical owner:

- this specification owns target architecture;
- the implementation plan owns sequence and status;
- ADRs own decisions and alternatives;
- schema/migration files own executable data changes;
- benchmark artifacts own measured budgets;
- changelog owns released history;
- module map owns production ownership after cutover.

## 14. Acceptance criteria

The architecture is complete only when all of the following hold:

1. Registering a new capability, revision, provider binding, and eval requires no
   modification to L5 core code.
2. Removing or deprecating a capability does not corrupt historical evidence.
3. Codex, Hermes, OpenClaw, and eibrain can advertise different capability sets
   and all pass their own contract suites.
4. Package version and hostname changes alone do not alter capability maturity.
5. Deployment-dependent evidence is still bound to verified commit/receipt/session.
6. Knowledge can create a traceable hypothesis and eval, but cannot self-promote.
7. A contradiction, stale source, failed eval, or bad outcome propagates to the
   affected capability state and does not leave an active stale projection.
8. Automatic code evolution can propose, verify, apply, recover, and observe a
   bounded change without human approval.
9. Storage backfill is restartable, idempotent, digest-verified, and reversible
   at the reader cutover.
10. Shadow L5 v3 output is explainable against exact input evidence.
11. Performance budgets pass on all declared scale tiers.
12. Old fixed taxonomies and duplicate compatibility paths are removed only
    after parity is demonstrated.

## 15. Explicit non-goals

- A universal ontology that predicts all future capability names.
- Requiring every adapter to expose identical model tools.
- Treating knowledge volume, model size, tool count, or version number as
  intelligence.
- Replacing local-first durability with a mandatory network database.
- Allowing an LLM-generated definition or eval to execute without schema,
  provenance, resource, and policy validation.
- Claiming production L5 from unit tests, service health, or module presence.
