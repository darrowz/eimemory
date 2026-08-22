# eimemory Architecture

`eimemory` is a local-first memory, knowledge, evaluation, and governed-learning
runtime. The architecture separates durable data, recall, control, and host
integration so that one production governance flow owns learning state.

## Design rules

1. `Runtime` is the public in-process facade.
2. A capability is a revisioned semantic job, not a package version, machine,
   hostname, model, or adapter name.
3. Capability-domain calls use an exact owner scope (`tenant_id`, `agent_id`,
   `workspace_id`, `user_id`) plus a logical `capability_scope`; evidence keeps
   its revision, binding, profile, provenance, and applicability context.
4. Durable records are authoritative for general records; the normalized SQLite
   v3 capability-domain tables are authoritative for their typed entities.
5. Runtime state belongs below `EIMEMORY_ROOT`, outside the source checkout.
6. Host integrations use adapter contracts instead of direct database access.
7. Learning evidence and the candidate being promoted must be bound to the same
   release identity when the claim is deployment-dependent.
8. A healthy service is necessary but insufficient for L5 closure.
9. There is no second experimental scheduler or shadow promotion state owner.
10. A verified local code patch may be applied by machine gates without a human
   approval queue; repository commit and production deployment remain explicit
   machine-policy opt-ins, not consequences of a local write.

## Runtime planes

### Data plane

```text
models.records
  -> storage.jsonl / storage.sqlite_store
  -> payload_segments + runtime_store
  -> recall and knowledge projections
  -> memory_edges and event graph
```

- `models` defines envelopes, scopes, source partitions, claims, relations,
  pages, and recall views.
- `storage` owns atomic files, JSONL segments, SQLite projections, payload
  segments, maintenance, replay buffers, and runtime state.
- `raw` preserves bounded source chunks and raw retrieval evidence.
- `knowledge` turns reviewed sources into claims, relations, pages, views, and
  synthesized briefs while retaining provenance.

For general memory records, SQLite and derived files are rebuildable
projections. They may accelerate reads but must not silently redefine the
durable record contract. The capability domain has the narrower authority model
below so its typed lifecycle and evaluation relations are queryable without a
second mutable store.

### Capability and L5 v3 data boundary

```text
exact runtime scope + logical capability scope
  -> semantic definition / revision / relation / provider binding
  -> profile / evaluation spec / evaluation run / observation
  -> immutable ledger and audit-export evidence
  -> reproducible state snapshot and L5 assessment
```

The SQLite capability v3 domain tables are the transactional authority for
definitions, revisions, relations, bindings, advertisements, profiles,
evaluation specifications and runs, knowledge links, state snapshots, and L5
assessment references. The `RuntimeStore` capability mutation boundary owns
their writes, lifecycle transitions, idempotency, and operation journal.

Observations and ledger events retain append-only evidence identity; their
SQLite rows provide typed, idempotent query paths. Content-addressed source and
evaluation artifacts retain large immutable bodies. The `capability.audit.v1`
record stream is an audit/export and recovery mirror, not a second writable
authority. JSON exports, dashboards, and optional PostgreSQL/pgvector remain
read models.

Every capability v3 key includes the exact four-part runtime scope and logical
capability scope. A cross-scope lookup, an unscoped registry request, or a
compatibility inference from a hostname/package version fails rather than
borrowing evidence. Migration is forward-only and expand-contract: schema,
idempotent writes, bounded scoped backfill, comparison/cutover, and later
cleanup are separate steps. The presence of this machinery does not claim that
all historical data is already backfilled or that a performance budget has
passed in a particular deployment.

### Recall plane

```text
query + scope + source policy
  -> intent and candidate planning
  -> lexical / SQLite / vector / graph candidates
  -> governance and visibility filters
  -> fusion + scoring
  -> RecallBundle + diagnostics
```

- `recall` contains intent and lexical indexing primitives.
- `retrieval` owns governed candidate generation, fusion, proactive policy, and
  optional PostgreSQL/vector backends.
- `embeddings` supplies local embedding support.
- `scoring` defines the canonical memory score and legacy-score adapters.

