# L5 v3 Production Closure Review — 2026-08-22

## Scope

This report compares the live Hongtu production deployment with the target in
`docs/superpowers/specs/2026-08-20-l5-dynamic-cognitive-architecture-v3-design.md`.
It separates implementation presence, production evidence, and remaining
closure work. Service health alone is not treated as L5 evidence.

Exact runtime scope:

```text
tenant_id=default
agent_id=hongtu
workspace_id=embodied
user_id=darrow
capability_scope=global
```

## Production identity

| Field | Value |
| --- | --- |
| Package version | `1.9.137` |
| Deployed commit at evidence capture | `90d20ab42095cf061c11a905b371ce710c08ddde` |
| Import root | `/opt/eimemory/releases/90d20ab42095cf061c11a905b371ce710c08ddde/eimemory` |
| Package tree digest | `407c3caa5db97d07cb275402b101e840168632f58844cfa816ee0da935169e1f` |
| Runtime store | `/var/lib/eimemory` |
| RPC health | ready; runtime identity matched |
| Storage migration | complete; no pending migrations |
| Capability v3 backfill | complete |

The Git repository was six commits ahead of `origin/master` before this report
and its documentation update. The final documentation commit, push, and exact
immutable deployment are recorded after verification rather than anticipated
here.

## Live storage and learning state

| Entity | Count |
| --- | ---: |
| General records | 72,246 |
| Events | 10,103 |
| Event outcomes | 7,265 |
| Capability definitions | 11 |
| Capability revisions | 11 |
| Provider bindings | 2 |
| Adapter advertisements | 2 |
| Evaluation specifications | 4 |
| Capability knowledge links | 1 |
| Capability hypotheses | 1 |
| Evaluation runs | 12 |
| Capability observations | 12 |
| Capability state snapshots | 5 |
| L5 v3 assessments | 3 |

The seed manifest registered eleven definitions. Only `memory.recall` has been
lifecycle-activated; the other ten remain discovered and do not contribute to
current readiness. Descriptor status remains the immutable initial value, while
the lifecycle-effective state for `memory.recall` is active.

## Current L5 v3 axes

```text
status=ready
loop_maturity=evolving
capability_ready=true
adapter_ready=true
deployment_required=false
deployment_blocking=false
gaps=[]
```

| Revision and binding | Maturity | Confidence | Adapter |
| --- | --- | ---: | --- |
| `memory.recall:v1` / `binding.hermes.memory-recall:v1` | reliable | 0.9625 | ready |
| `memory.recall:v1` / `binding.openclaw.memory-recall:v1` | reliable | 1.0 | ready |

Observations are independently partitioned by provider binding:

- Hermes and OpenClaw each have independent hypothesis-bound acceptance
  evidence in addition to their prior observations.

The trusted catalog is loaded from the `eimemory` distribution through
`eimemory.capability_catalog.bootstrap.v1`; it is sealed with one executor and
two provider-specific cases. Adapter advertisements remain data and never gain
executable registration authority.

Both snapshots now report `knowledge_context_checked`. A canonical PDF artifact
for arXiv `2603.07670` was verified, one approved/high-trust retrieval claim was
linked to `memory.recall:v1`, and a bounded hypothesis passed independent Hermes
and OpenClaw catalog evaluations. The hypothesis gate is allowed from verified
feedback; the knowledge record did not self-promote maturity.

## Completion against the 12 original acceptance criteria

| # | Original criterion | Status | Evidence / remaining limit |
| ---: | --- | --- | --- |
| 1 | Add capability/revision/binding/eval without L5 core edits | Complete | Dynamic contracts, registry, profiles, catalog entry point, provider-specific case selectors. |
| 2 | Deprecation/removal preserves historical evidence | Complete | Immutable entities, lifecycle events, append-only observations, compatibility tests. |
| 3 | Codex/Hermes/OpenClaw/eibrain advertise independently | Complete in implementation; live proof for Hermes/OpenClaw | Shared adapter contract suites; two live provider bindings and advertisements. Codex/eibrain remain contract-tested rather than active in this production profile. |
| 4 | Version/hostname alone cannot alter maturity | Complete | Semantic capability identity and explicit applicability/binding rules. |
| 5 | Deployment-dependent evidence binds to commit/receipt/session | Complete in contract | Deployment assurance remains a separate axis. Current recall observations are portable, so deployment evidence is correctly not required. |
| 6 | Knowledge creates hypotheses/evals but cannot self-promote | Complete | Canonical artifact → approved link → candidate hypothesis → two independent provider evals → eligible feedback; snapshots report `knowledge_context_checked`. |
| 7 | Contradiction/stale/failure propagates and invalidates stale state | Complete | Knowledge refresh, applicability, observation, projection, and regression tests. |
| 8 | Automatic code evolution proposes/verifies/applies/recovers/observes | Partially evidenced | Exact machine policy now authorizes local apply, commit, and deployment only for the future `code.implementation:v1` Hermes binding. The capability/evaluator are not yet active and this review did not fabricate a production mutation. |
| 9 | Backfill is restartable/idempotent/digest-verified/reversible at cutover | Complete | Scoped production backfill finished; migration state and dual-write verification available. |
| 10 | Shadow/v3 output is explainable from exact evidence | Complete | Projection/assessment digests, watermarks, snapshots, and evidence references are retained. |
| 11 | Performance budgets pass on declared tiers | Complete | `docs/audit/l5-v3-performance-baseline.md` and bounded production execution. |
| 12 | Fixed taxonomy/duplicate paths removed only after parity | Complete | Dynamic default, explicit legacy compatibility only, cleanup and ownership documentation. |

