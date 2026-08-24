# Project-Wide Business Closure Audit — 2026-08-23

## Authority and simulation boundary

- Synthetic records and temporary datasets prove executable mechanics only. The sharded baseline and candidate rehearsals use managed roots that are deleted after each isolated run, and fixtures never count toward production L5 maturity, release acceptance, or autonomous-evolution evidence. Pre-repair residual roots are listed separately below rather than being treated as evidence.
- Production evolution authority is limited to real, scoped business events, terminal outcomes, signed/validated receipts, and immutable release identity emitted by the deployed runtime.
- If real evidence is insufficient, the truthful state is `data_accumulating`; this audit does not manufacture an L5 claim from fixtures, rehearsals, or a passing unit test.
- The source audit is read-only. Test roots used `/tmp/eim-pytest-file-*`, were owner-private (`umask 077`), and were removed after each file, including read-only snapshot fixtures after restoring owner write permission under the disposable test root only.

## External audit worktree intake decision

The five `/dev-project/` audit deliverables were reviewed as candidate inputs rather than accepted on report labels alone. None of their uncommitted product changes is included in `1.11.4`:

| Worktree/domain | Decision | Code-level reason |
| --- | --- | --- |
| D1 recall | defer | The report itself leaves three intent families unable to accept fallback-only recall end to end. Its chat-log demotion promises that logs never occupy the top three, but the implementation appends the demoted tail whenever fewer than three primary results exist, so a demoted log can still rank first through third. |
| D2 knowledge | defer | The source-registry scan-outcome breaker has no production caller, and the ChatPaper fallback declares a timeout and lock that no execution path uses. The direct fallback iterates `None` when metadata has no categories, while parse failures record fallback pressure without performing the claimed mid-run fallback, so the new operational guarantees are neither safe nor wired closed. |
| D3 runtime/deploy | reject | The new release preflight and failed-unit reset helpers are not connected to the immutable installer or CLI. The reset helper attempts user-to-system `systemctl` fallback, while the root-resolution change breaks existing default runtime/CLI behavior and parses `--allow-dev-root` without applying it. The recall normalizer also conflates execution success with gate acceptance. |
| D4 capabilities/L5 | reject | The readiness report describes independent `min` caps as multiplicative and raises the displayed score without adding real evidence. Its gate classifier treats any `ok=true` payload as accepted even when recall status is rejected, strict state is rejected, migration remains pending, or lineage is incompatible; all four counterexamples retained `L5/1.0`. Newly advertised owner fields and incubation actions also lack dedicated failure-path assertions. |
| Coupling inventory | backlog input | This is a broad documentation inventory, not an executable repair batch; several severity labels treat ordinary configured storage defaults as high coupling. Individual items require counterexample-based triage before code changes. |

The D1-D4 reports, task files, and transcripts remain useful follow-up evidence, but their uncommitted worktrees are neither release artifacts nor proof of production L5 maturity. This intake decision preserves the already verified `1.11.4` repair set and avoids importing disconnected or semantics-weakening changes during release finalization.

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

After the two proven-dead deployment helpers were removed and the release owner was repaired, the `1.11.4` candidate worktree inventory contains 391 maintained files, 166,016 maintained lines, and 5,737 Python callables/classes. The only category change is operational gates from 64 to 62; syntax remains valid, all files remain classified, and the 31 reviewed `except_pass` signals are unchanged. The audit now ignores tracked paths already deleted in the worktree, with a regression proving deletion cannot be misreported as invalid syntax.

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

The matrix records mechanism closure and current evidence state separately. `Closed` means the maintained producer, authority, durable write, terminal consumer, and negative/recovery path agree and have executable evidence. It does not convert synthetic evidence into a live maturity claim.

