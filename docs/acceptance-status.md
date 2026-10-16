# Receipt-based acceptance status

## October 16 channel-scope and provenance repair: 1.14.114

This candidate carries the full channel tuple only from an independently resolved,
unique private operator binding and only when the dataset's complete scope equals
that tuple. The strict production scope and label gates remain unchanged.
Memory-evaluation provenance is persisted before append and projected through the
existing bounded diagnostic allowlist, including replay-generation confirmation.

The prior 1.14.112 nightly and its formal accepted 0/3, knowledge-source review
waits and identity-projection refusals remain historical evidence. This source
change does not establish a new deployment, nightly or quality acceptance. The
new release requires its own terminal deployment receipt and same-commit report;
controlled benchmark success and process exit remain separate from formal quality.

## October 16 operator-binding repair: 1.14.113

The operator confirms 1.14.112 nightly `ref_a9b903793611` ran
15:14:52–15:19:34 (Asia/Shanghai), exited 1 and binds
`04eb9ca4e37625911bc62cb41d9f6bc8f06eb2a0`. Production recall failed with
`dataset_scope_override_mismatch` and zero samples; the quality gate stayed
unaccepted at 0/3. Controlled memory did not run. Its new diagnostics report
`channel_source=operator_binding`, `logical_scope_reason=binding_user_missing`
and `matching_channel_count=0`, ending in
`memory_benchmark_channel_binding_missing`.

A local reproduction identifies inconsistent consumers of the existing
operator rename policy. Deployment receipt validation accepts a private pin
whose configured legacy agent name applies to the canonical caller. The
logical-user resolver and controlled channel matcher instead compared raw
agent names, so both ignored the same pin. `binding_user_missing` therefore
does not prove that the pin's user field is empty: a nonempty user can be
filtered out before selection.

1.14.113 reuses the existing pin-applicability rule in those two consumers and
uses the same complete scope-shape requirement for logical-user selection.
It retains the caller's canonical identity and the exact dataset gate;
unconfigured names, different tenants or workspaces, invalid/private-file
failures, explicit unrelated users and ambiguous user/channel choices remain
rejected. It neither guesses a binding from stored records nor changes pins,
labels, source approvals or ranking.

These are source fixes and isolated reproductions, not a newly completed
production nightly. The deployed patch still needs a report bound to its own
commit and receipt before any claim that production acceptance has recovered.

## October 16 deployment prerequisites: 1.14.112

This patch fixes two locally reproduced prerequisites: RPC start/restart
failures no longer skip messaging recovery, and deployment-worker subprocess
tests explicitly isolate HOME and settings. Recovery failures still reject
application acceptance and prevent background writers/timers from starting.
The worker follows the existing explicit config-path precedence.

These source and isolated regression results do not establish a production
switch or a new nightly. An operational, platform-approved independent
controller remains a deployment prerequisite. Historical transient units and
successful receipts do not grant permission to repackage a denied lifecycle
operation. The 1.14.110 production evidence and its quality blockers below
remain historical; this patch does not change memories, labels or gate scope.

## October 16 source repair: 1.14.111

The operator reports 1.14.110 deployed with an `ok=true` receipt and a running
RPC. A subsequent readback identifies nightly `ref_3567f900436c`, bound to
`f634d7e890e04bcde5c16c26f7d66c55c92e89fa` (1.14.110); this supersedes the
earlier statement that only a 1.14.109 report was available. It still reports
evaluation and knowledge-link blockers, not business acceptance. This source
repair does not run a production deployment, nightly, data repair or label
approval.

The operator confirms this run finished at 14:03:48 after starting at 13:58:20
(Asia/Shanghai), with exit code 1 and failed steps `production_recall` and
`recall_quality_gate`. Production recall reports
`dataset_scope_override_mismatch` with zero samples; controlled memory reports
`memory_eval_dataset_empty` and did not run. Production collection reports zero
new and zero existing cases, with formal acceptance still 0/3. Repeating the
unchanged release does not repair these scope and input boundaries.

Two defects were reproduced locally:

- Production dataset construction resolves a host account to its logical user
  through the existing private operator binding, but nightly evaluation passed
  the original host account back as an override. Loading and evaluation now
  use the same bound caller scope. Dataset metadata cannot grant a different
  tenant, agent, workspace or user. A rejected report exposes only the differing
  scope field names, including in per-agent verdicts; channel/base-workspace
  differences still require their actual authorized context rather than an
  automatic scope expansion.
- Supervisor persistence discarded release fields from its content even though
  the existing learning-record path supplied them from verified runtime identity.
  Persistence and readback now retain the commit, version, receipt and session.
  Completed or caught-exception nightly summaries carry a `run_id`, `started_at`
  and record ID. Diagnosis rejects mismatched commits or deployment bindings;
  missing identity stays unverified. A killed or still-running process may
  leave the preceding completed report visible: compare its identity and times
  with the service invocation before calling it the new run.

The 249 `source_projection_or_digest_mismatch` outcomes are from the CLI's
identity repair after nightly evaluation and summary persistence. They are not
by themselves evidence that these rows caused the earlier evaluation failure.
Repair previews now expose `mismatch_field_counts` and bounded per-row
`mismatch_fields`, using fixed names without payload values. In particular,
`payload_roundtrip_digest` distinguishes normalization of a legacy envelope from
`payload_stored_digest`, a checksum failure on its stored payload. Both remain
rejected under the original repair contract. Classify the actual rows before
choosing any sanctioned repair; the historical created-at fix is not proof of
the cause of these 249 rows.

The same 1.14.110 report's `nightly_diagnostics.memory_benchmark` identifies
`dataset_source=replay_dataset`, `dataset_generation.ok=true` and
`memory_benchmark_channel_binding_missing`. The controlled fallback stopped
before source-record selection because its readable private binding file had
no matching channel for the resolved caller scope. The summary omitted
`configured` and scope-resolution details; its other generation counters are
null, not zero. The existing fallback passes an isolated host-to-logical-user
reproduction. Configured empty inputs and failed replay generation deliberately
retain their own blockers.
The repaired summary retains `configured`, `success_confirmed` and allowlisted
resolution fields (`logical_scope_reason`, `logical_user_rewritten`,
`channel_source`, `matching_channel_count`) without exposing identity values or
binding paths. Missing fields remain unknown.

The same run's knowledge-review reasons are
`knowledge_inventory_enumeration_pending: 1`,
`knowledge_inventory_review_incomplete: 1` and
`knowledge_source_review_missing: 400`. These are missing source-review and
inventory-completion evidence, not a ranking diagnosis or permission to create
approvals. Exact gap revisions, source/candidate snapshot checks and inventory
progress must still distinguish missing authority from an association defect.
The 400 missing-source-review entries are cumulative manifest counts, not a
claim of 400 review failures in this run. An isolated inventory reproduction
can produce this exact reason combination while its cursor advances and
eventually completes with no eligible sources; these counts alone do not prove
a continuation defect.

Local regression checks establish the source behavior above. Production
acceptance still requires a new nightly bound to its deployed commit, receipt,
session and execution round, with unchanged quality and evidence requirements.

## Earlier production readbacks

Documentation updated on 2026-10-15 from production receipts read by the
operator's executor on honrui (times Asia/Shanghai).

**Production runs 1.14.86 (see the latest section below for its receipt).
Deployment acceptance passes on every release from 1.14.80 through 1.14.86.
Formal recall quality and L5 readiness remain unaccepted: both wait on
evidence that only accumulates over time (trusted recall labels, non-recall
L5 evidence). No threshold was changed.**

## October 15-16: 1.14.87 to 1.14.101