Source, tenant, agent, user, and visibility boundaries are applied before a
candidate is returned. Diagnostic fallbacks must not be treated as equivalent to
an authoritative index hit.

### Knowledge intake plane

```text
connector or paper source
  -> safe transport
  -> content-addressed raw PDF + canonical text + parser manifest
  -> normalized candidate
  -> review and policy
  -> compiled claims / relations / pages
  -> contradiction reconciliation + fail-closed refresh
  -> daily brief and recall projection
```

`intake` owns source discovery, connectors, safe transport, review, closure, and
paper metadata normalization. `knowledge` owns the compiled, queryable result
and the refresh consumer that retires stale projections before recompilation.
PDF artifacts are content-addressed below the runtime root. Optional parsers
must emit canonical text and extraction evidence; malformed, image-only, or
unavailable-parser inputs remain blocked rather than becoming empty evidence.
The runtime reader revalidates the immutable artifact manifest, PDF hash, text
hash, and both root-relative references before using canonical text. A bare
caller-supplied file reference is not source evidence.

### Experience and evaluation plane

- `experience` converts verified tool and task outcomes into sanitized outcome
  traces, diagnoses, and capability evidence.
- `evaluation` owns benchmarks, replay datasets, real-query gates, production
  recall checks, metrics, and reward calculation.
- `scoring` owns memory quality; evaluation owns task and capability quality.

Outcome success is fail-closed: malformed metrics or explicit negative signals
cannot be normalized into a pass.

Dynamic capability evaluation is selected through a sealed
`CapabilityEvaluationCatalog`. During process startup only, installed Python
entry points in `eimemory.capability_catalog.bootstrap.v1` may receive the
narrow typed bootstrap writer to register trusted executor callables, cases,
and graders. The catalog is sealed before normal runtime use. Adapter
advertisements, CLI requests, database rows, and JSON payloads are data, not
executable registration authorities. When no trusted application catalog is
installed, dynamic evaluation and consumers that require it fail closed with
`catalog_not_configured`; the runtime does not invent a default catalog.

The Hongtu production package supplies one such trusted installer in
`evaluation.hongtu_catalog`. It registers an aggregate-only recall executor and
separate Hermes/OpenClaw cases selected by exact binding ID. The executor calls
the real scoped recall path and returns only bounded counts, confidence and
scope-isolation checks; recalled payloads do not enter evaluation output. This
is an application catalog, not a general authority for adapters or stored data
to register executable code.

### Governance plane

The active control flow is:

```text
signals / corrections / outcomes / reviewed knowledge / adapter advertisements
  -> scoped capability registry + trusted evaluation catalog
  -> capability links and evidence-bound hypotheses
  -> closed_loop and episode_events
  -> correction_replay and capability replay packs
  -> autonomous_learning and candidate portfolio
  -> code proposer + code_evolution_bridge (when the goal is code-capable)
  -> isolated_evaluator + safety_replay
  -> autonomous_evolution
  -> promotion_manager
  -> rollout lifecycle, observe, reward, rollback, ledger
  -> l5_loop and l5_readiness
```

Key ownership boundaries:

| Concern | Owner |
| --- | --- |
| Capability identity, revisions, profiles, and scope | `capabilities.*`, `storage.capability_store`, `storage.migrations.capability_v3` |
| Learning goals and candidate portfolio | `autonomous_learning`, `goal_graph`, `goal_registry` |
| Code and policy evolution | `autonomous_evolution`, `code_evolution`, `code_evolution_bridge`, `rule_evolution` |
| Replay and acceptance | `evaluation.capability_catalog`, `capability_replay_*`, `correction_replay`, `live_task_acceptance` |
| Safety | `safety_replay`, `prompt_safety*`, active `safety` audit and kill-switch modules |
| Knowledge-to-capability gate | `capability_hypotheses`, capability knowledge links, `policy_trust` |
| Promotion and rollback | `promotion_manager`, `promotion_watch`, `rollout_lifecycle`, `code_automation_policy` |
| Evidence and reporting | `evidence_contract`, `capability_ledger`, dashboards and reports |
| Release closure and L5 | `release_closure*`, `closure_rehearsal`, `l5_loop`, `l5_assessment_v3`, `l5_v3_reconcile`, `l5_readiness` |