| Flow | Ingress | Authority/scope | Durable transition | Terminal success | Failure/rollback | Executable evidence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1. Record ingest, import/export, backup/rebuild/maintenance | `MemoryAPI.ingest`, `RuntimeStore.append`, knowledge-pack CLI/API, storage deploy helpers | `ScopeRef`; validated `RecordEnvelope`; SQLite transaction is the write owner and export outbox feeds the durable JSONL mirror | `sqlite_store.upsert` + `export_outbox`; payload segments; `rebuild_sqlite_from_jsonl`; storage snapshot/restore journal | returned record/import receipt; rebuild `ok`; maintenance `ok` with zero pending export | transaction rollback before commit; pending outbox remains retryable; invalid JSONL blocks replace; snapshot restore/recovery returns explicit error/rollback | `test_storage.py`, `test_storage_deploy.py`, `test_storage_cli.py`, `test_storage_maintenance.py`, `test_runtime_store_concurrency.py` in the 379-pass data-plane suite | Closed |
| 2. Recall/ranking/proactive/vector/graph/evidence | `MemoryAPI.recall`, `GovernedRecallEngine.recall`, adapter `prefetch`/`proactive_prefetch` | exact/authorized runtime scopes, source visibility and governance filters are derived before fusion | lexical/SQLite/vector/graph read models; proactive turn/ack/terminal rows; bounded recall audit/evidence records | `RecallBundle` with selected records, diagnostics, policy attribution and injection plan; proactive turn terminal outcome | optional backend/diagnostic fallback is labeled; cross-scope candidates are discarded; answer-evidence gates fail closed; projection rebuild/sync remains retryable | `test_recall_engine.py`, `test_recall_fusion.py`, `test_postgres_vector_sync.py`, `test_postgres_vector_source.py` in the 379-pass suite | Closed |
| 3. Source/intake/paper/review/knowledge/contradiction/refresh | source registry/connectors, `ingest_paper_source`, `promote_candidate`, `ingest_knowledge_source`, `refresh_knowledge_pages` | server connector provenance, safe transport, content hashes, paper artifact manifest, review policy, exact `ScopeRef` | candidates and paper artifacts; reviewed claim/entity/page records; operational projections; source-version/compile digests | promoted/reviewed inputs compile to active pages and recall projections; refresh returns `refresh_status=ok` | rejected/quarantined/blocked artifacts never compile; contradictions mark `needs_refresh`; stale CAS returns all-zero `retry_required`; refresh retires/deprecates unsafe projections atomically | `test_active_intake_platform.py`, `test_source_registry.py`, `test_knowledge_ingest.py`, `test_knowledge_refresh.py`, `test_paper_intake.py`, `test_paper_pdf_pipeline.py` in the 151-pass suite | Closed |
| 4. Experience/outcome/correction/replay/metrics | `Runtime.record_event`, `record_outcome`, `record_outcome_trace`, adapter `record_terminal` | server-bound release identity; exact scope; sanitized payload; trusted catalog for structured contracts; attested receipts | events/outcomes/outcome traces; `record_terminal_bundle` atomically consumes receipts and writes event, outcome and trace; optional typed capability observation | raw outcome `ok`; terminal bundle complete/idempotent; explicit attribution yields aligned observation and replay/evaluation input | malformed success fails validation; changed receipt set/partial retry rolls back; unclassified outcomes remain retained; observation failure is visible as `blocked` for reconciliation, never promoted | `test_experience_outcome.py`, `test_experience_bridge.py`, contract measured-closure tests in the 151-pass suite; terminal atomicity also passed in the full baseline | Closed |
| 5. Capability definition/revision/binding/advertisement/catalog/evaluation/observation/projection | `CapabilityService`/`CapabilityRegistry` registration, binding, advertisement and evaluation methods | exact four-part owner scope + logical capability scope; normalized v3 tables; startup-only sealed catalog bootstrap | definition/revision/relation/binding/advertisement/evaluation-run/observation rows, immutable ledger and reproducible projection snapshot | lifecycle-active compatible revision/binding with fresh advertisement, trusted catalog cases and passing observations projects to its earned readiness | missing catalog is `catalog_not_configured`; incomplete discovery stays discovered; failed incubation quarantines; stale/failed evidence downgrades or blocks projection; scoped backfill/dual-write inspector exposes drift | capability storage/projector/incubation tests in the 274-pass capability/L5 suite | Closed |
| 6. Autonomous learning/candidates/safety/evaluation/promotion/reward/rollback | `run_autonomous_learning_cycle`, candidate search/portfolio, `promote_candidate`, promotion watch | governance owns goals/candidates; evidence-bound capability hypothesis; replay/safety/isolated evaluation; deployment-controlled automation policy | learning loop/goals/candidate/promotion request/lifecycle/watch/reward and ledger records | gate-passed candidate becomes `promoted` or watched shadow, observation records reward and continuity | gate failures persist `blocked`; missing policy cannot grant side effects; watch regression invokes `rollback_capability_candidate`; interrupted apply is recovered or quarantined | autonomous integration/state/full-loop and promotion-manager tests in the 274-pass suite | Closed |
| 7. Code proposal/preflight/apply/verify/recovery/commit/deploy observation | `propose_code_patch_v2`, `run_code_patch_preflight`, `CodeEvolutionTransactionManager`, promotion code-apply path | bounded repo/allowlist/digests; focused argv verification; exact capability coordinates; environment machine policy separately authorizes local apply/commit/deploy | proposal/candidate/preflight; persisted transaction and step intents/results before effects; backups and terminal receipt | verified local transaction terminalizes at its success state; commit/deployment occur only when their own actions are enabled; release observation feeds projection | subject-state mismatch blocks; verification failure restores exact content; startup recovery restores known state or quarantines ambiguity and never reapplies an old patch | code transaction/recovery/promotion tests in the 274-pass suite | Closed mechanism; production maturity remains evidence-bound |
| 8. L5 assessment/readiness/live acceptance/rehearsal/release lineage | `build_l5_assessment_v3`, `build_l5_readiness_report`, `run_l5_closure_rehearsal`, `run_release_closure` | exact scope, trusted dynamic selection, full commit/version/tree/receipt/session/active-release identity | assessment/readiness, replay manifests, live acceptance, rehearsal, deployment receipt and release-lineage records | all four axes and release-bound gates pass; closure lineage finalizes for the active immutable release | missing real outcomes or observation window returns `data_accumulating`/precise gaps; mismatch blocks; pending closure resumes; post-switch deployment failure rolls back | L5 readiness/release/rehearsal tests in the 274-pass suite | Closed mechanism; live state must be read after deployment |
| 9. Codex/Hermes/OpenClaw/eibrain/RPC/bridge lifecycle | `AgentRuntimeMemoryService.prefetch/remember/record_terminal/status`; OpenClaw `message_received`/`before_prompt_build`/`agent_end`; eibrain RPC; Codex/Hermes adapters | channel auth/attestation, server-derived scope, bounded redaction, receipt signing and runtime-owned catalog | durable capture, recall audit, tool receipts, atomic terminal bundle, outcome trace, bridge delivery/task ledger | verified terminal event/outcome/trace, delivered reply receipt and adapter status; loop task reaches `done` or explicit `failed` | invalid auth/receipt/source trust fails closed; recall continuity uses declared safe fallback; stale loop work is reconciled after grace and recorded as failed rather than disappearing | adapter/Hermes/OpenClaw/RPC/platform tests in the 482-pass, 1-skip integration suite | Closed |
| 10. Jobs/timers/doctor/emergency stop/install/health/rollback | nightly/learning/systemd units, `check_user_systemd_timers`, `run_watch`, CLI doctor, audit verifier/kill switch, `install_immutable_release.sh <full-commit>` | installed private config/secrets, systemd user owner, storage transaction lock, immutable commit/release identity | incidents/audit/watch ledgers, storage transaction marker/snapshot, deployment receipt/lineage and atomic `current` symlink switch | scheduled jobs report `ok`; installer verifies RPC identity/integrations and commits the switch | audit break triggers kill switch; failed OpenClaw watch/compact units now enter the default incident path; stale loop repair terminalizes work; installer restores storage/prior release on post-switch failure | deployment/platform/production-bootstrap tests in the 482-pass, 1-skip integration suite plus the focused failed-watchdog regression | Closed |

