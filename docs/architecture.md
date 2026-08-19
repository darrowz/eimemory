# eimemory Architecture

`eimemory` is a local-first memory, knowledge, evaluation, and governed-learning
runtime. The architecture separates durable data, recall, control, and host
integration so that one production governance flow owns learning state.

## Design rules

1. `Runtime` is the public in-process facade.
2. Durable records are authoritative; indexes and views are rebuildable.
3. Runtime state belongs below `EIMEMORY_ROOT`, outside the source checkout.
4. Host integrations use adapter contracts instead of direct database access.
5. Learning evidence and the candidate being promoted must be bound to the same
   release identity.
6. A healthy service is necessary but insufficient for L5 closure.
7. There is no second experimental scheduler or shadow promotion state owner.
8. A verified local code patch may be applied by machine gates without a human
   approval queue; repository commit and production deployment remain explicit
   opt-ins, not consequences of a local write.

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

SQLite and derived files are projections. They may accelerate reads but must not
silently redefine the durable record contract.

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

### Governance plane

The active control flow is:

```text
signals / corrections / outcomes / intake evidence
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
| Learning goals and candidate portfolio | `autonomous_learning`, `goal_graph`, `goal_registry` |
| Code and policy evolution | `autonomous_evolution`, `code_evolution`, `code_evolution_bridge`, `rule_evolution` |
| Replay and acceptance | `capability_replay_*`, `correction_replay`, `live_task_acceptance` |
| Safety | `safety_replay`, `prompt_safety*`, active `safety` audit and kill-switch modules |
| Promotion and rollback | `promotion_manager`, `promotion_watch`, `rollout_lifecycle` |
| Evidence and reporting | `evidence_contract`, `capability_ledger`, dashboards and reports |
| Release closure and L5 | `release_closure*`, `closure_rehearsal`, `l5_loop`, `l5_readiness` |

The old Karpathy utility package, standalone state machine, held-out JSONL tool,
test-only skill merger, and duplicate safety primitives were removed. Their
production responsibilities already exist in the owners above.

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

When a learning cycle is explicitly run with `apply=True`, the existing machine
gates can promote the ready local patch without human approval. The promotion
manager persists a transaction before writing files, checks that the evaluated
subject state still matches, executes declared verification, and rolls back on
failure. Each apply-enabled learning/evolution cycle begins with transaction
recovery; recovery only restores known recorded content or quarantines an
ambiguous transaction, and never retries or reapplies the old patch. The cycle
report carries that result as `code_apply_recovery` (skipped when apply is
disabled). Git commit and production deployment both default to disabled and
require explicit settings.

### Integration plane

- `api` provides memory, evolution, and runtime facades.
- `adapters.runtime` provides authentication, redaction, HTTP, channel, receipt,
  and service primitives shared by host adapters.
- `adapters.codex`, `adapters.openclaw`, `adapters.hermes`, and
  `adapters.eibrain` translate host lifecycles into the common contracts.
- Codex and Hermes expose the common recall, durable-capture, verified-outcome,
  and status operations. OpenClaw keeps lifecycle behavior in its hooks and
  exposes only bridge status to the model surface; its E2E probe is operator-only.
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
it is the decision mechanism itself. It also does not authorize external
side-effects by default; commit and deployment remain opt-in capabilities with
their own evidence and rollback requirements.

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
claims; it does not generate a new claim set from changed source text. Concurrent
workers also do not gain a distributed source-version protocol merely because a
single refresh transaction is atomic. Finally, L5 remains a deployment evidence
claim, not an outcome of passing module-level gates.

See [Module map](modules.md) for the complete package inventory.
