# Project-Wide Business Closure Audit — 2026-08-23

## Authority and simulation boundary

- Synthetic records and temporary datasets prove executable mechanics only. They are deleted after each isolated test file and never count toward production L5 maturity, release acceptance, or autonomous-evolution evidence.
- Production evolution authority is limited to real, scoped business events, terminal outcomes, signed/validated receipts, and immutable release identity emitted by the deployed runtime.
- If real evidence is insufficient, the truthful state is `data_accumulating`; this audit does not manufacture an L5 claim from fixtures, rehearsals, or a passing unit test.
- The source audit is read-only. Test roots used `/tmp/eim-pytest-file-*`, were owner-private (`umask 077`), and were removed after each file, including read-only snapshot fixtures after restoring owner write permission under the disposable test root only.

## Source and entry-point coverage

Baseline source commit: `44133fb0b137633d0379e06ad927c461a4d2c370`.

`scripts/audit_business_closure.py --repo-root . --format json` validated every tracked maintained source selected from `eimemory/`, `deploy/`, `integrations/`, `scripts/`, `.github/workflows/`, and `pyproject.toml`:

| Measure | Result |
| --- | ---: |
| Maintained files | 393 |
| Maintained lines | 166,253 |
| Python callables/classes with exact spans | 5,743 |
| Native/Python syntax-invalid files | 0 |
| Unclassified maintained files | 0 |
| Business owners | 218 |
| Entry points/adapters | 61 |
| Operational gates | 64 |
| Shared contracts | 48 |
| Compatibility surfaces | 2 |

Maintained entry signals are: `__main__` (44 files), `argparse` (4), `console_script` (1), `node_plugin` (1), `package_entry_point` (1), `subprocess` (16), and `systemd_exec` (14). The only structural risk class reported is a swallowed cleanup/fallback exception in 31 files; these matches require owner/terminal-state review and are not defects merely because the syntax occurs.

Native syntax checks passed for the immutable installer, systemd ownership/runtime discovery helpers, L5 effect review, RPC port cleanup, and Hermes gateway wrapper. `compileall` passed for `eimemory`, `deploy`, `integrations`, `scripts`, and `tests`. The wheel built as `eimemory-1.11.3-py3-none-any.whl`; package import, `eimemory` CLI parsing, and the `hongtu` trusted catalog entry point passed.

## Clean executable baseline

| Field | Evidence |
| --- | --- |
| Exact source commit | `44133fb0b137633d0379e06ad927c461a4d2c370` |
| Package version | `1.11.3` |
| Python | `3.14.4` |
| Platform | `Linux-7.0.0-28-generic-x86_64-with-glibc2.43` |
| Collection | 250 test files; 3,212 test cases |
| Clean result | 3,207 passed; 5 skipped; 0 failed |
| Isolation | proposer/LLM/policy overrides unset; one test file per private short `/tmp` root; loopback RPC permitted |

The initial monolithic command was unsuitable for this host because retained pytest temporary states exhausted the 3.7 GiB `/tmp` tmpfs. The complete inventory was therefore run in deterministic sorted file order, one file per process, deleting the synthetic root before advancing. All 250 files completed. The five skips are optional external-dataset/runtime conditions in `test_deployment_tools.py`, `test_lme_evidence_mining.py`, and `test_locomo_adapter_chunks.py`; they do not skip a maintained business owner or release gate.

One product defect was reproduced before the clean run: an OpenClaw terminal payload carrying a structured capability contract reached outcome-trace validation without the runtime's trusted capability catalog, so validation stopped at the catalog requirement instead of enforcing the probe/rehearsal contract. Commit `44133fb0b` now supplies the runtime-owned catalog only when a structured contract is present; uncontracted payload shape is unchanged, and the host cannot grant executable authority. `tests/test_openclaw_outcome_hooks.py`, `tests/test_experience_outcome.py`, and `tests/test_application_catalog_bootstrap.py` passed after the repair and again in the complete baseline.

## Business-flow closure matrix

This baseline commit makes no flow-closure claim from test counts alone. Each of the ten rows is added only after the authoritative ingress, scope, durable transition, success state, failure/recovery path, and downstream consumer have been reviewed together.

## Confirmed product defects

| Counterexample | Authoritative owner | Repair/evidence | State |
| --- | --- | --- | --- |
| Structured OpenClaw outcome contracts were rejected for missing catalog before their probe/rehearsal invariant could be evaluated. | `eimemory.api.runtime.Runtime.record_outcome_trace` | Runtime selects its sealed catalog for contracted traces; exact and affected-family tests pass in `44133fb0b`. | repaired in baseline |

No other product assertion failed in the complete clean baseline. Structural risk matches and live operational observations remain findings only after a concrete producer/consumer or terminal-state invariant is disproved.

## Environment or prerequisite failures

| Observation | Classification | Resolution |
| --- | --- | --- |
| Monolithic pytest retained more temporary state than the 3.7 GiB `/tmp` tmpfs quota. | host quota | File-sharded run with immediate synthetic-data deletion completed all 250 files. |
| Sandboxed RPC tests received `PermissionError: [Errno 1] Operation not permitted` when binding loopback sockets. | sandbox capability | Clean run used the host test environment while retaining private, bounded test roots. |
| A long worktree basetemp exceeded the Unix-domain socket path limit in socket tests. | host path limit | Short `/tmp/eim-pytest-file-*` roots were used. |
| A basetemp under the group-writable workspace was rejected by production-dataset ownership checks. | intentional security gate | Private `/tmp` roots with `umask 077` satisfied the production loader contract. |
| The shared virtual environment initially exposed the previously installed wheel's entry-point metadata. | local prerequisite drift | Built and installed the exact local wheel without dependencies; catalog/owner tests passed. |

These conditions were not changed into weaker product behavior. The audit changed the verification environment so the existing security and durability contracts were exercised as designed.

## Dead-code, test, and deployment candidates

No deletion is claimed in the baseline commit. A candidate must have zero Python imports, dynamic launchers, package entry points, systemd/subprocess callers, public compatibility obligations, external integrations, and unique production-invariant tests before removal.

## L5 and code-evolution production evidence

The clean suite proves L5/readiness/code-evolution mechanisms, not current production maturity. Live evidence is reviewed separately against the deployed immutable commit, real terminal tasks/outcomes, observation windows, rollback state, and the code-implementation owner. Until those records meet the release-bound gates, the only valid conclusion is `data_accumulating` or the explicit failing gate returned by production.

## Ordered repair batches

The baseline contains one repaired authority/validation defect and no unresolved test counterexample. Repair ordering will be populated only from concrete flow review findings, in authority/storage/adapter/L5/cleanup/release dependency order.

## Final exact-commit verification

This section is intentionally limited to the baseline commit evidence above. Final verification will be rewritten with the eventual release commit's tree, wheel, full tests, CI identity, immutable deployment identity, service health, rollback availability, and live L5/code-evolution status; no future result is asserted here.