Focused executable totals on the same source tree were: storage/data plane 379 passed; intake/knowledge/experience 151 passed; capability/learning/code/L5 274 passed; integration/release 482 passed and 1 optional-condition skip.

The manual terminal-path review followed the owners in the matrix rather than treating broad `except` matches as defects. The 31 `except: pass` files are confined to best-effort cleanup, bounded compatibility/diagnostic fallbacks, or optional projection availability. The authoritative transaction, trust, promotion, release and terminal-bundle paths either roll back or return an explicit restrictive state. One producer/consumer mismatch was confirmed in operational monitoring and is recorded as OPS-1 below.

## Confirmed product defects

| Counterexample | Authoritative owner | Repair/evidence | State |
| --- | --- | --- | --- |
| Structured OpenClaw outcome contracts were rejected for missing catalog before their probe/rehearsal invariant could be evaluated. | `eimemory.api.runtime.Runtime.record_outcome_trace` | Runtime selects its sealed catalog for contracted traces; exact and affected-family tests pass in `44133fb0b`. | repaired in baseline |
| Production installed and enabled `openclaw-loop-watch.timer`, and its service was visibly failed with lease-expired work, but `check_user_systemd_timers` could not observe that timer/service because neither appeared in the default inventory. | `eimemory.ops.timer_monitor` plus its installed systemd service contract | Defaults now cover current audit/monitor/OpenClaw watch/compact timers and services plus release closure; the regression proves a failed watchdog creates the exact issue and persisted incident while legacy learning controls remain opt-in. | OPS-1 repaired |
| The monitor classified a healthy daily timer as stale 90 minutes after its last run even when `NextElapseUSecRealtime` was correctly scheduled in the future; adding the 48-hour one-shot L5 timer would have amplified the same false alert. Conversely, a missing service with `LoadState=not-found` produced no issue. Its ISO-only parser also skipped the weekday-prefixed timestamp returned by real `systemctl show`. | `eimemory.ops.timer_monitor._timer_issues` | Staleness is now measured from an overdue next elapse, not the age of the previous success; systemctl runs under C/UTC and the parser accepts its real weekday-prefixed UTC form; any non-loaded/non-masked unit is unavailable. Regressions cover future/overdue raw systemd schedules, an elapsed successful one-shot, and a missing watchdog service. | OPS-2 repaired |
| The immutable installer shipped audit-verifier and timer-monitor units but did not install or enable them, so a clean one-command deployment could omit its own aggregate controls. The scheduled monitor also did not opt into the installed learning timers. | `deploy/install_immutable_release.sh` and `deploy/systemd/eimemory-timer-monitor.service` | The release transaction now installs/enables both control timers; the managed monitor explicitly includes learning units and its defaults cover L5 review and the release-closure path. Installer ownership tests pass. | DEPLOY-1 repaired |
| The obsolete Python Feishu receipt helper rejected malformed platform IDs, but the actual maintained OpenClaw `messageToolDeliveryReceipt` path accepted any nonempty string and could mark a reply `platform_accepted`; it also descended into nested success data even when the outer tool result explicitly said `ok:false`. | `integrations/openclaw/eimemory-bridge/index.js` | One strict `om_...` predicate guards inbound correlation, persisted attempts, gateway acceptance and message-tool receipts, and an explicitly failed wrapper stops recursion. Regressions prove malformed or contradictory failed receipts remain pending with no delivery ID; the unreachable helper/tests were deleted. | ADAPTER-1 repaired |
| Full-evaluation workers, converted-data smoke tests, the OpenClaw terminal fixture, and code-provider transport tests created ad-hoc temporary roots; default CLI/hook tests could also write synthetic records to the operator's real memory root and OpenClaw task ledger. Several error/skip paths could leave data behind. | Evaluation/smoke scripts and test isolation owners | Managed `TemporaryDirectory` lifetimes now cover runtime close, success, raised evaluation errors, bridge state, socket permission failures, and pytest teardown. The autouse test fixture binds default `EIMEMORY_ROOT` and `OPENCLAW_LOOP_HOME` beneath each managed pytest root while tests that exercise explicit configuration still override/clear them. Worker/smoke deletion regressions, 57 CLI/event tests, independent exception injection, and before/after `/tmp` inventories all pass with no new root. | TESTDATA-1 repaired |

