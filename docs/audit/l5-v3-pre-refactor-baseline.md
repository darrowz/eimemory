# L5 v3 Pre-refactor Baseline

Status: WP0 custody and source baseline, recorded 2026-08-20.

## Authority snapshot

| Field | Value |
| --- | --- |
| Repository | `E:\eimemory` |
| Branch | `master` |
| Upstream | `origin` -> `https://github.com/darrowz/eimemory.git` |
| Upstream baseline at capture | `6901442f3c54c0be559bcff00cc44327f11aa043` (`chore(release): 1.9.135`) |
| Refactor starting commit | `4a0cb0b5d70d8ed53eeb2cbb2b20f84ef43a46a0` (`refactor: consolidate governance and prepare L5 v3`) |
| Working tree at capture | clean; `master` is ahead of `origin/master` by one commit |
| Declared package version | `1.9.135` in `pyproject.toml` and `eimemory/version.py` |
| Python | `3.14.3` |
| SQLite | `3.50.4` |
| Platform | Windows 11 `10.0.22631` |

This document is a source-control boundary, not release or production evidence.
No push, deployment, version bump, or production-L5 claim is implied by the
baseline commit.

## Baseline commit ownership

Commit `4a0cb0b` contains the pre-refactor work that must remain intact while L5
v3 is implemented:

| Group | Included work | Owner after baseline |
| --- | --- | --- |
| Module cleanup | Removed autonomous experiment stack, duplicate safety prototypes, state-machine wrappers, dead OpenClaw tool wrapper, and their tests | Current active governance, safety, adapter, and evaluation owners listed in `docs/modules.md` |
| Adapter closure | Codex/Hermes common tool contract, OpenClaw hook/E2E surface, manifests, runtime wiring, deployment checks | `adapters.*`, `integrations/*`, deployment verifier |
| Automatic evolution | Proposal boundary, positive command policy, preflight/apply/recovery behavior, direct machine-gated write path | `governance.code_evolution*`, `promotion_manager`, `autonomous_learning` |
| Knowledge closure | Canonical PDF artifacts, verified text loading, paper identity, refresh and stale-projection retirement | `intake.papers.*`, `knowledge.refresh`, `knowledge.projectors` |
| Documentation | Architecture/module map, release/deployment notes, cleanup history, L5 v3 target design and plan | documentation owners named by the L5 v3 specification |

The commit changed 118 files: 6,791 insertions and 9,599 deletions. Future
refactor commits must not fold unrelated cleanup corrections back into this
baseline without recording their ownership and reason.

## Validation status at baseline

- `python -m compileall -q eimemory` completed successfully before the commit.
- `node --check integrations/openclaw/eimemory-bridge/index.js` completed
  successfully before the commit.
- A broad targeted pytest batch was deliberately interrupted at user direction
  before completion. It is **not** evidence for this commit and must not be
  reported as a passing regression run.
- No full suite was run for this commit.

The first implementation task that touches any baseline owner must run its own
focused characterization and regression tests. WP16 is the only planned full
integration suite gate.

## Fixed-taxonomy inventory

The following production areas currently encode a fixed capability universe and
are migration targets, not new authorities:

| Owner | Fixed construct | Required v3 replacement |
| --- | --- | --- |
| `experience/capability_contract.py` | case ID to capability validator mapping | revisioned evaluation/contract catalog |
| `governance/capability_acceptance.py` | embedded core and weak acceptance fixtures | registered `EvaluationSpec` artifacts |
| `governance/capability_replay_packs.py` | `CORE_REPLAY_CAPABILITIES` | profile/registry-driven replay selection |
| `governance/l5_readiness.py` | `READINESS_CAPABILITIES`, `STRONG_CAPABILITIES`, `WEAK_CAPABILITIES` | profile-specific capability state query |
| `governance/closure_rehearsal.py` | core replay import | declared closure profile requirements |
| `governance/release_lineage.py` | core/weak replay assumptions | deployment applicability declarations |
| `governance/autonomy_goal_queue.py` | default capability, value, and risk maps | registry/profile/policy query |
| `governance/self_model.py` | `CAPABILITY_DIMENSIONS` and fallbacks | registry rendering and explicit unclassified state |
| `governance/replay_dataset.py`, `world_watchers.py` | keyword-to-fixed capability classification | data-driven attribution rules plus `unclassified` |

Existing test constants are historical fixtures until the corresponding
production owner is migrated. They must be updated as behavioral contract tests,
not deleted simply to remove source-text references.

## Storage baseline and decision boundary

The current runtime uses durable record/event payloads, SQLite projections and
runtime access, and content-addressed artifacts for the newly closed PDF path.
L5 v3 must not introduce a second uncontrolled state store.

The accepted target is documented in
`docs/superpowers/specs/2026-08-20-l5-dynamic-cognitive-architecture-v3-design.md`:

1. Immutable source/eval bodies remain content-addressed artifacts.
2. Historical observation and evidence remain append-only durable records/events.
3. Registry definitions, bindings, evaluation runs, and current state use
   normalized SQLite v2 domain tables with the operation journal/outbox.
4. PostgreSQL/pgvector stays optional and projection-only.

No migration, schema change, or backfill has been performed by WP0.

## Baseline performance state

No valid L5 v3 performance baseline exists yet. WP2 must create reproducible
small/medium/large fixtures and record measured recall, write, projection,
SQLite/WAL, backfill, adapter, and evaluation metrics before Storage v2 is
allowed to change active read paths.

Host, OS, Python, SQLite, model, and provider metadata are benchmark context.
They are not capability identity and are never a standalone maturity gate.

## Refactor custody rules

1. Start all L5 v3 changes from `4a0cb0b`.
2. Retain the current baseline module map and architecture documentation until
   the cutover evidence explicitly replaces the corresponding statement.
3. Keep version `1.9.135` unchanged during architecture, schema, shadow, and
   internal cutover work unless a release task explicitly authorizes a bump.
4. Never use package version or hostname as a capability ID, readiness input, or
   compatibility shortcut.
5. Preserve the existing automatic evolution machine gate; do not add a human
   approval queue while introducing dynamic capabilities.
6. Preserve current public adapter surfaces unless an additive contract and
   adapter-specific regression demonstrate a compatible change.
7. Do not push/deploy this baseline or any pre-WP16 refactor commit. The user
   has granted standing authorization for WP17 to push and deploy automatically
   only after WP16 passes all final integration gates.

## WP0 completion evidence

- `git status --short --branch` was clean at the baseline snapshot.
- The starting commit, parent, upstream, declared version, runtime versions,
  modified ownership groups, test limitation, and fixed-taxonomy targets are
  recorded above.
- The next task is WP1: ADRs and validated v3 contracts.