The old Karpathy utility package, standalone state machine, held-out JSONL tool,
test-only skill merger, and duplicate safety primitives were removed. Their
production responsibilities already exist in the owners above.

#### Dynamic L5 v3

L5 v3 keeps four facts separate: loop maturity, capability readiness by
revision/provider binding, adapter readiness, and deployment assurance. A
profile selects requirements from registered capabilities; it does not compile a
global strong/weak list into source code. Revision compatibility is explicit,
so an incompatible contract starts without inherited maturity while eligible
evidence can survive a compatible implementation change. Package versions,
hostnames, and machine fingerprints are diagnostics or applicability inputs,
not L5 gates by themselves.

The default dynamic path requires a trusted catalog and exact scoped evidence.
It does not fall back to retired case maps, inferred capability keywords, or a
machine-specific cohort. Historical fixed cohorts remain behind an explicit
`legacy_compatibility=True` request for maintenance/replay only, and cannot
manufacture current dynamic readiness.

In the 2026-08-22 reference deployment, `memory.recall:v1` is lifecycle-active
with reliable Hermes and OpenClaw snapshots. The other seed-manifest definitions
remain discovered. A canonical knowledge link and independently verified
hypothesis feedback advanced the resulting ready assessment to loop stage
`evolving`; it must reach `compounding` only through later repeated outcomes,
never through an operator-set label.

Discovered definitions have a separate incubation path so the active-only
profile cannot create a bootstrap deadlock. Incubation reads exact-scope
definitions, revisions, bindings, advertisements, and sealed Catalog cases;
only a complete fresh target with repeated passing preflight can transition to
active. Immediate profile acceptance then persists normal evidence. Failure
quarantines the definition instead of leaving an unverified active capability.

Reviewed knowledge reaches this control plane only through typed links and a
traceable hypothesis. It must produce bounded evaluation/replay evidence and,
where required, independent outcomes before a projection can change capability
state. Contradicted, stale, rejected, artifact-invalid, or failed evidence
remains restrictive.

#### Automatic code-patch path

For a code-capable goal, `autonomous_learning` takes either a structured patch
seed, an injected runtime proposer, or the command configured by
`EIMEMORY_CODE_PATCH_LLM_COMMAND`. That setting is a non-empty JSON argv array
(`EIMEMORY_LLM_COMMAND` is the global fallback); integrations can instead set a
runtime `code_patch_proposer` or `autonomous_code_proposer`. `code_evolution_bridge`
then requires a bounded repository root and allowlist, complete replacement
updates, a unified diff, base/subject-state digests, and focused verification
commands. A ready proposal is still read-only at that stage; an unavailable or
invalid proposal is recorded as blocked and cannot masquerade as a policy/SOP
candidate. The automatic proposal path permits only argv-shaped `python -m
compileall` targets or focused `python -m pytest -q tests/...` targets; it
rejects a broad full-suite command, shell, Git, network tools, and `python -c`.
Release-baseline validation is outside this targeted gate.

For the default dynamic path, a code candidate must name an active capability
revision and carry an evidence-bound hypothesis with qualifying independent
feedback. A generic, unscoped `code_patch` request is blocked. The sole
authority for code side effects is the deployment-controlled
`EIMEMORY_CODE_AUTOMATION_POLICY_JSON` machine-environment policy. It matches
the profile/capability/revision/scope/binding coordinates and declares allowed
actions; a proposer, incident, candidate, or patch payload cannot grant those
permissions. A missing, malformed, incomplete, or nonmatching policy blocks the
action directly.