No other product assertion failed in the complete clean baseline. Structural risk matches and live operational observations remain findings only after a concrete producer/consumer or terminal-state invariant is disproved.

## Environment or prerequisite failures

| Observation | Classification | Resolution |
| --- | --- | --- |
| Monolithic pytest retained more temporary state than the 3.7 GiB `/tmp` tmpfs quota. | host quota | File-sharded run with immediate synthetic-data deletion completed all 250 files. |
| Sandboxed RPC tests received `PermissionError: [Errno 1] Operation not permitted` when binding loopback sockets. | sandbox capability | Clean run used the host test environment while retaining private, bounded test roots. |
| A long worktree basetemp exceeded the Unix-domain socket path limit in socket tests. | host path limit | Short `/tmp/eim-pytest-file-*` roots were used. |
| A basetemp under the group-writable workspace was rejected by production-dataset ownership checks. | intentional security gate | Private `/tmp` roots with `umask 077` satisfied the production loader contract. |
| The shared virtual environment initially exposed the previously installed wheel's entry-point metadata. | local prerequisite drift | Built and installed the exact local wheel without dependencies; catalog/owner tests passed. |
| In the current restricted command sandbox, a minimal Node parent/child reproduction loses all nested-child stdout/stderr even when the child exits 0; five terminal-bridge cases therefore receive `{}`. | sandbox process-I/O capability | Classified by a product-independent minimal reproduction. The same terminal contract passed 7/7 in the clean host baseline; no product assertion or transport code was weakened. |
| The current command sandbox rejects AF_INET and AF_UNIX socket creation/use with `PermissionError`, while its `/tmp` mount is owned by mapped uid 65534 rather than root. | sandbox socket/user-namespace capability | The full candidate run retained the original fail-closed socket and trusted-dataset contracts. Existing user-namespace test support models a conventional root-owned ancestor only inside the one affected test; the production loader was not relaxed. |
| Pre-repair sandbox runs had left 26 synthetic `eimemory-provider-*` directories under `/tmp` (25 transport roots and one stale-socket reproduction). | pre-repair test residue | TESTDATA-1 prevents any new residue, including on the same permission failure. The final pre-release inventory found zero matching roots; none is referenced by production configuration or counted as evidence. |