| Release | Deploy (CST) | Receipt | live-acceptance | capability-acceptance | Nightly |
|---|---|---|---|---|---|
| 1.14.87 `322cdf36` | 14:35, ok, backup first | `rec_277caa7ec9cf` | 10/10 (reused, same receipt) | default mode blocked (`memory.recall` definition still `discovered`); `--legacy-compatibility` 27/27 | success; per-agent gate blocked, only one agent had a dataset |
| 1.14.88 `16f863c8` | 16:42, ok (first attempt hit transient `research_review_hermes_runtime_unavailable` pre-switch, retry clean) | `rec_d6a766651c26` | 10/10, reused from `rec_d6a766651c26` | `--legacy-compatibility` 27/27 | success; every agent now has a dataset, gate `recall_quality_evidence_incomplete` |
| 1.14.89 `28764a85` | 18:01, ok (deploy retried: back-to-back backup made the import-only research-review check exceed its 10 s budget) | `rec_52db24b1818f` | not recorded (run interrupted) | not run | not run (superseded same evening) |
| 1.14.90 `96c5abae` | switched 18:19, **rolled back 20:05** (blocking restart of the nightly oneshot; nightly exit 1 on capability steps -> installer restored the 18:16 storage snapshot) | `rec_418e7cd17469` (live-acceptance while live) | 10/10 fresh, scope `hongrui` | default mode rejected (`memory.recall` `discovered`) | migrated 43,719 records, discarded by the rollback |
| 1.14.91 `2df532e4` | not deployed (superseded by 1.14.92) | - | - | - | - |
| 1.14.92 `875bfc99` | 21:36, ok, backup `20261015T125014Z` first; stale 1.14.90 transaction cleared via `--recover-only` (resume_validation, no snapshot replay) | `rec_1e4529f42c0a` | see 1.14.93 | see 1.14.93 | 21:43: migration ~2,170 rec/min (was ~34): 35,051 applied; failed on capability steps (fixed in 1.14.93) |
| 1.14.93 `1b47bdb4` | 23:32, ok, backup `20261015T151508Z` | `rec_9e62d56ffdb4` | - | - | capability steps pass; `hongrui` MRR 0.0 = measurement bug (legacy-scope targets under `exact_scope_only`), fixed in 1.14.94 |
| 1.14.94 `56c61d57` | 00:01, ok, backup `20261015T154448Z` | `rec_cbc13dd4e865` | 10/10 (reused same receipt) | `--legacy-compatibility` 27/27; default failed (`evaluation_storage_error:IntegrityError` while migration ran); incubation `waiting` | ingest-stamped legacy rows now move: 9,146 applied |
| 1.14.95 `d07c97e6` | ~00:25, ok, backup `20261015T162051Z` | - | - | - | legacy rows of any source move; default-scope smoke 0.1 = ambiguous digest known item (fixed 1.14.96) |
| 1.14.96 `19859634` | released, not deployed separately | - | - | - | - |
| 1.14.97 `abf5e3fb` | 00:52, backup `20261015T164456Z` | `rec_7326141fcee1` | `acceptance_deployment_receipt_invalid` (receipt response reported its own id as release session; fixed 1.14.98) | `--legacy-compatibility` 27/27; default `evaluation_storage_error:IntegrityError` (composite FK: catalog run written to `hongrui`, spec/binding in legacy partition; fixed 1.14.98); incubation `waiting` | 01:05: all 4 agents false_recall 0.0 (hongrui 10 samples, MRR 1.0); gate failed once on p95 latency 3,024 ms > 3,000 while acceptance ran concurrently; 01:24 run: no failed steps, gate `recall_quality_evidence_incomplete` only; legacy channel subscopes move; per-user sanctioned repair: ou_07c 3,632+63, default 17, others 7 |
| 1.14.98 `2ef19bb0` (tag `0176d1ca`, +changelog heading) | ~01:45, ok, backup `20261015T173313Z` | published | 10/10 (reused) | `--legacy-compatibility` 27/27; default: spec/run now persist, case fails on a real verdict: `code.implementation` executor `execution_ok=false` (`catalog_pass_failed:CodeImplementationError`); incubation `waiting` (`memory.recall`: `active_provider_binding_missing`) | - |
| 1.14.99 `6063d260` | ~02:40, ok, published | - | - | default capability-acceptance passed 5/5 consecutive diagnostic runs (`code.implementation` failure intermittent; client timeout clamp 120s raised to budget+10s; failure reason code now surfaced) | 02:09 (on 1.14.98): `memory_eval_ci` failed on 2 unevaluable cases mined from a failed acceptance probe (fixed 1.14.100) |
| 1.14.100 `8db9a891` | ~03:00, ok, published | - | - | - | 03:03: no failed steps; gate `recall_quality_evidence_incomplete` (accepted cases need 1.14.101 to validate) |
| 1.14.101 `1739168f` (deployed head `97571a4e`, +docs) | 03:37, ok, published (first attempt refused: target not origin/master) | - | default capability-acceptance 1/1 on 1.14.100 | incubation `waiting` (`memory.recall` `active_provider_binding_missing`: no memory.recall provider adapter exists, needs a feature, not a fix) | 03:44: no failed steps; per agent (known-item smoke) hongrui 10/0.0/1.0, hongtai 6/0.0/1.0, hongxin 6/0.0/1.0, xiaomage 10/0.0/0.9; all `recall_quality_evidence_incomplete` |