When a learning cycle is explicitly run with `apply=True`, the existing machine
gates can promote the ready local patch without human approval only when its
matching environment policy enables `local_apply`. The promotion manager
persists a transaction before writing files, checks that the evaluated subject
state still matches, executes declared verification, and rolls back on failure.
Each apply-enabled learning/evolution cycle begins with transaction recovery;
recovery only restores known recorded content or quarantines an ambiguous
transaction, and never retries or reapplies the old patch. The cycle report
carries that result as `code_apply_recovery` (skipped when apply is disabled).
Git commit and production deployment both default to disabled and require their
own explicit machine-policy capabilities.

### Integration plane

- `api` provides memory, evolution, and runtime facades.
- `adapters.runtime` provides authentication, redaction, HTTP, channel, receipt,
  service, and internal capability-advertisement primitives shared by host
  adapters.
- `adapters.codex`, `adapters.openclaw`, `adapters.hermes`, and
  `adapters.eibrain` translate host lifecycles into the common contracts.
- Codex and Hermes expose the common recall, durable-capture, verified-outcome,
  and status operations. OpenClaw keeps lifecycle behavior in its hooks and
  exposes only bridge status to the model surface; its E2E probe is operator-only.
- Capability advertisements are validated internal data: they name supported
  revisions, operations, limits, side-effect class, contract digest, and
  evidence sources. They do not infer capability from an adapter name or expand
  a host's model-visible tools.
- `ei_bridge` routes messages and agent calls without becoming a second memory
  or governance owner.
- `cli` exposes operator workflows; `ops` contains bounded operational helpers.

Dynamic entry modules such as `prompt_safety_openclaw`, `serve_console`,
`safety.audit_verifier`, and `llm.openclaw_adapter` are launched by deployment
configuration or systemd and therefore may have no in-package caller.

## Runtime identity and deployment

An immutable production release is identified by:

- package version;
- full Git commit;
- import root;
- package tree digest;
- `/opt/eimemory/current` target.

The RPC `/health` response and the release symlink must agree. The deploy path
must then verify managed services and task-specific acceptance evidence. L5
readiness additionally requires release-bound replay, live task acceptance,
closure rehearsal, observation, and an independent readiness read.

## Safety boundary

Active L3+ safety-wire declarations name controls that exist in the production
path: `kill_switch`, `audit_verifier`, `safety_replay`, and
`promotion_manager`. Declaring a module name is not itself evidence that the
control ran; promotion still requires the corresponding replay, audit, and gate
results.

Machine gating is not a human-approval detour for the bounded local code path:
it is the decision mechanism itself. `EIMEMORY_CODE_AUTOMATION_POLICY_JSON` is
the policy authority for automatic code actions; untrusted runtime inputs only
supply matching coordinates and diagnostics. It also does not authorize
external side-effects by default; commit and deployment remain opt-in
capabilities with their own evidence and rollback requirements.

Network intake uses `intake.safe_transport`. Host credentials are read from
private files and scrubbed from inherited environments. Receipts and evidence
are bounded, signed or attested where required, and persisted without raw secret
material.

## Test boundary

The suite is organized around durable behavior:

- unit tests for record, storage, ranking, and gate invariants;
- integration tests for CLI, RPC, adapters, deployment, and host lifecycle;
- contract tests for replay, receipts, release identity, and closure;
- safety tests for transport, atomic persistence, audit, and high-risk gates.

A module is removable only when it has no production import, dynamic entry,
public adapter contract, or external integration reference. Tests that only keep
such a module alive are removed with it; overlapping production behavior remains
covered at its current owner.

## Closure boundaries not yet claimed

Knowledge refresh verifies a source artifact and recompiles from active reviewed
claims; it does not generate a new claim set from changed source text. Its
source-version digest covers the complete source-specific compilation inputs and
is revalidated inside the existing atomic transaction, so concurrent stale plans
return an all-zero retry result without retiring projections. Nightly makes one
bounded retry; this is not a distributed scheduler or refresh ledger. Capability
v3 ships forward-only schema, audit, and scoped-backfill machinery, but this
document does not claim a completed historic migration or deployment-wide
performance-budget result.
Finally, L5 remains a deployment evidence claim, not an outcome of passing
module-level gates or a healthy service.

See [Module map](modules.md) for the complete package inventory.