These conditions were not changed into weaker product behavior. The audit changed the verification environment so the existing security and durability contracts were exercised as designed.

## Dead-code, test, and deployment candidates

Two deployment helpers passed the full reference-class check:

| Candidate | Import/dynamic/entry/systemd/subprocess search | History/obligation | Test invariant | Disposition |
| --- | --- | --- | --- | --- |
| `deploy/extract_feishu_message_id.py` | No production caller, installer reference, package entry point, systemd command, shell subprocess, documented external command, or string import. Only `tests/test_deployment_tools.py` imported it. | Added for an older deployment receipt shape; the official delivery lifecycle now lives in `integrations/openclaw/eimemory-bridge/index.js`, which consumes `primaryPlatformMessageId` directly. | Its fail-closed ID invariant now passes at the maintained JavaScript consumer. | Deleted with its test-only unit cases. |
| `deploy/verify_l5_v3_migration.py` | No caller or tests; only its own `__main__` and the historical implementation plan mentioned it. No entry point, unit, installer/subprocess, integration or compatibility reference existed. | One-off v3 rollout verifier added in `56e5483d8`; supported CLI owners now expose `l5-v3`, `l5-v3-reconcile`, `capability-v3-backfill-status`, and `capability-v3-dual-write`, while the immutable installer owns release health/closure. | No unique test or public contract. Schema/FK/backfill/dual-write/assessment invariants remain owned by capability storage, L5 and deployment suites. | Deleted. |

All other deployment sources have a maintained caller and a named failure consequence:

| Deployment class | Maintained sources | Caller and failure consequence |
| --- | --- | --- |
| Installer transaction and runtime discovery | `install_immutable_release.sh`, `clean_release_bytecode.py`, `find_prior_immutable_release.py`, `install_managed_systemd_dropin.py`, `run_with_governance_env.py`, `check_user_systemd_owner.sh`, `discover_python_runtime_units.sh` | Exact-commit installer/staged release; failure aborts before or rolls back the switch. |
| Runtime integrations | `ensure_openclaw_bridge_config.py`, `install_hermes_integration.py`, `verify_hermes_integration.py`, `verify_openclaw_plugin_runtime.py` | Immutable installer and post-switch integration checks; failure blocks/rolls back release. |
| Secret/config provisioners | `ensure_attestation_profile.py`, `ensure_evidence_receipt_key.py`, `ensure_rpc_auth.py`, `rotate_console_token.py`, the two `.example` files and managed `.conf`/wrapper files | Installer/operator rotation and private environment staging; missing/weak authority blocks the affected service or release gate. |
| Storage migration/rollback | `migrate_storage_release.py`, `storage_release_transaction.py`, storage guard and Python runtime drop-ins | Installer storage lock/prepare/guard/restore transaction; failure restores the snapshot/prior writer state. |
| Release evidence and health | `bootstrap_production_recall.py`, `capture_prior_health_snapshot.py`, `record_deployment_receipt.py`, `record_release_lineage.py`, `summarize_release_closure.py`, `verify_release_health.py` | Pre/post-switch acceptance and release-closure path; failure blocks finalization or invokes rollback. |
| Managed units | RPC, console, nightly, learning, audit, timer monitor, code-implementation refresh, L5 effect review, release-closure path, OpenClaw watch/compact service/timer/path sources under `deploy/systemd/` | Installed/enabled by the immutable installer; failure is exposed through service state, health/doctor, incident/audit records, or release verification. OPS-1 covers the one missing aggregate-monitor consumer. |