Migration end state (sanctioned path only): `hongtu` 2,958 rows remain at 01:24 (2,772 earlier plus 185 `semantic_relevance_monitor` evaluation packets bound to the 272 legacy `hongtu` proactive decisions, which stay in the decision's exact scope, and 1 capability audit in the legacy capability partition), all intentionally unmovable: 2,138 `identity_scope_preserved` pins, 130 deployment-receipt digest pins, 254 `target_conflict` (a copy already exists under both ids), 249 `source_projection_or_digest_mismatch`, 1 other. Reads for `hongrui` fan out to them through the legacy alias. 1.14.90's rollback discarded live writes between 18:16 and 20:05; JSONL ledger segments 15-18 still hold them (recovery only through `rebuild-sqlite`, needs an explicit decision).

1.14.88 nightly per agent (thresholds unchanged, no pooling): hongtai 6 samples, false_recall_rate 0.0, MRR 1.0; legacy `hongtu` (the default gateway, renamed to the canonical id in 1.14.89) 10, 0.0, 0.6; hongxin 6, 0.0, 1.0; xiaomage 10, 0.0, 0.9. All blocked only on label trust (`recall_quality_evidence_incomplete`), none on a metric.

Label trust (2026-10-16, user-authorized): 3 real hongrui production cases accepted by operator label (`prqa_13d88225…`, `prqa_1544b506…`, `prqa_8e647a32…`; each labels `mem_778b2152…`, the stored deploy-acceptance requirement, for deploy/nightly/receipt-acceptance requests; verified against the vaulted query text, which is not reproduced here). hongtai, hongxin and xiaomage have no vaulted query inputs and mostly empty recalls, so no case could be verified and none was accepted. Alias evidence binding (user-authorized policy, 1.14.100-1.14.101) uses configured `legacy_agent_ids`. Partial backup `.tmp-20261015T101229Z` deleted on user decision; 18:16-20:05 writes intentionally not recovered (321 records absent, mostly derived learning artifacts).

Formal production eval for hongrui (3 accepted cases, dataset built to `state/operator-labels-20261016/`, not installed as the global dataset because the conventional path applies to every agent): recall@5 0.0, MRR 0.0 on redacted query features, gate also blocked by `prior_release_baseline_high_water_missing`. The nightly does not use accepted cases unless a dataset file is configured. `outcomes stats` 03:30: 564 natural decisions, signal coverage 0.09, verified outcomes 0. The scheduled `eimemory-backup.timer` (`--keep 5`) pruned backups older than `20261015T164456Z` at 02:43; its run killed by the deploy restart left `.tmp-20261015T184355Z` (2.0 GB, not deleted).

Earlier finding: known-item smoke is never trusted label evidence. Trusted labels come only from accepted production real-query cases (>=3 cases/labels per gate scope). In each gate scope, auto-review dry-run accepts 0 of the pending cases: hongrui 371/371 `pending_capture_boundary_invalid` (payload scope and decision still say `hongtu`; `same_scope` rejects aliasing as evidence authority by design), and for the other agents mostly `no_candidate_refs` (empty recalls) plus `independent_signal_agreement_missing`. The nightly auto-review only runs in the settings scope, where it reviews 0 cases. `outcomes stats`: 564 natural decisions, signal coverage 0.09, verified outcomes 0.

Operations: 58 byte-identical synthetic correction_replay rules retired reversibly (batch `srr-f2ef2d4633b4`, manifest under `state/synthetic-rule-retirement/`, one canonical kept). `memory.recall` stays `discovered`: the sanctioned incubation path reports `active_provider_binding_missing`, so it is not activated by hand. l5-readiness gaps (terminal receipt, transaction evidence, system-originated incident, lineage) need a real autonomous code-evolution transaction; no code defect found.

## October 15: 1.14.82 to 1.14.86

| Release | Commit | Deploy receipt | Live / capability acceptance | Notes |
| --- | --- | --- | --- | --- |
| 1.14.82 | `f1ff0fe7` | went live; receipt missing (`health_identity_missing` while RPC warmed up) | not run | fixed by 1.14.83 |
| 1.14.83 | `b3eff80e` | `ok=true`, all step exit codes 0 | 10/10 | per-case `recorded_at` populated |
| 1.14.84 | `d86cbe88` | `ok=true` | 10/10, 1/1 | known-item false recall fixed (identical daily digests) |
| 1.14.85 | `89a234d5` | `ok=true` | 10/10, 1/1 | Codex in-stream provider errors classified/retried |
| 1.14.86 | `8a9123ea` | `ok=true`, backup `20261015T045853Z` | 10/10, 1/1 | nightly reports `production_recall_smoke`; 13:13 nightly `execution_ok=true`, `false_recall_rate` 0.0 (10 samples) |

- The 03:30 nightly of 2026-10-15 executed but its release check measured
  `false_recall_rate` 0.1 (threshold 0.05). The single false recall was a
  correct verdict caused by byte-identical daily research digests; 1.14.84
  fixed the digest content and recall collapse. The next release check
  measured 0.0 (0/10).
- The 10:28 nightly failed `research_closure_review` on one provider error
  that was classified `unknown`; 1.14.85 classifies Codex in-stream error
  frames. The 11:41 nightly passed (`execution_ok=true`) and reviewed that
  record; its own known-item smoke eval measured `false_recall_rate` 0.0.
- `learn l5-readiness` stays `incomplete`; the recall-quality gate stays
  blocked with `recall_quality_evidence_incomplete`, not by a metric.

The sections below are earlier, historical readbacks.

## October 14 deployment boundary and source repair

The remote 1.14.78 commit is
`e02083e99553f16db0e6a1b21eb4189aff301038`. Its Project validation and Linux
deployment contracts both passed. CI does not grant permission for the installer
to stop/start the current Hermes gateway. The operator reports that action was
blocked before deployment, leaving production on 1.14.76; no new health request,
deployment receipt or nightly run is claimed here.

Mainline 1.14.79 fixes a separate, locally reproduced installer defect: a denied
stop could leave the old gateway active, and the subsequent start/PID check
could certify that old process as freshly restarted. The restart step now
requires independently verified terminal state before reset/start and reports
reload, unknown-state and start failures explicitly. Graceful-close exit status 1
compatibility remains for confirmed inactive/failed units. This source repair
does not create a lifecycle exemption or certify production recovery.

Deployment remains blocked until the execution platform provides an approved
entry for the existing installer, including failure restoration. See the
[lifecycle boundary](../deploy/systemd/README.md#deployment-lifecycle-boundary).

## Earlier reported deployment and nightly: 1.14.72

Full commit: `612dc458322eb60f1631b4605b0a30eb8a517e99`. The operator supplied
October 13, 2026 times in Asia/Shanghai. Deployment ran 02:11:29–02:15:04;
receipt `rec_fba7350aa0b4` has `ok=true`, with installation, binding refresh
and health-check exit codes all 0. The current release matches the target,
and the RPC service is reported active.

Nightly `ref_d6eb0f6e9c6f` ran 02:15:05–02:23:40 and exited 0 with
`execution_ok=true` and no failed steps. It produced 6,545, promoted 10 and
rolled back 56; rollback is a task count, not a release rollback.

The controlled baseline ran eight samples: seven passed and one failed.
Case `controlled-memory-f5894372aefafa1900dc2d02` expects target
`mem_c2c5b75407f1f262177f4586400db98f`. Its native target trace completed,
with complete, untruncated observations. The SQLite source was available
without fallback. `provider_returned` observed 14 candidates with the target
`absent_in_observed_window`; the target was also absent at `provider_bounded`,
`hydrated`, `filtered`, `fusion_input` and `selector_input`. Selector output and
final output were both empty.

These measurements exclude the expected target entering the selector and being
rejected for grounding in this run. The gap is before the source-return boundary;
the trace alone does not prove a missing index entry. Query generation, source
matching and source-internal filtering still need distinct evidence. Grounding,
query, case ID, expected target and quality thresholds remain unchanged. The
trace does not reconstruct 1.14.71 membership or historical query material,
certify recovery of this miss or establish natural-query/no-answer acceptance.

Collection created five cases, found 164 existing cases and reported no skips;
all five new cases have empty candidates. The review summary records 365 reviews,
zero pending/open reviews and **0/3 formally accepted cases**;
`recall_quality_accepted=false`. Empty cases do not generate trusted positive
labels or independently justified no-answer evidence.

Each of nine evolution results has zero candidate hypotheses. An applicable
knowledge link for the current gap revision is still missing. No completed
maintenance/evolution transaction or participant-level acceptance is supplied.
The two earlier participant L1 queries, production role correctness and automatic
factual correction remain unverified. This readback used existing receipts only:
no rerun, probe or production modification was performed.

## Mainline 1.14.74: business-closure repairs

The October 13 audit identified seven priority-one and nine priority-two source
issues. The new release repairs identity input, Hermes event/capture/ACK recovery,
formal summary authority, negative capability evidence, backup payload retention,
and the remaining evaluation/retrieval/feedback/idempotency defects. The original
audit remains a fixed 1.14.73 baseline; the
[repair record](audit/business-closure-remediation-2026-10-13.md) records changed
contracts and local validation separately.

This is a source-level repair, not a new production readback. The latest supplied
receipts remain the 1.14.72 ones above. No new deployment, nightly, recovered miss,
trusted case count or team acceptance is inferred. Existing quality thresholds,
exact scope/source boundaries and the three-case floor remain in force.

## Earlier source 1.14.73: controlled eligibility and SQLite source diagnosis

The source repair reuses existing recall source-policy rules when selecting
positive known-item targets. Capture-rejected records, shared raw episodes
and default evidence-only material must not create an expectation of ordinary
recall. Normal eligible L1 episodic memories remain supported. Generation retains
explicit skip reasons, and existing controlled datasets are revalidated; an
ineligible source is not an evaluable pass. An empty eligible dataset cannot
pass. Selection does not depend on previous retrieval failure or grounding.

A separate bounded native SQLite trace observes actual source matching, scoring,
rank/cap windows and target-specific branches under the active source/store/request
and borrowed reader binding, with thread/task/nested-invocation isolation.
It adds no target lookup, SQL/read/model call or injected candidate and preserves
query, ordering, limits, selector, grounding and quality thresholds. A target
absent from an observed SQL `WHERE` + `LIMIT` window is not thereby absent from
the index or known to be rejected by one particular predicate. Missing evidence
remains unknown; the trace adds no query, database path, key, text or identity
payload to public diagnostics.

The latest production evidence remains 1.14.72 above. These repairs do not change
its failed case, backfill a source trace into that receipt, prove the older
miss's cause or recovery, certify the two participant L1 queries, or close formal
quality/evolution acceptance. No 1.14.73 production acceptance is supplied.
See [native source trace fields](evaluation.md#sqlite-source-target-trace-11473).

## Earlier reported deployment and nightly: 1.14.71

Full commit: `e598397b4c0ef2afebe4024c8ad6f842e28c4f07`. The operator supplied
October 13, 2026 times in Asia/Shanghai. Deployment ran 01:02:40–01:06:16;
receipt `rec_e56a7b8df209` has `ok=true`, with installation, binding refresh
and health-check exit codes all 0. The current release matches the target,
and the RPC service is reported active.

Nightly `ref_b23ca097a83f` ran 01:06:17–01:15:06 and exited 0 with successful
execution and no failed steps. It produced 6,573, promoted 10 and rolled back
56; rollback is a task count, not a release rollback. The earlier research-review
execution failure did not recur in this run. This does not identify that earlier
provider failure's cause or certify every provider request.

The controlled baseline ran nine samples: eight passed and one failed. The
failed known-item case is `controlled-memory-f5894372aefafa1900dc2d02`.
Its source was available, fallback was false, retrieval returned 14 candidates
to the selector, and the selector admitted zero with 14 `grounding` drops.
The final case returned zero records and reports `empty_result_cause=selector_rejected_all`.
This establishes rejection of the current selector inputs. The report does not
establish whether the expected target was among those 14 candidates; it cannot
prove the target's loss stage, justify changing grounding or identify an algorithm
defect. These controlled measurements do not create formal labels, natural-query
acceptance or correct-no-answer evidence. They do not backfill the 1.14.69 miss's
missing stage fields.

Production collection created four cases, found 160 existing cases and recorded
no collection skips. All four new cases have empty candidates. The review summary
records 360 reviews, zero pending/open reviews and **0/3 formally accepted cases**;
`recall_quality_accepted=false`. A completed empty-case review is not a trusted
positive label or independently justified no-answer conclusion. Review totals
cannot establish the number of new usable production cases.

Each of nine evolution results has zero candidate hypotheses. An applicable
knowledge link for the current gap revision is still missing. No current inventory
completion, successful maintenance transaction or participant-level recall
acceptance is supplied. The earlier `knowledge_inventory_truncated` belongs to
the 1.14.69 readback below and is not inferred as this run's blocker.

The 1.14.71 deployment and execution results do not verify recovery of the two
earlier participant L1 queries, actual production role-extraction behavior or
automatic factual correction. Formal quality and evolution remain unaccepted.
This documentation update reads only the supplied receipts and performs no
production action or gate change.

## Mainline 1.14.72: single-target controlled diagnosis

The new source observes one authorized target through the native controlled
recall invocation, retaining actual stage presence, target-specific rejection
codes and missing/unknown observations. Query-generation metadata contains only
the material field, lengths and truncation, without query or memory text.
The engine/source/scope/query binding isolates nested and concurrent recalls;
the observer adds no search, record read or injected candidate. The scheduler
retains the new trace in bounded failed-case references for later runs.

The original query, case ID, expected target, selector, grounding and quality
thresholds remain unchanged. The 1.14.71 receipts above still lack per-target
membership; they cannot inherit these new fields. Native synthetic cases likewise
cannot establish a historical production query's material type or prove the
earlier miss has recovered. The later 1.14.72 readback above verifies the native
trace on one failed controlled case; formal quality remains unaccepted.
See [target trace fields](evaluation.md#controlled-target-stage-trace-11472).

## Earlier reported deployment and nightly: 1.14.69

Commit as supplied: `1729dfd2`. Deployment ran 21:10:56–21:23:40 with `ok=true`;
install, binding refresh and health checks exited 0. The RPC service was active
and its target identity matched the receipt. Times are as reported; the run
date and receipt timezone were not separately supplied.

Nightly `ref_0b76c0408ad1` ran 21:23:41–21:32:59 and exited 1. Its only failed
step was `research_closure_review`, for `replay_1df2894e0453`, with
`command_completion_failed:provider_request_failed`. This identifies an
execution failure in model review; it does not identify a network, configuration,
quota or other underlying provider cause.

The controlled memory baseline ran 10 samples: nine passed and one failed
`known_item_missing`, returning zero records. Failed case:
`controlled-memory-f5894372aefafa1900dc2d02`. This confirms execution of the
controlled path, not formal memory/business acceptance or independently
justified no-answer. The existing report does not supply enough stage evidence
to diagnose the retrieval miss as an algorithm defect.

Collection reports four new cases and 156 existing cases. All four new cases
have empty `candidate_refs` and completed non-certifying conclusions with
`no_candidate_refs`; they do not create trusted labels or certify correct
no-answer. Formal recall acceptance remains false at 0/3.

The single `semantic_off_topic` now retains native pointers:
pending `prqp_aaf8c42199c6377220b39bf450331dc4` and review
`prar_38c268153d484e3668e1afcb991ee123`. The operator identifies this as an
existing 1.14.58 case, not one of the four new cases. No additional decision,
observation or capture association is reconstructed from the earlier full-store
observations. This verifies the supplied reason-to-receipt reference, without
claiming a new-version semantic regression.

Evolution still has no candidates. `knowledge_inventory_truncated` blocks
applicability review; this is a bounded-inventory workflow limitation, not a
reason to say the loop only needs more data. No successful evolution transaction
or participant-level recall acceptance is supplied by this readback.

## Mainline 1.14.70: complete inventory and bounded diagnosis

The new source advances knowledge inventory through bounded persistent pages,
retaining progress instead of raising a fixed window or accepting the first
window as the whole corpus. A unique applicable source cannot be certified until
the complete inventory has been checked under the same authority and evidence.
Provider failures receive safe categories, and controlled misses retain bounded
retrieval-stage diagnostics. Missing or ambiguous evidence remains blocked.

The source contracts and their earlier validation do not retroactively change
the reported 1.14.69 outcomes. The later 1.14.71 deployment/nightly success is
recorded separately above; it does not establish the cause of the earlier
provider failure, formal recall acceptance or a successful evolution result.
See [inventory review](operations.md#knowledge-applicability-for-capability-gaps)
and [controlled checks](evaluation.md#controlled-baseline-from-existing-memory-11469).

## Small participant readback on 1.14.69

The operator additionally reports a real Hermes session with healthy queuing
and no observed missing writes. Retained conversation sources (L0) contained
both a state-change statement and an older state reference, while two specific
durable-memory (L1) queries returned empty results. Business questions and
account details are omitted from this public summary.

The readback supplies no exact source IDs, resolved scope, full deployed commit
or extraction/retrieval stage fields for those queries. It cannot identify the
affected source transactions or assign the result to a new algorithm regression
or to one of the source defects found locally. This is a small participant
observation, not completed four-category acceptance, a team average or a trusted
formal case. The official deployment/nightly results above remain unchanged.

## Mainline 1.14.71: memory extraction and source ordering

Native source checks reproduce four separate defects: an entire mixed input
was filtered before the model because it ended in a question; an empty formatted
USER section consumed an ASSISTANT line; an older source could overwrite newer
state; and a current source reload could still use stale caller text. The
repairs address these paths without assigning them to the participant's
unbound production queries.

Independent statements followed by a question can reach the existing model,
without converting questions into facts. Role boundaries, current source binding
and atomic source-time checks protect the write path. Reference-only extraction
diagnostics record actual admission, model, filter and store outcomes. They do
not certify factual truth or semantic grounding for every assistant/model output.
Ordering guards only established same-key or explicit update-target relations;
different-key paraphrases can remain separate active states. Per-write/source
snapshot checks do not make the whole extraction a single transaction.
Historical `NoMemory` outcomes are not bulk replayed or changed into passes.

The 1.14.71 deployment and nightly receipts above establish execution success,
not recovery of the participant's unbound queries, production role correctness,
factual truth, formal business quality or a completed evolution transaction.
Earlier 1.14.70 source/CI validation remains specific to that release. See
[extraction diagnostics](operations.md#conversation-capture-and-memory-extraction).

## Earlier reported nightly execution: 1.14.67

Bound commit: `4257d23c156703b71cba13042ee741a014b46141`.
Nightly `ref_5df3562505ca` ran 23:02:39–23:11:25 and exited 0.
Times are as reported; the run date and receipt timezone were not
separately supplied.

The durable report and corresponding original logs contain no
`production_recall_collection` section. Its `new_count`, `existing_count`,
`new_pending_record_ids` and `skipped` are **missing, not zero**. No new case
with non-empty candidates can be identified from those artifacts, so its
review reasons, semantic quality, signals and item states remain unknown.
The saved `recall_quality_gate` log exposes only `ok`, `blocked_reason`,
`skipped_reason` and `blocking_metrics`.

`recall_semantic_relevance.new_count=9` is a semantic-observation count.
It cannot establish how many production cases were collected or identify
their pending IDs. This is a **collection traceability gap**, not evidence
of a retrieval regression. Cases from elsewhere in the store are not
substituted for this run's unidentified new captures.

This readback supplies no new deployment receipt, formal-case count,
memory/evolution result or verification of the 1.14.67 review-reference
projection. Earlier detailed measurements remain attached to their original
releases. The operator inspected existing artifacts without rerunning
production or generating test questions.

Mainline 1.14.68 retains bounded collection counts, new-case pointers and
exact same-run review associations in subsequent nightly summaries. Missing
legacy fields stay unknown; the change does not reconstruct this batch,
authorize labels or establish that the first trusted case has been obtained.
See [collection diagnostics](operations.md#nightly-production-case-collection-diagnostics).

## Mainline 1.14.69: controlled evaluation and governed maintenance

These implementation contracts are now accompanied by the 1.14.69 operator
readback above: controlled checks ran, new cases were closed without labels,
and inventory truncation blocked knowledge review. Accepted deployment and
partial execution do not establish a trusted-case increase, successful
maintenance transaction or autonomous-evolution outcome.

| Path | Implemented behavior | Preserved boundary |
| --- | --- | --- |
| Controlled memory baseline | After an explicitly successful empty replay with no selected configured dataset, derive known-item cases from existing exact-scope/source-authorized records and measure actual runtime recall, with source and cutoff revalidation | A controlled pass stays `memory_benchmark_accepted=false`; no natural-production question, trusted label or no-answer evidence is generated |
| Knowledge applicability review | Use installed Hermes tool-free review of approved, trusted, canonical paper-backed knowledge against a live exact revision, keeping digest-bound quote references and revalidating revocation/current inputs | No eligible evidence, transport failure or ambiguous applicability stays blocked; an applicable link grants no write authority |
| Hermes v2 gap maintenance | Bind a legitimate known-gap context to registered candidate bounds, protected tests, independent suite evidence, official provider attestation and trusted v2 machine policy; retain the existing transaction owner | Known or user-reported maintenance does not certify autonomous discovery/product completion; effects and recovery remain guarded |

Writable candidates exist only within the supported immutable registered
contract and matching protected test-plan/machine-policy bounds. Unsupported
targets or absent authority remain evaluation-only; existing zero-write
hypotheses retain their bounds. Neither a generated query,
a model judgment nor a profile gap can supply missing trust or authorization.
The controlled run demonstrates source availability for those known-item
checks. At that 1.14.69 readback, inventory truncation prevents completing knowledge applicability;
no successful maintenance transaction or valid production code-maintenance
authority is established by that readback.

See [controlled benchmarks](evaluation.md#controlled-baseline-from-existing-memory-11469),
[applicability review](operations.md#knowledge-applicability-for-capability-gaps)
and [maintenance authority](operations.md#hermes-v2-known-gap-maintenance).
The existing three-case formal floor and quality thresholds remain unchanged.

## Earlier 1.14.66 nightly and traceability readback

Bound commit: `2587331f26062ceba5a35b71852b43c94c76991a`.
Nightly `ref_12f098002823` ran 22:05:39–22:14:44 (reported times; receipt
timezone not separately supplied), bound to deployment `rec_f7c085d6a768`.
Exit 0, `Result=success`, `execution_ok=true`, no failed steps. Produced 6,045,
promoted 0, rolled back 55; rollback is a task count, not a release rollback.

Formal `recall_quality_accepted=false`, evidence `insufficient`. Ten known-item
smoke samples executed with no missing/invalid metrics, but trusted labels and
release authority did not pass validation. The readback supplied no new smoke
accuracy values. Accepted cases are 0/3. All 343 reviews completed: 0 passes,
327 not-evaluable closures, 5 closures without a label, 11 rejections, 0 pending
or open reviews. Reasons include `pending_capture_decision_missing=186`,
`no_candidate_refs=135`, `pending_capture_boundary_invalid=10` and
`semantic_off_topic=1`; reasons may overlap. Memory source `replay_dataset`
has zero retrieval cases, `memory_eval_dataset_empty`, and no run. Nine
evolution results have zero hypotheses, `hypothesis_missing_or_ambiguous`.

One real new dedupe decision, `pd:1cdce141f8f3ec2889ed263a46d223e5`, binds to
1.14.66 and the full commit above. It removed one candidate and recorded one
witness, with no truncation or missing match. Candidate
`mem_15ff033f0c1661a1e27406c7d99fa122` / `hermes` links to prior decision
`pd:c5c91b7c5ce3c4027e615c3aa71f2619`, citation `pm:7b21cefa55ab74866747`,
`not_used`, ever injected and terminal, basis `ever_injected`. This confirms
the diagnostic on that new sample, not the old four count-only decisions.

The nightly report has `evidence=[]`, `links=[]`, empty `detail`, and no
`review_results`, `review_receipt_id` or `pending_record_id`. Original nightly
logs also retain only counters. Its unique off-topic count therefore cannot
identify a review receipt, pending case, semantic observation or decision.
This is a **report traceability gap**, not proof of an algorithm regression.
Full-store observations `rec_66d6e1e8b9fc` (1.14.27) and `rec_cc1802dbad8a`
(1.14.58) are distinct historical observations; neither is certified as this
run's source. Mainline 1.14.67 adds bounded native receipt pointers for new
summaries without reconstructing this history or changing verdicts or gates.
All production readbacks in this section were read-only, without reruns.

## Earlier failed October 11 deployment attempt: 1.14.64

Source: the operator's final read-only receipt and task-state readback for full
commit `f61487e2d1d85ca291a240086fc70aeb921669a3`.

- The current release link switched to the target. The source integrity check
  passed, with zero bytecode files. These checks do not establish RPC readiness.
- Final receipt completed at **20:16:31**, as reported. Installer and binding
  refresh exit codes are **0**; the service is reported **`active`**.
- Health acceptance failed: **`health_exit_code=1`**, **`error=TimeoutError`**,
  final **`ok=false`**. Earlier observed health requests timed out at 10 and
  30 seconds. Timeout does not establish a crashed process or a specific cause.
- Closure summary: `blocked_stage=deployment_receipt`,
  `blocked_reason=health_fetch_failed`, `execution.closure_exit_status=1`,
  `business_closure_outcome=failed`. Hard errors contain `health_fetch_failed`
  at both `$.blocked_reason` and `$.deployment_receipt.error`; these are two
  references to the reported condition, not two independently identified causes.
- Last reported log stage: `binding_refresh=verified`, `scope_count=2`.
  **2026-10-11 20:16:32 +08:00** is the log file modification time, not an
  independent timestamp on that line. The final receipt time's timezone field
  was not separately supplied.
- Final handoff has `deploy_exit=1` and `deployment_ok=false`, so **nightly
  did not start**. The previously pending receipts have now finalized.
- At this failed attempt's readback, no new nightly or quality report was
  supplied. The latest database nightly then was `ref_840ade0e6fbf` from
  1.14.63; it could not accept that candidate.
- The operator only read receipts and task state; no rerun, restart or new
  health request was performed for the final readback.

Mainline 1.14.65 bounds waits for migration and optional candidate-health
snapshots, uses one in-flight reader per fragment, isolates caches per runtime,
and refuses readiness for unavailable migration status. Authenticated liveness
does not call these readers. Local SQLite contention reproduces the old request
timeout; the production request's underlying cause is still unconfirmed.
No 1.14.65 deployment or production recovery is reported.

## Session deduplication readback

Source: the operator's read-only trace of these four fixed previous decisions.
No rerun, production modification, query or memory body was supplied.

| Decision | Deduped candidates |
| --- | ---: |
| `pd:5c4e2e49b153777cd5e108f5e7307d80` | 1 |
| `pd:9b2bf1efc83d7fbef7ef80ef6d0181bc` | 1 |
| `pd:1310a6e43da8e3ab266a2ede496d6bbe` | 1 |
| `pd:0a393689845a99d5f485064e17b1bd75` | 2 |

Each decision's channel, authorized scope, source, session and preceding time
window contains historical items with `ever_injected=1`, closed as `not_used`.
That state means previously injected without recorded actual use. The ledger
also admits host `used/rejected` items and still-open volunteered reservations;
closed never-injected items without such host feedback do not suppress recall.
No defect in these rules has been established.

These old audits retain counts only, so the **five removed candidates across
four decisions cannot be matched individually** to that historical delivery.
Same-session delivery is not a 4/4 exact verification. Mainline 1.14.66 adds
[candidate/history witnesses](operations.md#session-deduplication-provenance)
for new decisions without reconstructing the missing historical IDs or changing
deduplication, ranking or quality gates. The later 1.14.66 sample recorded
above confirms this diagnostic on one new decision, without matching the
five historical removed candidates.

The operator found genuine terminal receipt
`pret_107d2400930d19d8fc15c6a33d9b43f4`: `disposition=not_evaluable`,
`close_reason=no_recall`, `recall_status=no_recall`,
`answer_quality=not_evaluable`, `does_not_certify_answer=true`,
`review_complete=true`, `reasons=["no_candidate_refs"]`.
This corrects the earlier characterization of that case as pending: its review
is complete, closed without a label; the original collection record remains.
The collector uses only the current decision's final persisted candidates,
without copying historical delivery into a positive label. No independently
justified no-answer case was verified end to end by this readback.

The audit does not establish a recall algorithm fault, current service health,
a newer nightly, formal business acceptance or team-wide recall accuracy.

## Earlier completed October 11 deployment and nightly: 1.14.63

Source: the operator's readback of persisted receipts bound to full commit
`4fbcbbc99e228c753c2e0e255b7bad264a98c01a`. These historical results remain bound
to that runtime identity; they do not describe the subsequent candidate switch.

- Deployment completed October 11, 2026 at 12:09:26. Association receipt:
  `rec_c37c15ee113e`; `ok=true`, install, binding refresh and health checks exited 0.
- At that readback, the release path matched the receipt; RPC process, storage
  and readiness were reported healthy.
- Nightly ran 12:09:26–12:17:36, exited **0**, with `execution_ok=true` and
  **no failed steps**. Produced **5,981**, promoted **10**, rolled back **54**.
- Nightly receipt: `ref_840ade0e6fbf`; quality report: `ref_6ae80df64e32`.
- Smoke **10/10**, failed **0**, error rate **0.00**, hit@1 and hit@5 **1.00**.
- Formal gate remains **`gate_ok=false`**, with
  **`recall_quality_evidence_incomplete`**; accepted cases remain **0/15**.
- Label review processed **321**, passed **0**, pending **0**. Detailed current
  reason counts were not supplied in this initial readback; older counts are not
  assigned to this run.
- Memory benchmark has **0 retrieval cases**, `memory_eval_dataset_empty`, and
  did not run. Current dataset-source field was not supplied in the initial readback.
- Dynamic evolution has **nine results**, each with candidate hypothesis count
  **0**. The producer lacks an applicable knowledge association:
  `no_applicable_knowledge_link_for_gap_revision`.
- Times are the operator's reported times. The receipt's timezone field was
  not supplied.
- The operator only read existing results, without a rerun or production change.

The preceding smoke error-rate violation did not recur, and the gate reason is
now evidence insufficiency. This confirms the reported smoke recovery on
1.14.63, without establishing natural-query or colleague-wide quality. No
per-case identity comparison or historical candidate/stage trace was supplied;
smoke recovery alone cannot prove where the older target was lost or that an
identical case set was rerun. Formal labels, a retrieval benchmark and eligible
capability knowledge links remain necessary under the existing gates.

## Team recall in actual Feishu conversations

The operator additionally clarified that **鸿泰、鸿欣、小马哥 have no completed
actual-conversation recall acceptance**. Earlier controlled checks returned
empty results, but the session identity and environment were not fully
verified. Those observations do not establish that any participant currently
has a recall fault. Shared-service checks and the historical 10/10 smoke do not
establish accurate recall for those participants either.

Current status: **1.14.72 deployment acceptance and nightly execution passed,
and formal recall remains 0/3. All five new cases have empty candidates;
365 reviews have no pending/open work. Natural-question recall under each
colleague's actual identity remains unverified**. Verify the
actual host, effective scope and environment first, then cover known memories
with natural questions, no-answer near neighbors, cross-member permission
boundaries and authorized source readback. Keep participant-level persisted
results separate from aggregate smoke and each release's exact formal policy.
See the [team recall acceptance guide](team-recall-acceptance.md). No team
acceptance was executed or certified by this documentation update.

The historical 1.14.63 run uses `production_recall_gate_policy.v4`, which
requires at least 15 cases and 15 labels overall and has no fixed per-client
quota. Its coverage is limited to observed dataset channels, not all installed
or enabled hosts. The previous documentation's three-client five-case rule was
incorrect for this runtime. A qualifying Hermes-only dataset can satisfy the
coverage prerequisite; this does not establish that all team use is Hermes-only
or that every participant was tested. Keep actual team usage, dataset acceptance
and cross-client product coverage separate. The documentation correction itself
did not change that run's policy.

## Mainline 1.14.64: explicitly requested three-case floor

The operator subsequently requested **3 cases / 3 labels**. Mainline 1.14.64
implements `production_recall_gate_policy.v5` with that floor in each dataset's
existing exact scope. Label trust, release authorization, source/scope boundaries,
ranking, resources, isolation and noninferiority remain required. Coverage stays
limited to observed dataset hosts; it grants no cross-member access.

The historical completed nightly on **1.14.63 / `4fbcbbc9`** reports **0/15**
accepted cases under its historical v4 rule. Do not relabel that result as 0/3
or use it to accept the failed 1.14.64 deployment attempt. Historical policy
payloads and digests keep their original floors; old receipts cannot become
accepted by changing their policy tag. No 1.14.64 nightly result is supplied.

Existing authorized memories can provide controlled positive targets, source
readback and contrast probes; they do not automatically become trusted labels
or natural-production cases. See [the four-check procedure](team-recall-acceptance.md#using-existing-memory-for-four-checks).

## Earlier October 11 deployment and nightly: 1.14.62

Source: the operator's persisted-receipt readback for full commit
`82cba75afcea7e50a8fee758ee24b03f5b286717`. These results belong to this deployed
commit, not a later documentation or diagnostic patch.

- Deployment completed October 11, 2026 at 10:36:45 (reported time; receipt
  timezone unspecified). Association receipt: `rec_710320cdd83e`.
- Deployment `ok=true`; install, binding refresh and health checks all exited 0.
  The live release path matches the receipt; RPC process, storage and readiness
  are reported healthy.
- Nightly ran 10:36:46–10:45:00, exited **1**, and produced 5,897, promoted 10,
  rolled back 53. Persisted reference: `ref_e8739d53bb13`.
- Only failed step: **`recall_quality_gate`**.
- Quality report `ref_a2dee7107fde`: smoke **9/10**, blocked with
  **`recall_quality_gate_failed`**, error rate **0.10 > 0.05** (required ≤0.05).
- Follow-up diagnostic readback: numerator **1**, denominator **10**;
  `known_item_miss_with_return_count=1`, `no_answer_return_count=0`.
  The failed case used topk 5, returned two items, was non-empty, matched no
  expected item, and had smoke `noise_rate=1.0`. Expected/actual grouping was
  the operator's presentation of fields, not an original nested report object.
  Case ID: `generated-digest_bc2b3a1308f3d795`; returned IDs:
  `ktop_f52653534923ea69`, `digest_76552a58fd3bba5d`.
- Formal accepted cases **0/15**; automatic review processed **312**, passed **0**.
  A pending-count field, model identity and new authorization evidence were not
  supplied for this run; they must not be copied from historical runs.
- Memory benchmark: **0 retrieval cases**, `memory_eval_dataset_empty`;
  current dataset source was not supplied.
- Dynamic evolution: **nine results**, all candidate hypothesis counts **0**.
  No current producer-status/skip details were supplied.
- The operator reports no repeated deployment or rerun.

Unlike the earlier evidence-only block, this run has an explicit measured
violation that fails the gate. The metric combines known-item misses with a
return and erroneous returns on no-answer cases; this run's violation is the
first kind. It does not establish no-answer or colleague-wide natural-query
quality. Missing formal evidence remains a separate blocker. Retrieval root
cause in the actual run is not yet confirmed; changing the metric, expected
item or threshold cannot establish a repair.

Additional operator readback compares the target (digest, October 10, 256
distinct source IDs), the returned digest (October 11, the same 256-source set),
and the returned topic (one source ID, different set, no digest date). The two
digests have identical first 220 summary characters, both at record top level
and in `content.summary`; the topic's prefix differs from both. This confirms
the collision's input shape. It does not prove that the historical execution
lost the target at deduplication without that run's candidate/stage trace.

The classification repair is confirmed by that 1.14.62 persisted nightly summary:

| Directly preserved code | Reported occurrences |
| --- | ---: |
| `pending_capture_boundary_invalid` | 10 |
| `pending_capture_decision_missing` | 186 |
| `semantic_judgment_not_delivered` | 6 |

The third count is **6 in this run**, versus **5 in the earlier 1.14.61 run**.
These codes no longer collapse to `reason_not_allowlisted`. Counts can overlap;
they do not identify the cause of the known-item miss.

## Earlier October 11 nightly: 1.14.61

Source: the operator's supplied readback, identifying **1.14.61 / `7a625fe1`**.
The repository resolves that abbreviation to
`7a625fe1a280862b5ef4c1680d5162efab848bcb`; a full commit field from the receipt
and a new deployment receipt were not supplied.

- October 11, 2026, 03:30:00–03:37:46, as reported by the operator. The client
  timezone is Asia/Shanghai; the receipt's own timezone field was not supplied.
- Exit code `0`, `execution_ok=true`, no failed steps.
- Produced 5,776; promoted 10; rolled back 52.
- Persisted nightly reference: `ref_a650963881f2`.
- Current RPC process, storage and readiness are reported healthy.
- Automatic label review enabled: 304 reviewed, 0 passed, 304 not passed,
  **0 pending**. Formal accepted cases remain **0/15**.

| Review reason | Reported occurrences |
| --- | ---: |
| `reason_not_allowlisted` | 201 |
| `no_candidate_refs` | 98 |
| `independent_signal_agreement_missing` | 9 |
| `no_candidate_delivered` | 5 |
| `semantic_off_topic` | 1 |

One case can have multiple distinct reasons. These counts cannot be added to
obtain a case total. A follow-up operator readback checked all 304 per-case
receipts and deduplicated each receipt's raw reasons. It supplied these codes:

| Underlying code | Reported occurrences |
| --- | ---: |
| `pending_capture_boundary_invalid` | 10 |
| `pending_capture_decision_missing` | 186 |
| `semantic_judgment_not_delivered` | 5 |

The operator confirmed the third count as **5**: **10 + 186 + 5 = 201**,
matching the nightly aggregate and involving 201 distinct receipts in this run.
No additional reason code is missing from this reconciliation.

The first is a capture-envelope/boundary rejection. The second means no
authoritative decision row matched the capture's exact owner/channel/source;
the code alone cannot distinguish pruning from a mismatch. The third means no
candidate was actually injected, so no semantic relevance is certified. These
are eligibility/evidence failures and do not establish a recall algorithm defect.
The collector already uses bounded retention pins for capture decisions;
missing historical evidence cannot be recreated or accepted by a diagnostic fix.

- Memory benchmark: `not_run`; source `replay_dataset`, retrieval cases **0**,
  reason `memory_eval_dataset_empty`. No accepted memory evaluation is reported.
- Hypothesis producer: `no_eligible_evidence`; one skipped gap with
  `no_applicable_knowledge_link_for_gap_revision`.
- Dynamic evolution: nine results, all with `candidate_hypothesis_count=0` and
  `hypothesis_missing_or_ambiguous`.
- The operator reports only reading existing results, with no rerun or modification.

Pending reviews are no longer the primary explanation. Current blockers are
non-passing reviews, an empty retrieval dataset and missing applicable hypothesis
evidence. No new smoke/ranking metrics, model identity or release-authorization
evidence were supplied. Older evidence gaps must not be silently filled from
this execution success.

## Earlier 1.14.60 deployment and nightly

Source: the operator's supplied readback. The deployed full commit is
`40285e36ab2e43d22fe20ee658af95eab4b5bcad`; the following results belong to that
exact release, rather than a later documentation commit.

- Deployment receipt: `ok=true`; installation, binding refresh and health checks
  succeeded. The live release path matches the receipt; RPC process, storage
  and readiness are reported healthy. A deployment receipt reference was not supplied.
- One additional nightly: 19:04:03–19:11:56, timezone unspecified.
- Exit code: `0`; `execution_ok=true`; no failed steps.
- Persisted nightly reference: `ref_3287829a4ae2`, bound to the full commit above.
- Produced: 5,683; promoted: 10; rolled back: 51.
- `research_closure_review` is no longer reported as failed. Actual review counts,
  verdicts and provider/model identity were not supplied in this readback.
- Formal recall cases: **0/15 accepted**, with **five pending review**. Evidence
  remains `insufficient`; label trust and release authorization are unverified.
- Additional automatic-label-review readback: `status=completed`, `enabled=true`,
  `pending_count=5`. Reason counts are five each for
  `independent_signal_agreement_missing`, `semantic_judgment_unknown` and
  `tool_free_transport_unavailable`. These counts were aggregated from persisted
  per-case reviews; they were not fields saved directly in the nightly summary.
- Memory benchmark: `not_run`, not accepted. Its specific skip/blocking reason
  was not supplied, so the earlier empty-dataset diagnosis is not assigned to this run.
  Associated receipt: `ref_d5b6b1ded019`; dataset source and retrieval-case count
  were not supplied in readable form.
- Dynamic evolution: **nine waiting items**, all with `hypothesis_missing_or_ambiguous`.
  `result_count=9`, `results_truncated=false`. The supplied reason-count fragment
  omits its numeric value; candidate counts and hypothesis-producer skip reasons
  were absent from the available persisted summary and journal.
- The operator reports one extra nightly, with no repeated deployment or restart.

Execution success closes the reported execution failure for this run. It does
not supply trusted production labels, a memory benchmark or accepted hypotheses.
No new smoke ranking or latency measurements were supplied for this release.

## Earlier 1.14.58 nightly failure

Source: the operator's screenshot and subsequent record readback report. The
operator identified the deployment as **1.14.58**, full commit
`534bb2f02562ed6cc45a7e3b8d87d59ff54a2aa1`. The production database was not
independently queried by this documentation publisher.

- Nightly started once automatically after a successful deployment receipt;
  the operator did not start it again.
- Run window: 15:21:17–15:27:25, timezone unspecified.
- Exit code: `1`; `execution_ok=false`.
- Persisted reference: `ref_4cc83efb2f5d`.
- Only reported failed step: `research_closure_review`.
- Produced: 5,620; promoted: 10; rolled back: 50.
- Formal cases: 0/15, with two pending review. Recall evidence remains
  `insufficient`; label trust and release authorization are unverified.
- Memory benchmark did not run; `memory_benchmark_accepted=false`.
- Ten evolution items wait with `hypothesis_missing_or_ambiguous`.

The operator confirmed that this durable nightly receipt did not save
`research_closure_review.unavailable_records`. Readback of 15 research records
in the execution window reported `review_error=research_review_llm_unconfigured`,
including `replay_0c66c27caffd` and `replay_2fa4d834f02a`. These are separately
read diagnostic fields, not a reconstructed unavailable-record list.

This establishes a missing research-specific/shared model command in that run.
It is separate from the earlier missing `codex` executable and dataset-path
permission restrictions. Configuring a working managed review bridge remains
necessary; package diagnostics and preflight checks do not supply a model route.

## Earlier 1.14.57 execution

- Run window: 10:54:37–11:01:18, as supplied by the operator; timezone unspecified.
- systemd exit code: `0`.
- Scheduler: `execution_ok=true`, with no failed execution steps.
- Produced: 5,645; promoted: 10; rolled back: 50.

Counts and exit status describe execution, not business acceptance.

## Earlier 1.14.57 recall evidence

| Evidence | Reported result | Acceptance interpretation |
| --- | --- | --- |
| Known-item smoke | 10/10; hit@1, hit@5, MRR all 1.00; P95 340.8 ms | Fixed known items are retrievable; natural-query and colleague-wide recall remain unproven |
| Formal gate | `gate_ok=false`; `recall_quality_evidence_incomplete` | Formal acceptance blocked by incomplete evidence |
| Accepted production cases | 0/15; another 2 pending review | Pending or generated cases do not count as accepted labels |
| Label trust / release authority | Smoke label trust not accepted; release authorization unverified | Neither success nor authorization may be inferred |
| Smoke noise | 0.80, compared with required ≤0.40 | Smoke observation only; no formal business score assigned in this run |
| Smoke precision@3 | 0.333, compared with required ≥0.60 | Smoke observation only; no formal business score assigned in this run |

The formal gate first stopped for missing evidence. It did not use the two
smoke ranking observations as a completed formal business evaluation.
The historical deployed 1.14.63 policy-v4 dataset required at least 15 cases and 15
accepted labels overall; observed dataset channels define the coverage scope,
with no fixed per-channel quota. This describes that release's code contract,
without assigning an unreported policy field to the historical receipt.
The subsequently requested v5 minimum is three cases / three labels in mainline
1.14.64; it does not change this older receipt.
Acceptance must retain trustworthy
labels, exact owner/source/channel boundaries, release identity and the required
release authorization evidence. The complete contract is in the
[evaluation guide](evaluation.md#production-recall-gate).

## Other incomplete evidence in 1.14.57

- Memory CI was skipped with `memory_eval_dataset_empty`. A separate passing
  code-capability evaluation cannot substitute for retrieval benchmark evidence.
- Dynamic capability evolution has nine waiting items, all marked
  `hypothesis_missing_or_ambiguous`. Diagnostics cannot fabricate knowledge
  links, grant behavior authority or close unresolved hypotheses.
- Research closure could not find the `codex` executable; model review was
  unavailable. An unavailable reviewer cannot approve a candidate.

## Earlier 1.14.57 receipt references

| Receipt | Reference |
| --- | --- |
| Nightly | `ref_1993a5692b9a` |
| Deployment association | `rec_300a9cce017c` |
| Recall quality | `ref_41e1ea639d00` |
| Memory evaluation | `ref_9b97981f26e0` |

These are runtime references, not public links or credentials. The operator
reported reading them back from persistent storage. This page includes no raw
queries, memory payloads or private labels.

## Mainline workflow repairs

**1.14.61** records completed pass/fail conclusions for every scanned automatic
label review, retaining reasons and exact evidence identity. Missing semantic
review, insufficient agreement or unavailable approval results in **fail**,
without a relevance label or benchmark certification. Captures stay active and
later qualifying evidence can trigger a new review. Semantic and research
review share supported Hermes runtime discovery while the semantic judge retains
tool-free/read-only execution. New nightly diagnostics retain bounded conclusion
and dataset reasons plus reported hypothesis counts; absent values remain unknown.
The earlier October 11 readback reports execution on 1.14.61 and zero pending
reviews, with all 304 reviews non-passing. This does not certify business quality.

**1.14.62** expands the versioned safe reason catalog to include capture,
original-query, feature-quality, candidate, semantic and native acceptance
failures previously generalized by an incomplete list. Unknown arbitrary text
remains redacted and oversized reason maps disclose truncation. Closed/reopened
supervisor receipt tests verify the exact native reasons and unchanged label
eligibility. This diagnostic patch cannot recover raw reasons absent from an
older summary. The earlier 1.14.62 production readback confirms the classification
repair; deployment passed and the nightly quality gate failed.

**1.14.63** fixes digest/synthesis identity collisions by using full content,
date, supporting evidence and source identity instead of a truncated summary.
An isolated native smoke with two differently dated digests sharing the same
256-source set reproduces the known-item miss using the unchanged mainline
dedupe method; the repaired method retains the target with the same query,
expected ID and cutoff. Exact duplicates still collapse; source quotas and
default digest suppression remain unchanged.
It also preserves expected record IDs alongside returned IDs and explicit
`false_recall_count` / `false_recall_denominator` in diagnostic quality receipts.
Closed/reopened storage tests distinguish a non-empty known-item miss, a
no-answer return and an empty miss, retain raw-body redaction, and require the
same ≤0.05 gate and nightly failure behavior. The historical operator readback
confirms deployed 1.14.63 execution and smoke 10/10, error rate 0.00. It does not
establish the historical loss stage, an identical case set or formal business acceptance.

The workflow repairs introduced in **1.14.58** are separate from the earlier
1.14.57 result. The operator identified the later failed run as commit `534bb2f0`:

- Research closure uses a configured model command, validates structured results
  and records the actual model/provider. Missing, failed or disallowed execution
  remains unavailable; unrelated replay records do not starve pending reviews.
- Catalog cases use their immutable capability executors. Memory benchmarks
  require retrieval cases; empty inputs persist a not-run receipt rather than
  becoming a pass. Dataset-generation failures remain failures.
- A dedicated memory dataset can be securely loaded from
  `EIMEMORY_MEMORY_EVAL_DATASET` or `<EIMEMORY_ROOT>/evaluation/memory_eval.json`
  before generated replay is considered. The automatic conventional-path entry
  is new; the ancestor ownership and protected write-mode checks already existed
  in 1.14.57, originating in `3f3365e3`. New tests exposed a host-path restriction
  when first exercising this entry. See [path security and test isolation](evaluation.md#existing-path-security-new-automatic-entry).
- Available exact binding cases can run diagnostics while missing bindings keep
  their gaps. Projection and catalog selection share a precise time cutoff.

Mainline **1.14.59** additionally preserves bounded research-review failure
reasons and validated bridge categories in durable nightly supervisor receipts.
The full report's `error` reaches the step summary, and managed deployment
checks durable route configuration before stopping writers or switching current.
Unavailable records keep
structured current failure metadata. Retries clear that current metadata while
leaving the previous supervisor receipt unchanged until another run writes it.
See [durable diagnostics](evaluation.md#durable-review-failure-diagnostics-11459).

Mainline **1.14.60** adds automatic Hermes runtime discovery when no explicit
review command is selected. Reviews follow the installation's current provider,
model, credentials and SDK transport without a fixed host path or model label.
Managed preflight accepts this route only after runtime/configuration checks;
actual model response validation and all formal quality requirements remain.
See [automatic Hermes review](evaluation.md#automatic-hermes-review).

A subsequent operator screenshot reports **1.14.60 / `57013648`** with 228 local
regression passes and two failures in `test_governance_env.py`, before deployment
or nightly execution. Those tests expected an unavailable reviewer but did not
isolate the OS account home where Hermes was installed. The follow-up test-only
repair retains their rejection assertions, isolates account lookup and system
PATH in the test subprocess, and separately verifies service-runtime discovery
and current-profile validation. It does not change production routing or gates.

The earlier operator readback reports deployment and nightly execution success for
**1.14.60 / `40285e36`**, as detailed above. Formal business acceptance remains
open. Earlier focused local verification reported 438 passes and four
existing audit failures reproduced on the unmodified `eab88240` baseline.
Passing implementation tests does not supply missing production labels.

## Work required to close acceptance

The latest 1.14.72 deployment and nightly execution passed; formal recall remains
0/3. Controlled checks passed 7/8. The failed target was absent from the complete
source-returned window of 14 candidates, before the selector; the upstream cause
remains unknown.
Evolution still lacks an applicable current-revision knowledge link. The
remaining work is evidence-specific:

1. Trace a real newly collected case with non-empty candidates from its native
   pending ID and capture decision to its exact review receipt. The 1.14.68
   diagnostic retains these bounded associations. The latest 1.14.72 batch has
   five new cases, all empty, and 164 existing cases with no collection skips;
   the older 1.14.67 batch remains unidentified.
   Check delivery, host-use/proof
   signals and semantic verdicts only where recorded. Missing historical
   decisions and repeated scans are not new production cases. Neither nine
   semantic observations nor a process exit code establishes a trusted case.
   Preserve non-passing outcomes until independent signals agree.
2. Assemble accepted natural production cases with exact channel, owner, source,
   label trust and release authorization evidence. Do not turn smoke labels into
   accepted production gold.
3. Keep a controlled existing-memory baseline separate from independent memory
   acceptance. The 1.14.69 fallback can measure an eligible empty-replay path;
   formal memory acceptance still needs independent labelled retrieval evidence.
   For the current controlled miss, inspect query generation, source matching and
   source-internal filtering to distinguish the cause before source return.
   The target's absence in this run excludes its grounding rejection; it does
   not prove index absence. Retain grounding and quality thresholds. Older
   aggregate drops do not become per-target causal proof. Code/capability
   execution cannot substitute for either result.
4. Supply applicable, traceable knowledge links for the exact gap revision,
   supported by independent evaluation and feedback. The 1.14.69 producer can
   review eligible canonical knowledge automatically, but the 1.14.72 readback
   still lacks an applicable current-revision link. Its inventory progress was
   not supplied; the earlier 1.14.69 truncation is not this run's inferred cause.
   The bounded scan must finish the corpus before certifying uniqueness; no source
   approval, trust, canonical support or missing inventory can be fabricated.
5. Assess persisted outcomes against their exact runtime identity and policy.
   For maintenance candidates, independently verify registered writable bounds,
   machine policy, suite evidence, provider attestation and transaction effects.
   Assess ranking and noise only with qualifying formal evidence and unchanged
   thresholds; a controlled pass cannot substitute for that acceptance.

The earlier smoke violation remains historical evidence, and the ≤0.05 limit
still applies. Its exact loss stage would require historical candidate traces
or an identical-case comparison; the new aggregate smoke success does not
supply that causal evidence. This separate diagnostic question does not change
the current formal gate's evidence requirements.

Historical L5 readiness, a clean deployment, a 10/10 smoke result or a code pass
cannot perform these steps. The operator's latest readback is recorded above;
this documentation update performed no production run or gate relaxation.

## 1.14.102 (69dd0957, deployed 2026-10-16 04:2x +08:00)

- Nightly reads each agent's own production-recall dataset (env map, per-agent path, or accepted cases). hongrui now reaches its 3 accepted cases; gate blocked by `secure_dataset_fingerprint_missing` (accepted cases built in memory carry no file fingerprint). Sanctioned next step: `stage_production_query_dataset` + `activate_production_query_dataset` into `<root>/evaluation/agents/<agent>/` so the pointer carries digest/size/device/inode evidence.
- `prior_release_baseline_high_water_missing`: the baseline is written only by the deploy-time pre-switch bootstrap (`deploy/bootstrap_production_recall.py`, gated by `EIMEMORY_POST_SWITCH_GATES=1`) against an eligible, fingerprinted dataset, keyed by (running commit, dataset digest). There is no post-switch self-blessing path. No eligible dataset existed at prior deploys, so no anchor exists; the first deploy after a fingerprinted dataset is activated creates it.
- memory.recall probe: scope now the canonical deployment scope; query configurable (`EIMEMORY_MEMORY_RECALL_PROBE_QUERY`, `EIMEMORY_MEMORY_RECALL_OPENCLAW_PROBE_QUERY`, or settings keys). Default trusted queries have no lexically grounded active memory in hongrui scope, so binding stays missing until an operator-chosen query is set.
- Recall candidates: index collectors admit only `status='active'` (archived rows had starved the pool).
- Backup: SIGTERM unwinds and removes its partial; directory flock serializes runs; orphan `.tmp-*` (not `-failed`) swept at start. `.tmp-20261015T184355Z` was already gone.
- Nightly 04:28: all four agents false_recall 0.0; hongrui MRR 1.0 (fingerprint block), hongtai 1.0 / hongxin 1.0 / xiaomage 0.9 (`recall_quality_evidence_incomplete`, no trusted labels).