Result: **11 of 12 criteria fully evidenced (91.7%)**, with one criterion
partially evidenced in production. The architecture and WP0–WP15 implementation
surface are present, but this is not a claim of final cognitive L5 maturity.
The current loop stage is honestly `evolving`, not `compounding`.

## Work-package completion

| Work packages | Implementation status | Operational qualification |
| --- | --- | --- |
| WP0–WP3 | Complete | Baseline, ADR/contracts, performance baseline, storage schema. |
| WP4–WP7 | Complete | Registry/profiles, advertisements, catalog, observations/ledger; live Hermes/OpenClaw evidence. |
| WP8 | Complete | Live canonical knowledge link, hypothesis, independent provider evaluations, feedback, and updated snapshots. |
| WP9–WP12 | Complete | Projector, four-axis assessment, consumer migration, release separation. |
| WP13 | Implemented and test-evidenced | Fresh production code-mutation exercise remains intentionally outstanding. |
| WP14–WP15 | Complete | Backfill/shadow machinery, dynamic cutover, legacy isolation, cleanup. |
| WP16 | Completed with operator-approved full-suite waiver | Static/package/docs checks passed; 3,068 tests passed in the clean full run before five HOME-dependent watchdog failures; affected modules then passed 416/416 and watchdog 10/10. The operator explicitly stopped a third full run. |
| WP17 | Ready for exact final commit push/deployment | Push and deploy the documented tree, then verify identity and readiness. |

## Repairs required to reach the live ready state

The production exercise found and fixed failure classes that isolated tests had
not previously closed:

1. Runtime timestamps used the host offset instead of canonical UTC.
2. Frozen profile requirements attempted to `deepcopy(mappingproxy)` on Python
   3.14.
3. Governed probes dropped `verdict`, causing a passing execution to persist as
   blocked.
4. EvaluationSpec IDs were stable while `created_at` changed, causing repeat-run
   idempotency conflicts.
5. Hermes and OpenClaw initially shared an ambiguous catalog target; they now use
   separate binding selectors and evidence chains.

## Remaining work

1. Exercise one bounded production code-evolution transaction, including
   focused and regression verification, commit, push, immutable deployment,
   post-deploy health, and rollback/recovery evidence, before claiming criterion
   8 as fully production-proven.
   The installed policy already requires exact profile/capability/revision/scope/
   binding coordinates and rejects `memory.recall`; activation still requires a
   real code capability, trusted catalog case, non-fabricated incident, and
   complete regression commands.
2. Advance loop maturity only from repeated real outcomes. Do not manually set
   `evolving` or `compounding`.
3. Keep provider advertisements fresh through their owning host lifecycle.
4. Push and deploy this exact documentation tree so source, GitHub, and
   production identity agree. For this closure the operator explicitly waived
   another full run after the isolated failure groups passed; future source
   changes restore the normal full-suite requirement.

## Final verification evidence

- `git diff --check`: passed.
- `compileall` for `eimemory`, `tests`, and `deploy`: passed.
- Documentation relative-link check across twelve authoritative files: passed.
- Wheel build and mainline catalog entry point: passed.
- First full run under inherited production environment: 3,020 passed, 4
  skipped, 51 failed; failures were traced to inherited prompt/model/proposer
  settings and group-writable umask.
- Clean-environment affected-module run: 416 passed after installing the
  declared PDF extra and clearing exhausted tmpfs artifacts.
- Clean full run with isolated HOME: 3,068 passed, 2 skipped, 5 failed solely
  because the empty HOME correctly lacked `~/.openclaw/openclaw.json`.
- OpenClaw watchdog group with clean environment and real HOME: 10 passed.
- A third full run using the corrected environment was started and explicitly
  stopped by the operator; no green full-suite claim is made.