## L5 and code-evolution production evidence

The clean suite proves L5/readiness/code-evolution mechanisms, not current production maturity. Live evidence is reviewed separately against the deployed immutable commit, real terminal tasks/outcomes, observation windows, rollback state, and the code-implementation owner. Until those records meet the release-bound gates, the only valid conclusion is `data_accumulating` or the explicit failing gate returned by production.

An additional isolated 11-scenario rehearsal passed storage, governed recall, intake/review/promotion, outcome-trace persistence, core capability acceptance/replay, low-risk policy evolution, observation reconciliation/rollback ownership, release-bound `data_accumulating`, failed-watchdog alerting, malformed Feishu receipt rejection, and OpenClaw closed-loop task mechanics. The rehearsal used one `TemporaryDirectory`; a post-run check found no `eimemory-business-simulation-*` root. These fixtures are mechanism evidence only and were not copied into any production store, score, readiness report, or evolution ledger.

## Ordered repair batches

1. OPS-1/OPS-2 — current-unit visibility and schedule-correct staleness repaired; focused regressions passed.
2. DEPLOY-1 — one-command ownership of the audit verifier and self-monitor repaired; installer contract passed.
3. ADAPTER-1 — repaired and focused regression passed.
4. TESTDATA-1 — future evaluation, smoke, bridge and provider fixture roots are managed and deleted on success/failure; the final pre-release inventory found no remaining provider fixture root.
5. Both proven-dead deployment helpers and only their obsolete helper tests — deleted; maintained affected-family coverage passed.
6. Remaining release work: run final verification, commit/push/deploy and perform production-bound observation. The isolated business rehearsal is already complete and its data has been deleted.

## Final exact-commit verification

Current candidate evidence before commit:

| Check | Result |
| --- | --- |
| Version surfaces | `1.11.4` in package metadata, Python module, Codex manifest and both Hermes manifests |
| Collection | 250 files; 3,211 tests |
| Changed/affected and repository-hygiene families | 451 passed; 2 optional/sandbox-condition skips; 3 Unix-socket cases explicitly deselected after the sandbox capability was independently reproduced |
| Fresh unrestricted-host final reruns | 438 passed; 1 optional-condition skip across the changed test files plus version/package/repository hygiene, release closure, deployment receipt, governance environment, production recall bootstrap and release lineage families. The same 43 socket/Node cases that failed only in the restricted sandbox passed on the host. |
| Sharded full candidate in current sandbox | All 250 files / 3,211 cases executed; effective exact-candidate result after the one trusted-ancestor test rerun: 3,107 passed, 6 optional/sandbox-condition skips, 95 failed and 3 setup errors across 11 remaining files. All 98 non-passes are the independently reproduced socket or nested-Node process-I/O restrictions; no product assertion counterexample remains. |
| Cleanup/isolation independent review | Two focused reviews found no Critical/Important issue. Cleanup injection: 34 passed, 1 sandbox-condition skip. Final root/trust-fixture review: 86 passed, preserving symlink, mode, descriptor-bound ownership and negative trust-gate coverage. |
| Isolated business rehearsal | 11 passed; dedicated temporary root absent after completion |
| Maintained source audit | 391 files; 166,016 lines; 5,737 callables; 0 invalid/unclassified |
| Native/package checks | installer `bash -n`, OpenClaw `node --check`, Python `compileall`, `git diff --check` passed |
| Wheel | `eimemory-1.11.4-py3-none-any.whl`; 316 members; SHA-256 `ca878dc560a3d261c9e650a655ce8671539d8d278a77c34ab514d347bc05f1b7`; version and entry-point metadata verified |

Release acceptance combines the clean unrestricted 3,207-pass baseline, the exhaustive 3,211-case exact-candidate sandbox inventory, and the fresh 438-pass unrestricted-host reruns of every changed and release-critical family. The host reruns include the socket and nested-Node paths that the sandbox cannot execute, so no environmental failure is represented as a product failure. A second monolithic host run was intentionally not substituted for the deterministic sharded inventory because this host's bounded `/tmp` quota had already disproved that execution shape. Immutable commit identity, push, deployment receipts and production observation are emitted after this source tree is sealed into the release commit.
